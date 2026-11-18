"""GoPro — Open GoPro BLE spec (Apache-2.0, gopro.github.io/OpenGoPro); documented by GoPro, not yet tested with
camctl on hardware. Listed alongside the Furble-derived drivers (MIT, github.com/gkoh/furble, commit 0cac22a).

  advertises service FEA6; bonding required (put the camera in pairing mode the first time:
  Preferences → Wireless Connections → Connect Device → GoPro Quik app).
  command  b5f90072-aa8d-11e3-9046-0002a5d5c51b, responses notify on b5f90073-…: [len, cmd id, status]
  shutter  03 01 01 01 (on) / 03 01 01 00 (off). Photo mode: "on" takes the picture. Video mode: on/off = record.
  keep-alive 03 5B 01 42 to the settings characteristic b5f90074-… every ~3 s, or the camera may sleep/drop BLE.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .base import RECORD, SHOOT, Remote

log = logging.getLogger("camctl.ble")

SERVICE = "0000fea6-0000-1000-8000-00805f9b34fb"
COMMAND_CHAR = "b5f90072-aa8d-11e3-9046-0002a5d5c51b"
RESPONSE_CHAR = "b5f90073-aa8d-11e3-9046-0002a5d5c51b"
SETTINGS_CHAR = "b5f90074-aa8d-11e3-9046-0002a5d5c51b"
SHUTTER_ON = bytes.fromhex("03010101")
SHUTTER_OFF = bytes.fromhex("03010100")
KEEPALIVE = bytes.fromhex("035b0142")
CMD_SHUTTER = 0x01


def parse_response(data: bytes) -> tuple[int, int] | None:
    """(command id, status) from a single-packet Open GoPro response (general 5-bit length header)."""
    if len(data) >= 3 and not data[0] & 0x80 and not data[0] & 0x60 and (data[0] & 0x1F) >= 2:
        return data[1], data[2]
    return None


class GoProRemote(Remote):
    brand = "gopro"
    label = "GoPro"
    default_name = "GoPro"
    capabilities = frozenset({SHOOT, RECORD})
    reports_shutter = True     # command acknowledgements (status 0 = accepted)
    needs_bond = True
    keepalive_interval = 3.0
    response_timeout = 3.0
    setup_hint = "wake the GoPro and keep it within range"
    pair_hint = "On the GoPro: Preferences → Wireless Connections → Connect Device → GoPro Quik app."

    def __init__(self, address: str, **kw: Any) -> None:
        super().__init__(address, **kw)
        self._cmd: Any = None
        self._settings: Any = None
        self._ka_task: asyncio.Task[None] | None = None

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        return (None, {}) if SERVICE in info.uuids else None

    async def _setup(self) -> None:
        self._cmd = self._find_char(COMMAND_CHAR, SERVICE)
        self._settings = self._find_char(SETTINGS_CHAR, SERVICE, required=False)
        await self._subscribe_required(self._find_char(RESPONSE_CHAR, SERVICE), self._on_response)
        if self._settings is not None:
            self._ka_task = asyncio.create_task(self._keepalive_loop())

    async def _teardown(self) -> None:
        task, self._ka_task = self._ka_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _keepalive_loop(self) -> None:
        while self.connected:
            await asyncio.sleep(self.keepalive_interval)
            try:
                await self.keepalive()
            except Exception:  # noqa: BLE001 — the daemon/next command notices a dead link
                log.debug("GoPro keep-alive failed", exc_info=True)
                return

    async def keepalive(self) -> bool:
        if not self.connected:
            return False
        if self._settings is not None:
            await self._write(self._settings, KEEPALIVE, retry=False)
        return True

    def _on_response(self, _char: Any, data: bytearray) -> None:
        r = parse_response(bytes(data))
        if r is None:
            self._event("notify", None, data)
        else:
            self._event("response", r[1] == 0, data, command=r[0], code=r[1])

    async def _command(self, payload: bytes) -> int | None:
        """Send a command, return its response status (0 = OK) or None if the camera did not answer."""
        t0 = time.monotonic()
        await self._write(self._cmd, payload)
        await self._poll(lambda: bool(self._responses(t0)), self.response_timeout)
        r = self._responses(t0)
        return r[-1].detail["code"] if r else None

    def _responses(self, t0: float) -> list[Any]:
        return [e for e in self._since(t0, "response") if e.detail.get("command") == CMD_SHUTTER]

    @property
    def feedback_ok(self) -> bool:
        return True

    def _released(self, t0: float) -> bool | None:
        r = self._responses(t0)
        return (r[-1].detail["code"] == 0) if r else None

    def _shot_hint(self, released: bool | None, focus_locked: bool | None, af: bool, t0: float) -> str | None:
        if released is None:
            return "The GoPro did not acknowledge the shutter command; wake it and retry."
        if released is False:
            code = self._responses(t0)[-1].detail["code"]
            return (f"The GoPro rejected the shutter command (status {code}); it may be busy, in a menu, or out of "
                    f"card space.")
        return None

    async def _settle_shutter(self, t0: float) -> None:
        return None     # _command already waited for the acknowledgement

    async def _shutter(self, af: bool, hold: float) -> None:
        await self._command(SHUTTER_ON)     # photo mode: "on" takes the picture (never send "off": it stops video)

    async def _record(self, want: bool | None) -> bool | None:
        start = (not self.status.get("recording")) if want is None else want
        code = await self._command(SHUTTER_ON if start else SHUTTER_OFF)
        if code == 0:
            self.status["recording"] = start
            return start
        return None
