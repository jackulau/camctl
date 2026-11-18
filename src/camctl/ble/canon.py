"""Canon — BR-E1 remote emulation. Community-confirmed (Furble: EOS M6, R6 Mark II, RP, PowerShot G9 X II, 200D);
not yet tested with camctl on hardware.

Protocol from Furble (MIT, github.com/gkoh/furble, commit 0cac22a) and canoremote:
  service 00050000-0000-1000-0000-d8492fffa821 (advertised while the camera is pairing)
    00050002 pairing: after bonding write 0x03 + ASCII remote name; the camera shows "Paired with: <name>"
             and the user presses OK.
    00050003 control, one byte, written without response:
             buttons RELEASE 0x80 | FOCUS 0x40 | TELE 0x20 | WIDE 0x10, OR'd with a mode:
             IMMEDIATE 0x0C, DELAY 0x04 (2 s timer), MOVIE 0x08.
             shutter 0x8C → 0x0C; focus 0x4C → 0x0C; movie record toggle 0x88 → 0x08. No notifications.
Camera menu: Wireless communication settings → Bluetooth function = Remote → Pairing. Set the drive mode to a
remote-capable mode; for movies set Remote control = Enable. Zoom via TELE/WIDE is experimental.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import AUTOFOCUS, RECORD, SHOOT, ZOOM, Remote, seconds_arg

SERVICE = "00050000-0000-1000-0000-d8492fffa821"
PAIR_CHAR = "00050002-0000-1000-0000-d8492fffa821"
CONTROL_CHAR = "00050003-0000-1000-0000-d8492fffa821"
COMPANY_ID = 0x01A9
RELEASE, FOCUS, TELE, WIDE = 0x80, 0x40, 0x20, 0x10
IMMEDIATE, DELAY, MOVIE = 0x0C, 0x04, 0x08
_NAME_PREFIXES = ("eos", "powershot", "canon")


def pair_payload(name: str) -> bytes:
    return b"\x03" + name.encode("ascii", "replace")[:32]


class CanonRemote(Remote):
    brand = "canon"
    label = "Canon camera"
    default_name = "Canon"
    capabilities = frozenset({SHOOT, AUTOFOCUS, RECORD, ZOOM})
    experimental = frozenset({ZOOM})
    needs_bond = True
    setup_hint = "set Wireless communication settings → Bluetooth function to Remote"
    pair_hint = ("On the camera: Wireless communication settings → Bluetooth function: Remote → Pairing; press OK "
                 "when it shows 'Paired with'.")

    def __init__(self, address: str, **kw: Any) -> None:
        super().__init__(address, **kw)
        self._ctrl: Any = None

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        if SERVICE in info.uuids:
            return True, {}
        if COMPANY_ID in info.mfg and (info.name or "").lower().startswith(_NAME_PREFIXES):
            return None, {}
        return None

    async def _setup(self) -> None:
        pair = self._find_char(PAIR_CHAR, SERVICE)
        self._ctrl = self._find_char(CONTROL_CHAR, SERVICE)
        # Writing the remote name needs an encrypted link: this is what triggers bonding on macOS. Furble repeats
        # it on every connection.
        await self._write(pair, pair_payload(self.remote_name))
        if self._pairing:
            self.prompt(f"Press OK on the camera to accept 'Paired with: {self.remote_name}'.")
            await asyncio.sleep(self.confirm_wait)
        await self._send(IMMEDIATE)

    async def _send(self, value: int) -> None:
        await self._write(self._ctrl, bytes([value]), response=self._wants_response(self._ctrl, prefer=False))

    async def _idle(self, mode: int = IMMEDIATE) -> None:
        await self._best_effort(self._ctrl, bytes([mode]), response=self._wants_response(self._ctrl, prefer=False))

    async def _shutter(self, af: bool, hold: float) -> None:
        try:
            if af:
                await self._send(FOCUS | IMMEDIATE)
                await asyncio.sleep(self.af_wait)
            await self._send(RELEASE | IMMEDIATE)
            await asyncio.sleep(hold)
        finally:
            await self._idle()

    async def _autofocus(self, seconds: float) -> None:
        await self._hold(lambda: self._send(FOCUS | IMMEDIATE), self._idle, seconds)

    async def _record(self, want: bool | None) -> bool | None:
        await self._hold(lambda: self._send(RELEASE | MOVIE), lambda: self._idle(MOVIE), 0.1)
        return None

    async def zoom(self, direction: str, seconds: float, speed: int = 0x20) -> None:
        if direction not in ("tele", "wide"):
            raise ValueError(f"zoom direction must be tele or wide, got {direction!r}.")
        seconds = seconds_arg(seconds, "zoom", 60)
        button = TELE if direction == "tele" else WIDE
        await self._hold(lambda: self._send(button | IMMEDIATE), self._idle, seconds)
