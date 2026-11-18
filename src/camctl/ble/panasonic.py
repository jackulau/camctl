"""Panasonic LUMIX — EXPERIMENTAL: command bytes come from a decompiled LUMIX Sync app, not from a confirmed remote.

No SMP bonding. The camera is matched by name (e.g. "S5-XXXXXX"; the address rotates) or by its pairing service.
  svc 054ac620-3214-11e6-ac0d-0002a5d5c51b:
    write 16 bytes "MEI0…" 4d45493001001000800102000003 0b54 to 8d08a420-3213-11e6-8aca-0002a5d5c51b,
    then the remote name to cd7a71a0-3213-11e6-8f56-0002a5d5c51b
  remote svc 7be5faae-475b-11e7-a919-92ebcb67fe33:
    write 01 to 7be5fd56-…, wait for notification 00 on 7be5fe6e-…, write 02;
    shutter press/release 04/05, record press/release 06/07;
    notification error codes: 01 can't wake the camera, 02 camera off, 03 busy auto-sending images.
Protocol notes collected alongside Furble (MIT, github.com/gkoh/furble, commit 0cac22a).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .base import RECORD, SHOOT, Remote
from .errors import ConnectFailed

PAIR_SERVICE = "054ac620-3214-11e6-ac0d-0002a5d5c51b"
MEI_CHAR = "8d08a420-3213-11e6-8aca-0002a5d5c51b"
NAME_CHAR = "cd7a71a0-3213-11e6-8f56-0002a5d5c51b"
REMOTE_SERVICE = "7be5faae-475b-11e7-a919-92ebcb67fe33"
REMOTE_CTRL = "7be5fd56-475b-11e7-a919-92ebcb67fe33"
REMOTE_NOTIFY = "7be5fe6e-475b-11e7-a919-92ebcb67fe33"
MEI0 = bytes.fromhex("4d454930010010008001020000030b54")
WAKE, READY = b"\x01", b"\x02"
SHUTTER_ON, SHUTTER_OFF, RECORD_ON, RECORD_OFF = b"\x04", b"\x05", b"\x06", b"\x07"
ERRORS = {1: "the camera could not be woken up; turn it on and retry",
          2: "the camera is switched off; turn it on and retry",
          3: "the camera is busy auto-sending images; wait for the transfer to finish or disable auto transfer"}
NAME_RE = re.compile(r"^(DC-)?(GH|GX|GF|G|S|BGH|BS|FZ|TZ|ZS|LX|FT|TS)[0-9A-Z]{0,6}-[0-9A-F]{4,8}$", re.I)


class PanasonicRemote(Remote):
    brand = "panasonic"
    label = "Panasonic LUMIX camera"
    default_name = "LUMIX"
    capabilities = frozenset({SHOOT, RECORD})
    experimental = frozenset({SHOOT, RECORD})
    rotating_address = True
    stage_timeout = 10.0
    setup_hint = "enable Bluetooth on the camera (Setup → Bluetooth → On)"
    pair_hint = "On the camera: Setup → Bluetooth → Bluetooth → SET → Pairing."

    def __init__(self, address: str, **kw: Any) -> None:
        super().__init__(address, **kw)
        self._ctrl: Any = None
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        if PAIR_SERVICE in info.uuids or REMOTE_SERVICE in info.uuids:
            return (True if PAIR_SERVICE in info.uuids else None), {}
        if info.name and NAME_RE.match(info.name):
            return None, {}
        return None

    @classmethod
    def matches_device(cls, found: Any, device: Any) -> bool:
        return bool(device.name) and found.name == device.name

    async def _setup(self) -> None:
        self._replies = asyncio.Queue()
        await self._write(self._find_char(MEI_CHAR, PAIR_SERVICE), MEI0)
        await self._write(self._find_char(NAME_CHAR, PAIR_SERVICE), self.remote_name.encode("utf-8", "replace"))
        self._ctrl = self._find_char(REMOTE_CTRL, REMOTE_SERVICE)
        await self._subscribe_required(self._find_char(REMOTE_NOTIFY, REMOTE_SERVICE), self._on_notify)
        await self._write(self._ctrl, WAKE)
        try:
            code = await asyncio.wait_for(self._replies.get(), self.stage_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise ConnectFailed(f"The LUMIX camera did not enter remote mode within {self.stage_timeout:g} s; enable "
                                f"its Bluetooth remote and retry.") from None
        if code[:1] != b"\x00":
            raise ConnectFailed(f"LUMIX remote mode refused: {ERRORS.get(code[0], f'error code {code.hex()}')}.")
        await self._write(self._ctrl, READY)

    def _on_notify(self, _char: Any, data: bytearray) -> None:
        data = bytes(data)
        self._replies.put_nowait(data)
        if data and data[0] in ERRORS:
            self._event("error", ERRORS[data[0]], data, code=data[0])
        else:
            self._event("notify", None, data)

    def _shot_hint(self, released: bool | None, focus_locked: bool | None, af: bool, t0: float) -> str | None:
        errs = self._since(t0, "error")
        return f"The camera reported: {errs[-1].value}." if errs else None

    async def _press(self, press: bytes, release: bytes, seconds: float) -> None:
        await self._hold(lambda: self._write(self._ctrl, press), lambda: self._best_effort(self._ctrl, release),
                         seconds)

    async def _shutter(self, af: bool, hold: float) -> None:
        await self._press(SHUTTER_ON, SHUTTER_OFF, hold)

    async def _record(self, want: bool | None) -> bool | None:
        await self._press(RECORD_ON, RECORD_OFF, 0.1)
        return None
