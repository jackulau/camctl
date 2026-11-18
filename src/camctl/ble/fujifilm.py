"""Fujifilm — community-confirmed via Furble; not yet tested with camctl on hardware.

Protocol from Furble (MIT, github.com/gkoh/furble, commit 0cac22a). Company id 0x04D8.

Secure pairing (firmware from ~2025-07; advertises a9d2b304-e8d6-4902-8336-352b772d7597, manufacturer data carries
a 5-byte serial), repeated on every connection:
  1. bond; read 4 bytes from f557d96b-8284-4667-8793-b971c1deca2a (svc 123d8f06-62a1-4935-9322-833c531ee225) and
     write back [s0, s1, s2, 0x20]
  2. write the ASCII remote name to 85b9163e-62d1-49ff-a6f5-054b4630d4a1
  3. subscribe svc 4c0020fe-f3b6-40de-acc9-77d129067b14: indications a68e3f66…, bd17ba04…; notifications
     f9150137…, ad06c7b7…, e6692c5c… (the last one's full UUID was never captured → 8-hex-digit prefix match)
  4. subscribe svc 4e941240-d01d-46b9-a5ea-67636806830b notifications bf6dc9cf, 75823784, aab609c4, 2a125640,
     82a9f452, deef7187 (prefixes) and c95d91ae-b247-4d6d-8661-7dd5d6a0f85b, then write uint16 LE 10 to c95d91ae….
Basic pairing (older firmware, no bonding): write the 4-byte token from the pairing advert (manufacturer data
  02 t0 t1 t2 t3) to aba356eb-9633-4e60-b73f-f52516dbd671 (svc 91f1de68-dff6-466e-8b65-ff13b0f16fb8), then the name.
Shutter (both): svc 6514eb81-4e8f-458d-aa2a-e691336cdfac char 7fcf49c6-4ff0-4777-a03d-1a79166af7a8, always two
  writes: 01 00, then press 02 00 / release 00 00 / focus 03 00. No record over BLE. Bulb (holding 02 00) is
  experimental.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from .base import AUTOFOCUS, BULB, SHOOT, Remote
from .errors import PairingFailed, PairingRequired

COMPANY_ID = 0x04D8
SECURE_ADV_SERVICE = "a9d2b304-e8d6-4902-8336-352b772d7597"
SECURE_PAIR_SERVICE = "123d8f06-62a1-4935-9322-833c531ee225"
SECURE_PAIR_CHAR = "f557d96b-8284-4667-8793-b971c1deca2a"
IDENTIFIER_CHAR = "85b9163e-62d1-49ff-a6f5-054b4630d4a1"
BASIC_PAIR_SERVICE = "91f1de68-dff6-466e-8b65-ff13b0f16fb8"
BASIC_PAIR_CHAR = "aba356eb-9633-4e60-b73f-f52516dbd671"
CONF_SERVICE = "4c0020fe-f3b6-40de-acc9-77d129067b14"
CONF_CHARS = ("a68e3f66-0fcc-4395-8d4c-aa980b5877fa", "bd17ba04-b76b-4892-a545-b73ba1f74dae",
              "f9150137-5d40-4801-a8dc-f7fc5b01da50", "ad06c7b7-f41a-46f4-a29a-712055319122", "e6692c5c")
GEO_SERVICE = "4e941240-d01d-46b9-a5ea-67636806830b"
GEO_INTERVAL_CHAR = "c95d91ae-b247-4d6d-8661-7dd5d6a0f85b"
GEO_CHARS = ("bf6dc9cf", "75823784", "aab609c4", "2a125640", "82a9f452", "deef7187", GEO_INTERVAL_CHAR)
SHUTTER_SERVICE = "6514eb81-4e8f-458d-aa2a-e691336cdfac"
SHUTTER_CHAR = "7fcf49c6-4ff0-4777-a03d-1a79166af7a8"
PREFIX = b"\x01\x00"
PRESS, RELEASE, FOCUS = b"\x02\x00", b"\x00\x00", b"\x03\x00"


class FujifilmRemote(Remote):
    brand = "fujifilm"
    label = "Fujifilm camera"
    default_name = "FUJIFILM"
    capabilities = frozenset({SHOOT, AUTOFOCUS, BULB})
    experimental = frozenset({BULB})
    needs_bond = True     # secure firmware; basic firmware uses the advert token instead
    setup_hint = "enable Bluetooth in the camera's Network/USB settings"
    pair_hint = "On the camera: Network/USB settings → Bluetooth settings → Pairing registration."

    def __init__(self, address: str, **kw: Any) -> None:
        super().__init__(address, **kw)
        self._shutter_char: Any = None

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        mfg = info.mfg.get(COMPANY_ID)
        secure = SECURE_ADV_SERVICE in info.uuids
        if mfg is None and not secure:
            return None
        mfg = bytes(mfg or b"")
        if secure:
            return True, {"secure": True, **({"serial": mfg[-5:].hex()} if len(mfg) >= 5 else {})}
        if len(mfg) == 5 and mfg[0] == 0x02:
            return True, {"secure": False, "token": mfg[1:5].hex()}
        return None, {}

    @property
    def secure(self) -> bool:
        return bool(self.extra.get("secure", "token" not in self.extra))

    async def _setup(self) -> None:
        if self.secure:
            await self._secure_handshake()
        else:
            await self._basic_handshake()
        self._shutter_char = self._find_char(SHUTTER_CHAR, SHUTTER_SERVICE)

    async def _secure_handshake(self) -> None:
        client = self._need_client()
        pair = getattr(client, "pair", None)
        if pair is not None:      # BlueZ/WinRT bond explicitly; Core Bluetooth bonds on first encrypted access
            try:
                await pair()
            except Exception:  # noqa: BLE001 — NotImplementedError on macOS, "already paired" elsewhere
                pass
        char = self._find_char(SECURE_PAIR_CHAR, SECURE_PAIR_SERVICE)
        s = bytes(await self._gatt(lambda: client.read_gatt_char(char)) or b"")
        if len(s) < 3:
            raise PairingFailed(f"The Fujifilm camera sent a {len(s)}-byte pairing challenge (expected 4); restart "
                                f"pairing registration on the camera and retry.")
        await self._write(char, bytes([s[0], s[1], s[2], 0x20]))
        await self._write(self._find_char(IDENTIFIER_CHAR), self.remote_name.encode("ascii", "replace"))
        for uuid in CONF_CHARS:
            c = self._find_char(uuid, CONF_SERVICE, required=False)
            if c is not None:
                await self._subscribe(c, self._on_notify)
        for uuid in GEO_CHARS:
            c = self._find_char(uuid, GEO_SERVICE, required=False)
            if c is not None:
                await self._subscribe(c, self._on_notify)
        await self._write(self._find_char(GEO_INTERVAL_CHAR, GEO_SERVICE), struct.pack("<H", 10))

    async def _basic_handshake(self) -> None:
        token = self.extra.get("token")
        if not token:
            raise PairingRequired("This Fujifilm camera needs its pairing token: start Pairing registration on the "
                                  "camera and pair again while it advertises.")
        await self._write(self._find_char(BASIC_PAIR_CHAR, BASIC_PAIR_SERVICE), bytes.fromhex(token))
        await self._write(self._find_char(IDENTIFIER_CHAR), self.remote_name.encode("ascii", "replace"))

    def _on_notify(self, char: Any, data: bytearray) -> None:
        self._event("notify", None, data, char=str(getattr(char, "uuid", char)))

    async def _cmd(self, param: bytes) -> None:
        await self._write(self._shutter_char, PREFIX)
        await self._write(self._shutter_char, param)

    async def _idle(self) -> None:
        await self._best_effort(self._shutter_char, PREFIX, RELEASE)

    async def _shutter(self, af: bool, hold: float) -> None:
        try:
            if af:
                await self._cmd(FOCUS)
                await asyncio.sleep(self.af_wait)
            await self._cmd(PRESS)
            await asyncio.sleep(hold)
        finally:
            await self._idle()

    async def _autofocus(self, seconds: float) -> None:
        await self._hold(lambda: self._cmd(FOCUS), self._idle, seconds)
