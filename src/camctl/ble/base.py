"""Brand-neutral Bluetooth LE camera remote.

A driver subclasses Remote and implements the brand's GATT layout, session setup, pairing handshake and button
primitives (`_shutter`, `_record`, ...). Remote provides: connection with error translation, the encrypted-write
retry loop used while an OS bond is being established, a timestamped notification log, and the result objects
(ShotResult/RecordResult/PairResult) the CLI/MCP report.

Library code never prints (the MCP server talks JSON over stdio): user-facing prompts go to `on_prompt(msg)`,
diagnostics to the `camctl.ble` logger.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, ClassVar

from . import bond
from .errors import BleError, ConnectFailed, NotSupported, PairingNotSaved, PairingRequired

log = logging.getLogger("camctl.ble")

# capabilities
SHOOT = "shoot"          # full release (single frame, or hold for a burst)
AUTOFOCUS = "autofocus"  # half-press / AF
FOCUS = "focus"          # manual focus drive near/far (Sony `focus(direction, seconds)`)
RECORD = "record"
ZOOM = "zoom"
BULB = "bulb"
AF_ON = "af_on"
C1 = "c1"
CAPABILITIES = (SHOOT, AUTOFOCUS, FOCUS, RECORD, ZOOM, BULB, AF_ON, C1)
_CAP_TEXT = {SHOOT: "shutter release", AUTOFOCUS: "autofocus (half-press)", FOCUS: "manual focus drive",
             RECORD: "movie recording", ZOOM: "zoom", BULB: "bulb exposures", AF_ON: "AF-ON", C1: "the C1 button"}

Prompt = Callable[[str], None]


def seconds_arg(value: Any, what: str, maximum: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number of seconds, got {value!r}.") from None
    if not 0 <= v <= maximum:
        raise ValueError(f"{what} must be between 0 and {maximum:g} seconds, got {v:g}.")
    return v


@dataclass
class Event:
    t: float                  # time.monotonic()
    name: str                 # focus | shutter | recording | response | error | notify
    value: Any
    raw: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, t0: float) -> dict[str, Any]:
        d = {"t": round(self.t - t0, 3), "event": self.name, "value": self.value, "raw": self.raw}
        d.update(self.detail)
        return d


@dataclass
class ShotResult:
    brand: str
    address: str
    released: bool | None          # True: camera reported shutter activity; False: it reports but didn't; None: no feedback
    focus_locked: bool | None
    af: bool
    hold: float
    feedback: bool                 # whether this camera reports shutter activity over BLE at all
    shutter_active: bool = False   # a shutter-active notification (Sony 02 A0 20) was seen
    events: list[dict[str, Any]] = field(default_factory=list)
    hint: str | None = None
    status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecordResult:
    brand: str
    address: str
    action: str                    # toggle | start | stop
    recording: bool | None         # state after the command; None when the camera did not report it
    reported: bool                 # the camera confirmed the state
    changed: bool = True           # False when start/stop was a no-op because the state already matched
    events: list[dict[str, Any]] = field(default_factory=list)
    hint: str | None = None
    status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PairResult:
    brand: str
    address: str
    name: str | None
    bonded: bool                   # an OS-level bond was used/created (False for handshake-only brands)
    verified: bool | None = None   # set by verify_pairing(): disconnect + reconnect worked
    extra: dict[str, Any] = field(default_factory=dict)   # persisted per-device data (e.g. Nikon dev id)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Remote:
    """One camera over BLE. Use `async with Driver(address) as r:` or connect()/disconnect()."""

    brand: ClassVar[str] = "generic"
    label: ClassVar[str] = "camera"                   # for messages: "Sony camera"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    verified: ClassVar[frozenset[str]] = frozenset()      # confirmed on real hardware by camctl
    experimental: ClassVar[frozenset[str]] = frozenset()  # from decompiled apps / unconfirmed guesses
    reports_shutter: ClassVar[bool] = False
    reports_focus: ClassVar[bool] = False
    reports_recording: ClassVar[bool] = False
    needs_bond: ClassVar[bool] = False
    rotating_address: ClassVar[bool] = False          # reconnect by re-scanning (Nikon, Panasonic)
    single_hold: ClassVar[float] = 0.05               # shutter hold for one frame
    af_wait: ClassVar[float] = 0.6                    # blind AF wait for brands without focus feedback
    confirm_wait: float = 10.0                        # time given to press OK on the camera after bonding
    setup_hint: ClassVar[str] = ""                    # camera menu for the remote function
    pair_hint: ClassVar[str] = ""                     # camera menu to start pairing
    unsupported_reason: ClassVar[str] = ""
    default_name: ClassVar[str] = "Camera"

    def __init__(self, address: str, *, name: str | None = None, extra: dict[str, Any] | None = None,
                 on_prompt: Prompt | None = None, on_event: Callable[[Event], None] | None = None,
                 remote_name: str = "camctl", connect_timeout: float = 15.0, bond_timeout: float = 30.0,
                 bond_retry: float = 3.0, confirm_wait: float | None = None) -> None:
        self.address = address
        self.name = name
        self.extra: dict[str, Any] = dict(extra or {})
        self.on_prompt = on_prompt
        self.on_event = on_event
        self.remote_name = remote_name
        self.connect_timeout = connect_timeout
        self.bond_timeout = bond_timeout
        self.bond_retry = bond_retry
        if confirm_wait is not None:
            self.confirm_wait = confirm_wait
        self.status: dict[str, Any] = {}
        self.events: deque[Event] = deque(maxlen=512)
        self._client: Any = None
        self._pairing = False
        self._bond_prompted = False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.address} connected={self.connected}>"

    # -- lifecycle --------------------------------------------------------------------
    async def __aenter__(self) -> "Remote":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    @property
    def connected(self) -> bool:
        c = self._client
        return c is not None and bool(getattr(c, "is_connected", True))

    async def connect(self) -> None:
        """Open the link and run the brand's per-connection session setup (handshakes included)."""
        if self.unsupported_reason:
            raise NotSupported(self.unsupported_reason)
        if self.connected:
            return
        await self._open()
        try:
            await self._setup()
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        try:
            await self._teardown()
        except Exception:  # noqa: BLE001 — teardown is best effort
            log.debug("teardown failed", exc_info=True)
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 — already gone; don't mask the command's result/error
                log.debug("disconnect failed", exc_info=True)

    async def pair(self) -> PairResult:
        """First-time pairing: connect with the brand handshake, prompting via on_prompt. Leaves the link open."""
        if self.unsupported_reason:
            raise NotSupported(self.unsupported_reason)
        if self.pair_hint:
            self.prompt(f"Pairing with {self.name or self.address}. {self.pair_hint}")
        self._pairing = True
        try:
            await self.disconnect()
            await self.connect()
            notes = await self._after_pair()
        finally:
            self._pairing = False
        return PairResult(self.brand, self.address, self.name, self.needs_bond, None, dict(self.extra), notes)

    async def verify_pairing(self, settle: float = 2.0) -> bool:
        """Disconnect, reconnect and send a harmless command to prove the pairing survives a new connection."""
        await self.disconnect()
        await asyncio.sleep(settle)
        try:
            await self.connect()
            await self._probe()
        except PairingNotSaved as e:
            raise PairingNotSaved(
                f"The camera did not keep the pairing (you must press OK on the camera's pairing prompt); clear the "
                f'Mac\'s stale bond (System Settings → Bluetooth → "{self.name or self.address}" → Forget, or pair '
                f"with allow_bt_restart=True), then pair again and press OK.") from e
        return True

    async def keepalive(self) -> bool:
        """Called periodically by the daemon. Returns False when the link is down (the daemon reconnects)."""
        return self.connected

    # -- brand hooks ------------------------------------------------------------------
    async def _setup(self) -> None:
        """Per-connection session setup (find characteristics, subscribe, brand handshake)."""

    async def _teardown(self) -> None:
        """Stop background tasks before the link closes."""

    async def _after_pair(self) -> list[str]:
        """Extra first-pairing steps after connect() (e.g. trigger bonding, wait for OK). Returns notes."""
        return []

    async def _probe(self) -> None:
        """A harmless authorised operation used by verify_pairing (default: connecting was proof enough)."""

    async def _shutter(self, af: bool, hold: float) -> None:
        raise NotSupported(self._unsupported(SHOOT))

    async def _record(self, want: bool | None) -> bool | None:
        """Press record. Return the new state if the brand knows it without notifications, else None."""
        raise NotSupported(self._unsupported(RECORD))

    async def _autofocus(self, seconds: float) -> None:
        raise NotSupported(self._unsupported(AUTOFOCUS))

    # -- helpers for drivers ----------------------------------------------------------
    def prompt(self, msg: str) -> None:
        """Tell the user to do something on the camera."""
        if self.on_prompt is None:
            log.info(msg)
            return
        try:
            self.on_prompt(msg)
        except Exception:  # noqa: BLE001 — a broken UI callback must not break the camera sequence
            log.debug("on_prompt failed", exc_info=True)

    def _unsupported(self, cap: str) -> str:
        if self.unsupported_reason:
            return self.unsupported_reason
        have = ", ".join(c for c in CAPABILITIES if c in self.capabilities) or "nothing"
        return f"The {self.label} does not support {_CAP_TEXT.get(cap, cap)} over Bluetooth (it supports: {have})."

    def require(self, cap: str) -> None:
        if cap not in self.capabilities:
            raise NotSupported(self._unsupported(cap))

    def _need_client(self) -> Any:
        if self._client is None:
            raise BleError(f"Not connected to {self.address}; use `async with` or call connect() first.")
        return self._client

    async def _open(self) -> None:
        from bleak import BleakClient

        client = BleakClient(self.address, timeout=self.connect_timeout)
        try:
            await asyncio.wait_for(client.connect(), self.connect_timeout + 5)
        except BaseException as e:
            try:   # half-open links otherwise linger in the OS until the peer times out
                await client.disconnect()
            except BaseException:  # noqa: BLE001
                pass
            if isinstance(e, Exception):
                raise bond.translate_error(e, self) from e
            raise
        self._client = client

    def _find_char(self, uuid: str, service: str | None = None, *, required: bool = True) -> Any:
        """Characteristic by full UUID, or by 8-hex-digit prefix. Searches `service` first, then everything."""
        client = self._need_client()
        want = uuid.lower()
        services = client.services
        ordered = []
        if service:
            s = services.get_service(service)
            if s is not None:
                ordered.append(s)
        ordered.extend(s for s in services if s not in ordered)
        for s in ordered:
            for c in s.characteristics:
                cu = str(c.uuid).lower()
                if cu == want or (len(want) == 8 and cu.startswith(want)):
                    return c
        if required:
            raise ConnectFailed(f"The {self.label} lacks Bluetooth characteristic {uuid}; "
                                f"{self.setup_hint or 'enable its Bluetooth remote function'}, and update firmware "
                                f"if it persists.")
        return None

    @staticmethod
    def _wants_response(char: Any, prefer: bool | None = None) -> bool:
        """Acked write if the characteristic has 'write' (unless prefer=False and it has write-without-response)."""
        props = list(getattr(char, "properties", None) or ["write"])
        if prefer is False and "write-without-response" in props:
            return False
        return "write" in props

    async def _gatt(self, op: Callable[[], Awaitable[Any]]) -> Any:
        """Run a GATT operation, retrying every bond_retry s while the OS bond is still being established."""
        tries = max(1, int(self.bond_timeout / self.bond_retry) + 1) if self.bond_retry > 0 else 1
        for i in range(tries):
            try:
                return await op()
            except Exception as e:
                if not bond.is_insufficient_encryption(e):
                    raise bond.translate_error(e, self) from e
                if i == tries - 1:
                    raise PairingRequired(
                        f"The {self.label} refused the command (link not encrypted after {self.bond_timeout:g} s); "
                        f"{self.pair_hint or 'put it in pairing mode'} and pair again, pressing OK on the camera.") from e
                if not self._bond_prompted:
                    self._bond_prompted = True
                    self.prompt("Bonding with the camera — if it shows a pairing prompt, press OK on the camera.")
                log.debug("encryption not ready (%s); retry %d/%d in %.1fs", e, i + 1, tries - 1, self.bond_retry)
                await asyncio.sleep(self.bond_retry)
        return None  # pragma: no cover

    async def _write(self, char: Any, data: bytes, *, response: bool | None = None, retry: bool = True) -> None:
        client = self._need_client()
        resp = self._wants_response(char) if response is None else response

        async def op() -> None:
            await client.write_gatt_char(char, bytes(data), response=resp)
        if retry:
            await self._gatt(op)
        else:
            await op()

    async def _best_effort(self, char: Any, *payloads: bytes, response: bool | None = None) -> None:
        """Button release: also runs on cancel/Ctrl-C so nothing stays held; errors are swallowed."""
        client = self._client
        if client is None or char is None:
            return
        for p in payloads:
            try:
                resp = self._wants_response(char) if response is None else response
                await client.write_gatt_char(char, bytes(p), response=resp)
            except Exception:  # noqa: BLE001 — link already down: the camera releases on disconnect
                log.debug("release write failed", exc_info=True)

    async def _subscribe_required(self, char: Any, callback: Callable[[Any, bytearray], None]) -> None:
        client = self._need_client()
        await self._gatt(lambda: client.start_notify(char, callback))

    async def _subscribe(self, char: Any, callback: Callable[[Any, bytearray], None]) -> bool:
        try:
            await self._need_client().start_notify(char, callback)
            return True
        except Exception:  # noqa: BLE001 — optional channel; callers decide what a missing one means
            log.debug("start_notify(%s) failed", getattr(char, "uuid", char), exc_info=True)
            return False

    def _event(self, name: str, value: Any, raw: bytes | bytearray | str = b"", **detail: Any) -> Event:
        ev = Event(time.monotonic(), name, value, raw if isinstance(raw, str) else bytes(raw).hex(), detail)
        self.events.append(ev)
        if name in ("focus", "shutter", "recording"):
            self.status[name] = value
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001
                log.debug("on_event failed", exc_info=True)
        return ev

    def _since(self, t0: float, name: str | None = None) -> list[Event]:
        return [e for e in self.events if e.t >= t0 and (name is None or e.name == name)]

    def _last(self, t0: float, name: str) -> Any:
        evs = self._since(t0, name)
        return evs[-1].value if evs else None

    @staticmethod
    async def _poll(pred: Callable[[], bool], timeout: float, step: float = 0.05) -> bool:
        """Wait for pred() in `step` sleeps (iteration-counted, so tests can stub asyncio.sleep)."""
        for _ in range(max(1, round(timeout / step))):
            if pred():
                return True
            await asyncio.sleep(step)
        return pred()

    async def _hold(self, press: Callable[[], Awaitable[None]], release: Callable[[], Awaitable[None]],
                    seconds: float) -> None:
        try:
            await press()
            await asyncio.sleep(seconds)
        finally:
            await release()

    # -- feedback ---------------------------------------------------------------------
    @property
    def feedback_ok(self) -> bool:
        """Shutter notifications are active on this connection."""
        return self.reports_shutter

    def _released(self, t0: float) -> bool | None:
        if not (self.reports_shutter and self.feedback_ok):
            return None
        return bool(self._since(t0, "shutter"))

    def _shot_hint(self, released: bool | None, focus_locked: bool | None, af: bool, t0: float) -> str | None:
        if released is not False:
            return None
        if focus_locked is False:
            return ("Camera did not release: focus never locked — remove the lens cap, add light, or switch the camera "
                    "to MF/DMF (af=False does not help in AF-S/AF-C: the camera still refuses to fire without focus).")
        return ("Camera reported no shutter activity — check drive mode/self-timer, card space, and that the camera is "
                "not in playback or a menu.")

    async def _settle_shutter(self, t0: float) -> None:
        if not (self.reports_shutter and self.feedback_ok):
            return
        if await self._poll(lambda: bool(self._since(t0, "shutter")), 1.5):
            await self._poll(lambda: self._last(t0, "shutter") is False, 0.5)

    def _focus_locked(self, t0: float) -> bool | None:
        if not (self.reports_focus and self.feedback_ok):
            return None
        return any(e.value is True for e in self._since(t0, "focus"))

    # -- public commands --------------------------------------------------------------
    async def shoot(self, af: bool = True, hold: float | None = None, *, burst: float | None = None) -> ShotResult:
        """Take a picture. `burst` = seconds to hold the release (drive mode must be Continuous)."""
        self.require(SHOOT)
        if burst is not None:
            hold = seconds_arg(burst, "burst", 60)
        hold = self.single_hold if hold is None else seconds_arg(hold, "hold", 60)
        self._need_client()
        t0 = time.monotonic()
        await self._shutter(af=af, hold=hold)
        await self._settle_shutter(t0)
        return self._shot_result(t0, af, hold)

    async def bulb(self, seconds: float) -> ShotResult:
        """Hold the release for `seconds` (camera in Bulb mode)."""
        self.require(BULB)
        seconds = seconds_arg(seconds, "bulb", 3600)
        self._need_client()
        t0 = time.monotonic()
        await self._shutter(af=False, hold=seconds)
        await self._settle_shutter(t0)
        return self._shot_result(t0, False, seconds)

    def _shot_result(self, t0: float, af: bool, hold: float) -> ShotResult:
        released = self._released(t0)
        focus_locked = self._focus_locked(t0)
        evs = self._since(t0)
        return ShotResult(self.brand, self.address, released, focus_locked, af, hold,
                          self.reports_shutter and self.feedback_ok,
                          any(e.name == "shutter" and e.value is True for e in evs),
                          [e.to_dict(t0) for e in evs], self._shot_hint(released, focus_locked, af, t0), dict(self.status))

    async def record(self, action: str = "toggle") -> RecordResult:
        """Start/stop/toggle movie recording and report the state if the camera tells us."""
        self.require(RECORD)
        if action not in ("toggle", "start", "stop"):
            raise ValueError(f"record action must be toggle, start or stop, got {action!r}.")
        self._need_client()
        want = None if action == "toggle" else action == "start"
        known = self.status.get("recording")
        if want is not None and known is want:
            return RecordResult(self.brand, self.address, action, known, True, False, [], None, dict(self.status))
        t0 = time.monotonic()
        state = await self._press_record(t0, want)
        if want is not None and self.reports_recording and state is (not want):
            # State was unknown and the toggle went the wrong way (it was already in the requested state): undo.
            t0b = time.monotonic()
            state = await self._press_record(t0b, want)
        evs = self._since(t0)
        reported = state is not None
        hint = None
        if not reported:
            hint = ("Camera did not report recording; is the Photo/Movie switch on Movie and is there card space?"
                    if self.reports_recording else
                    f"The {self.label} does not report recording state over Bluetooth; check the camera.")
        return RecordResult(self.brand, self.address, action, state, reported, True, [e.to_dict(t0) for e in evs],
                            hint, dict(self.status))

    async def _press_record(self, t0: float, want: bool | None) -> bool | None:
        known = await self._record(want)
        if not self.reports_recording:
            return known
        await self._poll(lambda: bool(self._since(t0, "recording")), 2.0)
        return self._last(t0, "recording")

    async def record_toggle(self) -> bool | None:
        """Compatibility with the old BleRemote: toggle, return the reported state (None = not reported)."""
        return (await self.record("toggle")).recording

    async def autofocus(self, seconds: float = 0.5) -> dict[str, Any]:
        """Half-press for `seconds`; reports whether focus locked when the camera says so."""
        self.require(AUTOFOCUS)
        seconds = seconds_arg(seconds, "autofocus", 30)
        self._need_client()
        t0 = time.monotonic()
        await self._autofocus(seconds)
        return {"brand": self.brand, "address": self.address, "focus_locked": self._focus_locked(t0),
                "events": [e.to_dict(t0) for e in self._since(t0)], "status": dict(self.status)}

    async def zoom(self, direction: str, seconds: float, speed: int = 0x20) -> None:
        raise NotSupported(self._unsupported(ZOOM))

    async def focus(self, direction: str, seconds: float, speed: int = 0x20) -> None:
        raise NotSupported(self._unsupported(FOCUS))

    async def af_on(self, seconds: float = 0.5) -> None:
        raise NotSupported(self._unsupported(AF_ON))

    async def c1(self) -> None:
        raise NotSupported(self._unsupported(C1))

    # -- description ------------------------------------------------------------------
    @classmethod
    def describe(cls) -> dict[str, Any]:
        caps = [c for c in CAPABILITIES if c in cls.capabilities]
        return {"brand": cls.brand, "label": cls.label, "capabilities": caps,
                "verified": [c for c in caps if c in cls.verified],
                "experimental": [c for c in caps if c in cls.experimental],
                "feedback": {"shutter": cls.reports_shutter, "focus": cls.reports_focus,
                             "recording": cls.reports_recording},
                "needs_bond": cls.needs_bond, "setup": cls.setup_hint, "pairing": cls.pair_hint,
                "supported": not cls.unsupported_reason, "reason": cls.unsupported_reason or None}

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        """Scan hook: (pairing_mode, extra) if the advertisement belongs to this brand, else None."""
        return None

    @classmethod
    def matches_device(cls, found: Any, device: Any) -> bool:
        """Is scan result `found` the remembered `device` (used when the address rotates)?"""
        return str(found.address).lower() == str(device.address).lower()
