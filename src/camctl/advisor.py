"""Exposure math used when planning a shoot."""

from __future__ import annotations

import math
from fractions import Fraction

# Shutter speeds the A7 V offers in the 1/25–1/8000 range (1/3-stop steps).
STANDARD_SHUTTERS = [25, 30, 40, 50, 60, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000,
                     1250, 1600, 2000, 2500, 3200, 4000, 5000, 6400, 8000]


def _positive(value: float, what: str) -> float:
    if not value > 0 or math.isinf(value):
        raise ValueError(f"{what} must be a positive number, not {value:g}")
    return value


def shutter_for_fps(fps: float, angle: float = 180.0) -> dict:
    """Shutter-angle rule → nearest real shutter speed. 24→1/50, 30→1/60, 60→1/125, 120→1/250."""
    _positive(fps, "fps")
    if not 0 < angle <= 360:
        raise ValueError(f"shutter angle must be in (0, 360], not {angle:g}")
    ideal = fps * 360.0 / angle
    nearest = min(STANDARD_SHUTTERS, key=lambda d: abs(math.log2(d / ideal)))
    return {"fps": fps, "angle": angle, "ideal": f"1/{ideal:g}", "shutter": f"1/{nearest}",
            "effective_angle": round(360 * fps / nearest, 1)}


def slowmo(source_fps: float, timeline_fps: float = 24) -> dict:
    """60p on a 24p timeline plays at 40% speed (2.5× slower) with no frame duplication."""
    factor = _positive(source_fps, "source fps") / _positive(timeline_fps, "timeline fps")
    return {"source_fps": source_fps, "timeline_fps": timeline_fps, "slowdown": round(factor, 3),
            "clip_speed_percent": round(100 / factor, 1)}


def _seconds(shutter: str | float) -> float:
    s = str(shutter).strip().rstrip("s")
    try:
        secs = float(Fraction(s)) if "/" in s else float(s)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"not a shutter speed: {shutter!r} (use e.g. 1/1000 or 0.5)") from None
    if not secs > 0 or math.isinf(secs):
        raise ValueError(f"not a shutter speed: {shutter!r} (must be > 0)")
    return secs


def nd_for(metered_shutter: str, target_shutter: str = "1/50") -> dict:
    """ND strength to get from the shutter the meter wants to the one you want to keep."""
    stops = math.log2(_seconds(target_shutter) / _seconds(metered_shutter))
    if stops <= 0:
        return {"stops": round(stops, 2), "nd_factor": 1, "note": "no ND needed — scene is darker than target"}
    factor = 2 ** stops
    return {"stops": round(stops, 2), "nd_factor": f"ND{round(factor)}",
            "optical_density": round(stops * 0.301, 2),
            "note": f"set variable ND to ~{stops:.1f} stops, then fine-tune with zebras"}


WB_KELVIN = {"daylight": 5600, "cloudy": 6250, "shade": 7000, "tungsten": 3200, "led_panel": 5600,
             "warm_home_bulb": 2700, "fluorescent": 4000, "golden_hour": 4500}
