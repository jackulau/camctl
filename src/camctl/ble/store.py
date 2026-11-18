"""Remembered cameras: ~/.config/camctl/ble.json.

Format v2: {"version": 2, "default": "<address>", "devices": {"<address>": {"address", "name", "brand", "extra"}}}
The Sony-only v1 file {"address": ..., "name": ...} is read transparently as one Sony device and rewritten as v2 on
the next change. `remembered()` still returns the default device's address.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..paths import CONFIG_DIR

log = logging.getLogger("camctl.ble")

STATE = CONFIG_DIR / "ble.json"
_DEFAULT_STATE = STATE


@dataclass
class Device:
    address: str
    name: str | None = None
    brand: str = "sony"
    extra: dict[str, Any] = field(default_factory=dict)   # Nikon dev_id, Fujifilm token, classic "mac", ...

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Device":
        return cls(str(d["address"]), d.get("name"), d.get("brand") or "sony", dict(d.get("extra") or {}))


def _path() -> Path:
    # Honour a patched camctl.ble.STATE as well as a patched store.STATE (tests, alternative config dirs).
    if STATE != _DEFAULT_STATE:
        return STATE
    return Path(getattr(sys.modules.get(__package__ or ""), "STATE", STATE))


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text())
    except FileNotFoundError:
        return {"version": 2, "default": None, "devices": {}}
    except (OSError, ValueError):
        log.warning("ignoring unreadable %s", _path())
        return {"version": 2, "default": None, "devices": {}}
    if not isinstance(data, dict):
        return {"version": 2, "default": None, "devices": {}}
    if "devices" not in data and data.get("address"):          # v1 (Sony-only) file
        addr = str(data["address"])
        return {"version": 2, "default": addr,
                "devices": {addr: {"address": addr, "name": data.get("name"), "brand": "sony", "extra": {}}}}
    devices = data.get("devices") if isinstance(data.get("devices"), dict) else {}
    clean = {}
    for k, v in devices.items():
        if isinstance(v, dict) and v.get("address"):
            clean[k] = v
    return {"version": 2, "default": data.get("default"), "devices": clean}


def _save(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ble.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _key(data: dict[str, Any], address_or_name: str) -> str | None:
    k = address_or_name.lower()
    for addr, d in data["devices"].items():
        if addr.lower() == k:
            return addr
    for addr, d in data["devices"].items():
        if (d.get("name") or "").lower() == k:
            return addr
    return None


def remember(address: str, name: str | None = None, brand: str | None = None, *, extra: dict[str, Any] | None = None,
             default: bool = True) -> Device:
    """Add or update a device (extra is merged) and make it the default unless default=False."""
    data = _load()
    key = _key(data, address) or address
    old = data["devices"].pop(key, {})
    dev = Device(address, name or old.get("name"), brand or old.get("brand") or "sony",
                 {**(old.get("extra") or {}), **(extra or {})})
    data["devices"][address] = dev.to_dict()
    if default or not data.get("default") or (data.get("default") or "").lower() == key.lower():
        data["default"] = address
    _save(data)
    return dev


def remembered() -> str | None:
    """Address of the default device (compatible with the Sony-only API)."""
    dev = get_device()
    return dev.address if dev else None


def devices() -> list[Device]:
    return [Device.from_dict(d) for d in _load()["devices"].values()]


def get_device(address_or_name: str | None = None) -> Device | None:
    """A remembered device by address or name; None → the default device."""
    data = _load()
    if address_or_name is None:
        key = data.get("default")
        key = _key(data, key) if key else None
        if key is None and data["devices"]:
            key = next(iter(data["devices"]))
    else:
        key = _key(data, address_or_name)
    return Device.from_dict(data["devices"][key]) if key else None


def set_default(address_or_name: str) -> Device:
    data = _load()
    key = _key(data, address_or_name)
    if key is None:
        raise KeyError(f"No remembered camera {address_or_name!r}.")
    data["default"] = key
    _save(data)
    return Device.from_dict(data["devices"][key])


def forget(address_or_name: str) -> bool:
    """Remove a remembered device. Returns False if it was not remembered."""
    data = _load()
    key = _key(data, address_or_name)
    if key is None:
        return False
    del data["devices"][key]
    if (data.get("default") or "").lower() == key.lower():
        data["default"] = next(iter(data["devices"]), None)
    _save(data)
    return True
