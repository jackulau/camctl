"""Sony — the protocol Sony's RMT-P1BT / GP-VPT2BT grips speak. VERIFIED on an A7 V (ILCE-7M5), macOS + bleak.

Scope is limited BY THE CAMERA: shutter half/full, record, AF-ON, C1, zoom, manual focus. Settings cannot be read
or changed over Bluetooth — use USB-C / Wi-Fi (ptp.py) for that.

Camera setup (A7 V):
  Menu → Network → Bluetooth → Bluetooth Function: On; Bluetooth Rmt Ctrl: On
  First connection: Menu → Network → Bluetooth → Pairing, then pair from camctl and PRESS OK on the camera.
macOS: the terminal app running camctl needs Bluetooth permission (System Settings → Privacy).

Protocol (community reverse-engineering: gregleeds.com, coral/freemote; confirmed 2026-09-26):
  service 8000FF00-FF00-FFFF-FFFF-FFFFFFFFFFFF (also CC00, DD00, EE00, BB00 services, unused here)
    FF01 write (write-with-response only) — 2-byte (buttons) or 3-byte (zoom/focus + step) commands
    FF02 notify — 02 3F xx focus, 02 A0 xx shutter, 02 D5 xx recording (xx = 20 active, 00 idle)
  advertising: company id 0x012D; camera payload starts 03 00 (device type camera), then protocol version,
    reserved, 2-byte model code, then tag 0x22 + flags (0x40 = pairing open), e.g. 03006500553122ffc0...
    Sony headphones/speakers share 0x012D with other device types and are filtered out.

Verified behaviour:
- FF01 writes fail with 'Insufficient Encryption' until macOS completes bonding (~10 s, retry every 3 s).
  Subscribing to FF02 does NOT trigger bonding; the FF01 write does.
- The camera then shows a confirmation; without OK it discards the bond at disconnect (→ CBError 14 forever).
- Shot = 0107 focus_down, 0109 shutter_down, 0108 shutter_up, 0106 focus_up. In Continuous drive a 0.1 s hold gave
  2 frames, so the single-shot hold is 0.03 s. In AF-S/AF-C without focus lock the camera silently ignores it.
- Record toggle gives no notification when the Photo/Movie switch is on Photo or the card is full.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .base import AF_ON, AUTOFOCUS, BULB, C1, FOCUS, RECORD, SHOOT, ZOOM, Remote, seconds_arg
from .errors import ConnectFailed

REMOTE_SERVICE = "8000ff00-ff00-ffff-ffff-ffffffffffff"
SONY_COMPANY_ID = 0x012D
CAMERA_DEVICE_TYPE = b"\x03\x00"          # first 2 bytes of Sony manufacturer data on cameras

CMD = {
    "focus_up": b"\x01\x06", "focus_down": b"\x01\x07",
    "shutter_up": b"\x01\x08", "shutter_down": b"\x01\x09",
    "record_up": b"\x01\x0e", "record_down": b"\x01\x0f",
    "afon_up": b"\x01\x14", "afon_down": b"\x01\x15",
    "c1_up": b"\x01\x20", "c1_down": b"\x01\x21",
}

STATUS = {
    (0x3F, 0x00): ("focus", False), (0x3F, 0x20): ("focus", True),
    (0xA0, 0x00): ("shutter", False), (0xA0, 0x20): ("shutter", True),
    (0xD5, 0x00): ("recording", False), (0xD5, 0x20): ("recording", True),
}


def zoom_cmd(direction: str, speed: int, press: bool) -> bytes:
    """direction tele|wide, speed 1..127 (0x10-0x8F on the wire for press)."""
    if direction not in ("tele", "wide"):
        raise ValueError(f"zoom direction must be tele or wide, got {direction!r}.")
    base = {"tele": 0x44, "wide": 0x46}[direction] + (1 if press else 0)
    val = (0x10 + min(max(speed, 1), 0x7F)) & 0xFF if press else 0x00
    return bytes([0x02, base, val])


def focus_cmd(direction: str, speed: int, press: bool) -> bytes:
    """direction in (near, 0x6B press / 0x6A release) | out (far, 0x6D / 0x6C), speed 1..127 (freemote)."""
    if direction not in ("in", "out"):
        raise ValueError(f"focus direction must be in (near) or out (far), got {direction!r}.")
    base = {"in": 0x6A, "out": 0x6C}[direction] + (1 if press else 0)
    val = min(max(speed, 1), 0x7F) if press else 0x00
    return bytes([0x02, base, val])


def parse_status(data: bytes) -> tuple[str, bool] | None:
    if len(data) >= 3 and data[0] == 0x02:
        return STATUS.get((data[1], data[2]))
    return None


def _pairing_flag(mfg: bytes) -> bool | None:
    # Tag 0x22 followed by a flags byte: 0x40 bit = pairing enabled (per coral/freemote notes).
    # Tags start after type(2) + protocol version(1) + reserved(1) + model code(2).
    i = mfg.find(b"\x22", 6)
    if i >= 0 and i + 1 < len(mfg):
        return bool(mfg[i + 1] & 0x40)
    return None


class SonyRemote(Remote):
    brand = "sony"
    label = "Sony camera"
    default_name = "Sony"
    capabilities = frozenset({SHOOT, AUTOFOCUS, FOCUS, RECORD, ZOOM, BULB, AF_ON, C1})
    verified = frozenset({SHOOT, RECORD})
    reports_shutter = reports_focus = reports_recording = True
    needs_bond = True
    single_hold = 0.03
    af_wait = 1.5          # max wait for the focus-lock notification
    setup_hint = "turn on MENU → Network → Bluetooth → Bluetooth Function and Bluetooth Rmt Ctrl"
    pair_hint = "On the camera: MENU → Network → Bluetooth → Pairing; press OK when the camera asks."

    def __init__(self, address: str, **kw: Any) -> None:
        super().__init__(address, **kw)
        self._cmd_char: Any = None
        self._notify_char: Any = None
        self._notify_ok = False
        self._response = True

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        mfg = info.mfg.get(SONY_COMPANY_ID)
        if mfg is None or not bytes(mfg).startswith(CAMERA_DEVICE_TYPE):
            return None
        return _pairing_flag(bytes(mfg)), {}

    @property
    def feedback_ok(self) -> bool:
        return self._notify_ok

    async def _setup(self) -> None:
        client = self._need_client()
        svc = client.services.get_service(REMOTE_SERVICE)
        if svc is None:
            raise ConnectFailed("The camera has no Bluetooth remote service; turn on MENU → Network → Bluetooth → "
                                "Bluetooth Rmt Ctrl.")
        chars = {str(c.uuid)[4:8].lower(): c for c in svc.characteristics}
        self._cmd_char = chars.get("ff01")
        self._notify_char = chars.get("ff02")
        if self._cmd_char is None:
            raise ConnectFailed("The camera's remote service lacks command characteristic FF01; update the camera "
                                "firmware or turn Bluetooth Rmt Ctrl off and on.")
        # FF01 is write-only on the A7 V → write with response; fall back to write-command if that's all it has.
        self._response = "write" in getattr(self._cmd_char, "properties", ["write"])
        self._notify_ok = False
        await self._subscribe_status()

    async def _subscribe_status(self) -> None:
        if self._notify_char is not None and not self._notify_ok:
            self._notify_ok = await self._subscribe(self._notify_char, self._on_notify)

    def _on_notify(self, _char: Any, data: bytearray) -> None:
        st = parse_status(bytes(data))
        if st:
            self._event(st[0], st[1], data)
        else:
            self._event("notify", None, data)

    async def send(self, payload: bytes, gap: float = 0.05) -> None:
        """Write one FF01 command (retrying while the bond is being set up), then wait `gap` s."""
        await self._write(self._cmd_char, payload, response=self._response)
        if not self._notify_ok:      # subscription can fail before the link is encrypted; retry once bonded
            await self._subscribe_status()
        if gap:
            await asyncio.sleep(gap)

    async def _release(self, *payloads: bytes) -> None:
        await self._best_effort(self._cmd_char, *payloads, response=self._response)

    async def press(self, down: str, up: str, hold: float = 0.1) -> None:
        """Press and release a named button from CMD (e.g. 'afon_down', 'afon_up')."""
        await self._hold(lambda: self.send(CMD[down]), lambda: self._release(CMD[up]), hold)

    async def _shutter(self, af: bool, hold: float) -> None:
        """Half-press (AF), full-press, release — the sequence Sony remotes send."""
        t0 = time.monotonic()
        try:
            await self.send(CMD["focus_down"])
            if af and self._notify_ok:          # wait for focus lock (02 3F 20); the camera won't fire without it
                await self._poll(lambda: self._last(t0, "focus") is True, self.af_wait)
            await self.send(CMD["shutter_down"], gap=0)
            await asyncio.sleep(hold)
        finally:
            await self._release(CMD["shutter_up"], CMD["focus_up"])

    async def _after_pair(self) -> list[str]:
        await self._probe()     # the first FF01 write is what makes macOS bond (retries until encrypted)
        self.prompt("Press OK on the camera now to save the pairing — without OK the camera forgets it at disconnect.")
        await asyncio.sleep(self.confirm_wait)
        return ["bonded; OK must be pressed on the camera or the bond is discarded at disconnect"]

    async def _probe(self) -> None:
        await self.send(CMD["focus_up"], gap=0)    # releasing an unpressed half-press is harmless

    async def _record(self, want: bool | None) -> bool | None:
        await self.press("record_down", "record_up")
        return None

    async def _autofocus(self, seconds: float) -> None:
        await self._hold(lambda: self.send(CMD["focus_down"]), lambda: self._release(CMD["focus_up"]), seconds)

    async def zoom(self, direction: str, seconds: float, speed: int = 0x20) -> None:
        seconds = seconds_arg(seconds, "zoom", 60)
        press, release = zoom_cmd(direction, speed, True), zoom_cmd(direction, 0, False)
        await self._hold(lambda: self.send(press), lambda: self._release(release), seconds)

    async def focus(self, direction: str, seconds: float, speed: int = 0x20) -> None:
        seconds = seconds_arg(seconds, "focus", 60)
        press, release = focus_cmd(direction, speed, True), focus_cmd(direction, 0, False)
        await self._hold(lambda: self.send(press), lambda: self._release(release), seconds)

    async def af_on(self, seconds: float = 0.5) -> None:
        await self.press("afon_down", "afon_up", seconds_arg(seconds, "af_on", 60))

    async def c1(self) -> None:
        await self.press("c1_down", "c1_up")


BleRemote = SonyRemote     # the pre-multi-brand name
