"""OM System / Olympus — recognised in scans by name only; there is no public BLE remote protocol, so every
command raises NotSupported. Use USB (PTP) for these bodies.
"""

from __future__ import annotations

import re
from typing import Any

from .base import Remote

NAME_RE = re.compile(r"^(OM[- ]?\d|OM SYSTEM|OLYMPUS|E-M\d|E-P\d|E-PL\d|PEN[- ]|TG-\d)", re.I)


class OMSystemRemote(Remote):
    brand = "omsystem"
    label = "OM System camera"
    default_name = "OM System"
    unsupported_reason = ("OM System / Olympus cameras have no public Bluetooth remote protocol; control them over "
                          "USB (PTP) instead.")

    @classmethod
    def match(cls, info: Any) -> tuple[bool | None, dict[str, Any]] | None:
        return (None, {}) if info.name and NAME_RE.match(info.name) else None
