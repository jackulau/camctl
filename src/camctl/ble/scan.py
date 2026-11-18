"""Scan for cameras of every supported brand (read-only: nothing connects)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .registry import DRIVERS, normalize_brand


@dataclass
class AdvInfo:
    """Brand-neutral view of one advertisement."""

    name: str | None
    mfg: dict[int, bytes]
    uuids: set[str]
    rssi: int | None

    @classmethod
    def from_bleak(cls, dev: Any, adv: Any) -> "AdvInfo":
        mfg = {int(k): bytes(v) for k, v in (getattr(adv, "manufacturer_data", None) or {}).items()}
        uuids = {str(u).lower() for u in (getattr(adv, "service_uuids", None) or [])}
        name = getattr(adv, "local_name", None) or getattr(dev, "name", None)
        return cls(name, mfg, uuids, getattr(adv, "rssi", None))


@dataclass
class Found:
    """A camera seen while scanning. Positional order (address, name, rssi) is kept from the Sony-only API."""

    address: str
    name: str
    rssi: int | None
    pairing_mode: bool | None = None
    raw: str = ""                  # manufacturer data (hex) of the brand's company id, if any
    brand: str = "sony"
    extra: dict[str, Any] = field(default_factory=dict)   # e.g. Nikon dev_id, Fujifilm token/secure

    @property
    def supported(self) -> bool:
        cls = DRIVERS.get(self.brand)
        return cls is not None and not cls.unsupported_reason

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "supported": self.supported}


_COMPANY_IDS = {"sony": 0x012D, "canon": 0x01A9, "fujifilm": 0x04D8, "nikon": 0x0399}


def detect(address: str, info: AdvInfo, brands: Iterable[str] | None = None) -> Found | None:
    """Which brand (if any) an advertisement belongs to."""
    wanted = {normalize_brand(b) for b in brands} if brands else None
    for brand, cls in DRIVERS.items():
        if wanted is not None and brand not in wanted:
            continue
        m = cls.match(info)
        if m is None:
            continue
        pairing, extra = m
        cid = _COMPANY_IDS.get(brand)
        raw = info.mfg[cid].hex() if cid is not None and cid in info.mfg else ""
        return Found(address, info.name or cls.default_name, info.rssi, pairing, raw, brand, extra)
    return None


async def scan(timeout: float = 6.0, *, brands: Iterable[str] | None = None,
               include_unsupported: bool = True) -> list[Found]:
    """Listen for `timeout` seconds; strongest signal first. Sony headphones etc. are filtered out."""
    from bleak import BleakScanner

    wanted = list(brands) if brands else None
    found: dict[str, Found] = {}

    def cb(dev: Any, adv: Any) -> None:
        f = detect(dev.address, AdvInfo.from_bleak(dev, adv), wanted)
        if f is None or (not include_unsupported and not f.supported):
            return
        prev = found.get(dev.address)
        if prev is not None:     # adverts and scan responses arrive separately; keep what we already learned
            if not (getattr(adv, "local_name", None) or getattr(dev, "name", None)):
                f.name = prev.name
            if f.pairing_mode is None:
                f.pairing_mode = prev.pairing_mode
            f.extra = {**prev.extra, **f.extra}
            f.raw = f.raw or prev.raw
        found[dev.address] = f

    async with BleakScanner(cb):
        await asyncio.sleep(timeout)
    return sorted(found.values(), key=lambda f: -(f.rssi if f.rssi is not None else -999))
