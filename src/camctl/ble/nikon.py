"""Nikon — ML-L7 remote emulation. Shutter community-confirmed (Furble: COOLPIX B600, Z6 III); not yet tested with
camctl on hardware. Record is experimental.

Protocol from Furble (MIT, github.com/gkoh/furble, commit 0cac22a). No SMP bonding: an application handshake runs
on every connection.
  service 0000de00-3dd4-4255-8d62-6dc7b9bd5561 (advertised while pairing, no manufacturer data)
  1. subscribe indications on 00002084-…, then on the pairing char 00002087-…
  2. write 17 bytes: 01 | 00 00 00 00 00 00 00 01 | dev[4] (random, dev[0] = 0x01) | nonce[4]
  3. expect indication 02 + 16×00; write 03 + 16×00
  4. expect indication 04 + 8×00 + 8-byte ASCII serial. Each stage times out after 10 s.
  The dev id is persisted; a paired camera then advertises manufacturer data 99 03 | dev[4] | 00 (company 0x0399),
  which is how it is found again (its address may change).
  shutter: 00002083-…: 02 02 press, 02 00 release. Movie mode 0x03 → record press 03 02 / release 03 00
  (experimental guess).
Camera menu: "Connection to remote (ML-L7)" → pairing.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .base import BULB, RECORD, SHOOT, Remote
from .errors import PairingFailed, PairingRequired

SERVICE = "0000de00-3dd4-4255-8d62-6dc7b9bd5561"
SHUTTER_CHAR = "00002083-3dd4-4255-8d62-6dc7b9bd5561"
INDICATE_CHAR = "00002084-3dd4-4255-8d62-6dc7b9bd5561"
PAIR_CHAR = "00002087-3dd4-4255-8d62-6dc7b9bd5561"
COMPANY_ID = 0x0399
SHUTTER_PRESS, SHUTTER_RELEASE = b"\x02\x02", b"\x02\x00"
RECORD_PRESS, RECORD_RELEASE = b"\x03\x02", b"\x03\x00"


def new_dev_id() -> bytes:
    return b"\x01" + os.urandom(3)


def hello(dev_id: bytes, nonce: bytes) -> bytes:
    """Stage-1 pairing write: 01 | 00×7 01 | dev[4] | nonce[4] (17 bytes)."""
    if len(dev_id) != 4 or len(nonce) != 4:
        raise ValueError("Nikon dev id and nonce must be 4 bytes each.")
    return b"\x01" + b"\x00" * 7 + b"\x01" + dev_id + nonce


STAGE2 = b"\x03" + b"\x00" * 16


class NikonRemote(Remote):
    brand = "nikon"
    label = "Nikon camera"
    default_name = "Nikon"
    capabilities = frozenset({SHOOT, RECORD, BULB})
    experimental = frozenset({RECORD, BULB})
    rotating_address = True
    stage_timeout = 10.0
    setup_hint = "enable Connection to remote (ML-L7) on the camera"
    pair_hint = "On the camera: Network menu → Connection to remote (ML-L7) → pair."

    def __init__(self, address: str, **kw: Any) -> None:
        super().__init__(address, **kw)
        self._shutter_char: Any = None
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        if SERVICE in info.uuids:
            return True, {}
        mfg = info.mfg.get(COMPANY_ID)
        if mfg is not None and len(mfg) >= 4:
            return False, {"dev_id": bytes(mfg[:4]).hex()}
        return None

    @classmethod
    def matches_device(cls, found: Any, device: Any) -> bool:
        dev = (device.extra or {}).get("dev_id")
        return bool(dev) and found.extra.get("dev_id") == dev

    async def _setup(self) -> None:
        dev_hex = self.extra.get("dev_id")
        if self._pairing or not dev_hex:
            if not self._pairing:
                raise PairingRequired("This Nikon camera has not been paired with camctl yet; enable Connection to "
                                      "remote (ML-L7) pairing on the camera and pair first.")
            dev_hex = new_dev_id().hex()
        dev_id = bytes.fromhex(dev_hex)
        self._replies = asyncio.Queue()
        ind = self._find_char(INDICATE_CHAR, SERVICE)
        pair = self._find_char(PAIR_CHAR, SERVICE)
        self._shutter_char = self._find_char(SHUTTER_CHAR, SERVICE)
        await self._subscribe_required(ind, self._on_indicate)
        await self._subscribe_required(pair, self._on_pair)
        await self._write(pair, hello(dev_id, os.urandom(4)))
        r1 = await self._reply(1)
        if r1[:1] != b"\x02":
            raise PairingFailed(f"The Nikon camera rejected the pairing hello (got {r1.hex() or 'nothing'}); restart "
                                f"'Connection to remote (ML-L7)' pairing on the camera and retry.")
        await self._write(pair, STAGE2)
        r2 = await self._reply(2)
        if r2[:1] != b"\x04":
            raise PairingFailed(f"The Nikon camera did not confirm pairing (got {r2.hex() or 'nothing'}); restart "
                                f"pairing on the camera and retry.")
        self.extra["dev_id"] = dev_hex
        serial = r2[9:17].split(b"\x00")[0].decode("ascii", "replace").strip()
        if serial:
            self.extra["serial"] = serial

    async def _reply(self, stage: int) -> bytes:
        try:
            return await asyncio.wait_for(self._replies.get(), self.stage_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise PairingFailed(f"The Nikon camera did not answer pairing stage {stage} within {self.stage_timeout:g} "
                                f"s; keep 'Connection to remote (ML-L7)' open on the camera and retry.") from None

    def _on_pair(self, _char: Any, data: bytearray) -> None:
        self._replies.put_nowait(bytes(data))

    def _on_indicate(self, _char: Any, data: bytearray) -> None:
        self._event("notify", None, data)

    async def _press(self, press: bytes, release: bytes, seconds: float) -> None:
        await self._hold(lambda: self._write(self._shutter_char, press),
                         lambda: self._best_effort(self._shutter_char, release), seconds)

    async def _shutter(self, af: bool, hold: float) -> None:
        await self._press(SHUTTER_PRESS, SHUTTER_RELEASE, hold)     # ML-L7 has no half-press: the camera AFs itself

    async def _record(self, want: bool | None) -> bool | None:
        await self._press(RECORD_PRESS, RECORD_RELEASE, 0.1)
        return None
