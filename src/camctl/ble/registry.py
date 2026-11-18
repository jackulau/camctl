"""Brand → driver class. Scan detection tries brands in this order (name-only matches last)."""

from __future__ import annotations

from typing import Any

from .base import Remote
from .canon import CanonRemote
from .errors import NotSupported
from .fujifilm import FujifilmRemote
from .gopro import GoProRemote
from .nikon import NikonRemote
from .omsystem import OMSystemRemote
from .panasonic import PanasonicRemote
from .sony import SonyRemote

DRIVERS: dict[str, type[Remote]] = {
    "sony": SonyRemote,
    "canon": CanonRemote,
    "fujifilm": FujifilmRemote,
    "nikon": NikonRemote,
    "gopro": GoProRemote,
    "panasonic": PanasonicRemote,
    "omsystem": OMSystemRemote,
}
ALIASES = {"fuji": "fujifilm", "lumix": "panasonic", "olympus": "omsystem", "om": "omsystem",
           "om-system": "omsystem", "om_system": "omsystem"}


def normalize_brand(brand: str) -> str:
    b = brand.strip().lower()
    b = ALIASES.get(b, b)
    if b not in DRIVERS:
        raise NotSupported(f"Unknown camera brand {brand!r}; use one of: {', '.join(DRIVERS)}.")
    return b


def driver_for(brand: str) -> type[Remote]:
    return DRIVERS[normalize_brand(brand)]


def supported_brands() -> dict[str, dict[str, Any]]:
    """Per brand: capabilities, which are verified on hardware / experimental, feedback, camera menus."""
    return {b: cls.describe() for b, cls in DRIVERS.items()}
