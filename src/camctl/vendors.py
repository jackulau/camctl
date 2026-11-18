"""Camera vendor layer: detection, per-vendor widget maps, value synonyms and USB quirks.

libgphoto2 names the same setting differently per vendor (Sony `f-number`, Canon `aperture`; Canon
`autoexposuremode`, Nikon `expprogram`) and spells values differently ("AI Servo", "Continuous-Servo AF",
"C-AF", "AF_C" are all AF-C). Presets stay vendor-agnostic; the profile detected for the connected camera
translates canonical keys and values, and its hooks handle the vendor's capture / movie / exposure-unlock
quirks.

Verified on hardware: Sony (ILCE-7M5). Canon, Nikon, Fujifilm, Panasonic and OM System/Olympus are built
from libgphoto2 2.5.34 config.c tables and are untested on hardware.

Integration entry points: `detect()` / `identify()`, `for_camera()`, `capabilities()`, `setup_hints()`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .settings import (APPLY_ORDER, CANONICAL, CANONICAL_BY_KEY, ENUMS, MENU_ONLY, SYNONYMS, WIDGET_KEY, MatchResult,
                       canonical_key as _sony_canonical_key, label_for, match_choice, normalize, resolve_widget)


class CameraError(RuntimeError):
    """A camera operation failed; the message is meant for the user (re-exported as ptp.CameraError)."""


# PTP DeviceInfo VendorExtensionID → vendor (0x06 = Microsoft/MTP: fall back to the manufacturer string).
VENDOR_IDS = {0x0B: "canon", 0x0A: "nikon", 0x0E: "fuji", 0x1C: "panasonic", 0x11: "sony",
              0xFFFD: "olympus", 0xFFFE: "olympus"}
MANUFACTURER_MATCH = {"canon": "canon", "nikon": "nikon", "fujifilm": "fuji", "panasonic": "panasonic",
                      "olympus": "olympus", "om digital": "olympus", "omsystem": "olympus", "sony": "sony"}
# Fallbacks when neither manufacturer nor vendor extension is reported (e.g. dumps, some PTP/IP stacks).
_MODEL_PATTERNS = (
    ("sony", r"^(ilce|ilca|ilme|dsc|zv|nex|slt)-"),
    ("canon", r"canon|^eos\b"),
    ("nikon", r"nikon|^z ?\d|^z ?f\b|^d\d{2,4}\b"),
    ("fuji", r"fujifilm|^x-|^gfx"),
    ("panasonic", r"panasonic|lumix|^dc-|^dmc-"),
    ("olympus", r"olympus|^om-\d|^e-m\d|^e-p\d|^pen"),
)
_FINGERPRINTS = (                       # widget names only one vendor's libgphoto2 driver creates
    ("sony", ("d25a",)),
    ("canon", ("eosremoterelease", "autoexposuremode", "eosmoviemode")),
    ("nikon", ("shutterspeed2", "movieprohibit", "liveviewprohibit")),
    ("fuji", ("filmsimulation", "prioritymode")),
    ("panasonic", ("expmode", "afmode")),
)

DEFAULT_LOCK = "locked in the current mode — check the mode dial"
# Lock reasons that depend on another setting rather than the mode dial (seen on a real A7 V).
COLOR_TEMP_LOCK = "set white_balance to Color Temp first"
MOVIE_ONLY_LOCK = "movie-only setting; switch to Movie mode"
_KELVIN_WB = ("temperature", "kelvin", "c.temp", "custom", "preset")     # WB modes that use / keep a Kelvin value
DEFAULT_DIAL_MESSAGE = ("Exposure is locked: remote mode change was refused. Turn the mode dial to M "
                        "and run apply again.")
EXPOSURE_KEYS = frozenset({"exposure_program", "shutter", "aperture", "iso", "iso_auto", "exposure_comp"})
DIAL_KEYS = ("exposure_program", "shutter", "aperture", "iso")
# Action / state widgets used by the hooks — reported by capabilities(), never applied as settings.
CONTROL_KEYS = ("live_view", "movie", "movie_mode_switch", "shutter_button", "bulb")
_HEX = re.compile(r"^[0-9a-f]{4}$")
_MOVIE_PROGRAM = re.compile(r"^\s*(?:movie|s&q)\s*\(\s*([a-z]+)\s*\)\s*$", re.I)
_AUTO_WORDS = {"auto", "automatic", "auto iso", "iso auto"}

# Setting names shared by libgphoto2's generic PTP driver (standard 0x50xx properties).
GENERIC_WIDGETS: dict[str, tuple[str, ...]] = {
    "exposure_program": ("expprogram",), "shutter": ("shutterspeed",), "aperture": ("f-number", "aperture"),
    "iso": ("iso",), "exposure_comp": ("exposurecompensation",), "white_balance": ("whitebalance",),
    "focus_mode": ("focusmode",), "metering": ("exposuremetermode",), "drive": ("capturemode",),
    "image_quality": ("imageformat", "imagequality"), "capture_target": ("capturetarget",),
}

# libgphoto2 standard StillCaptureMode strings ("Single Shot" / "Burst") used by Nikon, Fuji and generic PTP.
_PTP_DRIVE = {"single": ("single shot", "single frame"), "continuous_hi": ("continuous high speed", "burst"),
              "continuous_mid": ("continuous low speed", "burst"), "continuous_lo": ("continuous low speed", "burst")}


def _pick(choices: list[str] | None, *needles: str) -> str | None:
    """First choice whose normalised text contains a needle (needles tried in order)."""
    for needle in needles:
        for c in choices or []:
            if needle in normalize(c):
                return c
    return None


def _widget(cam: Any, name: str) -> dict | None:
    try:
        return cam.widget(name)
    except CameraError:
        return None


# Sony SDIO controls (buttons / one-shot operations). libgphoto2 2.5.34 exposes them as text widgets with no
# backing value: get_value() on them reads freed memory and intermittently segfaults a full config walk.
SONY_CONTROLS = frozenset({"d2c1", "d2c2", "d2c3", "d2c4", "d2c7", "d2c8", "d2c9", "d2d1", "d2d9", "d2dc",
                           "d2dd", "d2e9", "d2ea"})


@dataclass(frozen=True, eq=False)
class VendorProfile:
    """Everything camctl needs to know about one vendor's libgphoto2 driver.

    Data: `widgets` (canonical key → widget candidates, most likely first), `synonyms` (canonical value →
    vendor spellings, tried exactly before the global table), `setup` (camera-menu text), `lock_hints`
    (why a key is read-only), `physical` (keys owned by a dial: (camera step, why) — a read-only one becomes a
    camera-menu step instead of a failure). Hooks are methods; subclasses override the quirky ones.
    """

    name: str
    display: str
    widgets: Mapping[str, tuple[str, ...]]
    setup: str
    synonyms: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    lock_hints: Mapping[str, str] = field(default_factory=dict)
    physical: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    dial_keys: tuple[str, ...] = DIAL_KEYS
    dial_message: str = DEFAULT_DIAL_MESSAGE
    notes: tuple[str, ...] = ()
    tested: bool = False
    reopen_after_write: bool = False     # libgphoto2 2.5.34 Sony full-config-after-write segfault workaround
    capture_to_card: bool = False        # set capturetarget → memory card before capturing
    movie_unsupported: str = ""          # non-empty → movie() raises this sentence
    write_only: frozenset[str] = frozenset()   # widgets whose value must never be read (see SONY_CONTROLS)

    # -- key / widget mapping --------------------------------------------------------------------------
    @property
    def enums(self) -> Mapping[str, Mapping[str, str]]:
        return {}

    @property
    def menu_only(self) -> Mapping[str, str]:
        return {}

    def candidates(self, key: str) -> tuple[str, ...]:
        return tuple(self.widgets.get(key, ()))

    def supports(self, key: str) -> bool:
        """True if this vendor exposes the canonical key over USB at all (a given body may still lack it)."""
        return bool(self.candidates(key)) or key in self.menu_only

    def canonical_key(self, key: str) -> str:
        """Canonical key for a canonical name or raw widget name (hex dXXX codes are vendor-specific)."""
        if key in CANONICAL_BY_KEY:
            return key
        for ckey, cands in self.widgets.items():
            if key in cands:
                return ckey
        if _HEX.match(key):
            return key
        return WIDGET_KEY.get(key, key)

    def resolve(self, key: str, flat: Mapping[str, dict]) -> str | None:
        """Widget present in `flat` for a canonical key or raw widget name."""
        if key in flat:
            return key
        if key in CANONICAL_BY_KEY:
            return resolve_widget(key, flat, candidates=self.candidates(key))
        return None

    def control_widget(self, key: str, flat: Mapping[str, dict]) -> str | None:
        return next((w for w in self.candidates(key) if w in flat), None)

    def sort_for_apply(self, keys: list[str]) -> list[str]:
        return sorted(keys, key=lambda k: APPLY_ORDER.get(self.canonical_key(k), len(APPLY_ORDER)))

    def enum_table(self, key: str, widget: str | None) -> Mapping[str, str] | None:
        return None

    # -- values --------------------------------------------------------------------------------------
    def match(self, key: str, wanted: Any, choices: list[str]) -> MatchResult:
        """Vendor spellings first (exact only), then the shared matcher (synonyms, numbers, 'auto')."""
        table = self.synonyms.get(key)
        if table and choices:
            if isinstance(wanted, bool):
                wanted = "on" if wanted else "off"
            want = normalize(wanted)
            norm = {normalize(c): c for c in choices}
            if want not in norm:
                shared = SYNONYMS.get(key, {})
                for canon, spellings in table.items():
                    spellings = tuple(normalize(s) for s in spellings)
                    if want == normalize(canon) or want in spellings or want in shared.get(canon, ()):
                        for sp in spellings:
                            if sp in norm:
                                return MatchResult(norm[sp], True)
        return match_choice(key, wanted, choices)

    def translate(self, ckey: str, wanted: Any) -> tuple[Any, str | None]:
        """Preset value → value for this vendor. (None, camera step) = can't be done over USB, do it on the camera.

        Non-Sony exposure programs have no separate movie variants: "Movie (M)" means M while the camera's
        stills/movie switch is on movie.
        """
        if ckey == "exposure_program" and isinstance(wanted, str):
            m = _MOVIE_PROGRAM.match(wanted)
            if m:
                return m.group(1).upper(), None
        return wanted, None

    def expand(self, settings: Mapping[str, Any], flat: Mapping[str, dict]) -> dict[str, Any]:
        """Rewrite a settings dict for this vendor before applying (e.g. Nikon's separate auto-ISO toggle)."""
        out = dict(settings)
        if ("iso" in out and "iso_auto" not in out and self.supports("iso_auto")
                and self.resolve("iso_auto", flat)):
            auto = normalize(out["iso"]) in _AUTO_WORDS
            out["iso_auto"] = "On" if auto else "Off"
            if auto:
                del out["iso"]
        return out

    def readonly_note(self, ckey: str) -> str:
        return self.lock_hints.get(ckey, DEFAULT_LOCK)

    def movie_mode(self, flat: Mapping[str, dict]) -> bool | None:
        """Is the camera in a movie exposure mode? None = this vendor can't tell from the config."""
        return None

    def context_lock(self, ckey: str, flat: Mapping[str, dict]) -> str | None:
        """Why `ckey` is read-only when another setting (not the dial) is the cause; None = no such cause."""
        if ckey == "color_temp":
            w = self.resolve("white_balance", flat)
            if w is not None:
                table = self.enum_table("white_balance", w)
                wb = flat[w].get("value")
                wb = label_for("white_balance", wb, table) if table else wb
                if not any(k in normalize(wb) for k in _KELVIN_WB):
                    return COLOR_TEMP_LOCK
        if ckey == "steadyshot_movie" and self.movie_mode(flat) is False:
            return MOVIE_ONLY_LOCK
        return None

    def lock_reason(self, ckey: str, flat: Mapping[str, dict]) -> str:
        if ckey in self.physical:
            return self.physical[ckey][1]
        return self.context_lock(ckey, flat) or self.readonly_note(ckey)

    def physical_step(self, ckey: str, value: Any) -> str:
        step, why = self.physical[ckey]
        return f"{step.format(value=value)} ({why})"

    # -- hooks ------------------------------------------------------------------------------------------
    def unlock_exposure(self, cam: Any, ckey: str, widget: str, info: dict, flat: Mapping[str, dict],
                        dry_run: bool) -> dict | None:
        """Called before each write; return fresh widget info if the hook changed camera state."""
        return None

    def prepare_capture(self, cam: Any) -> None:
        if not self.capture_to_card:
            return
        for w in self.candidates("capture_target"):
            info = _widget(cam, w)
            if info is None:
                continue
            card = _pick(info.get("choices"), "memory card", "card", "sd")
            if card and str(info.get("value")) != card and not info.get("readonly"):
                try:
                    cam.set(w, card)
                except CameraError:
                    pass                      # capture still works; the file just stays off the card
            return

    def movie(self, cam: Any, start: bool) -> None:
        if self.movie_unsupported:
            raise CameraError(self.movie_unsupported)
        w = next((c for c in self.candidates("movie") if _widget(cam, c) is not None), None)
        if w is None:
            raise CameraError(f"This {self.display} camera exposes no movie-record control over USB — "
                              "start and stop recording on the camera.")
        try:
            cam.set(w, 1 if start else 0)
        except CameraError as e:
            raise CameraError(self.movie_refused(cam, e, start)) from e

    def movie_refused(self, cam: Any, err: Exception, start: bool) -> str:
        return (f"{self.display} refused to {'start' if start else 'stop'} recording ({err}) — "
                "put the camera in movie mode and check the card has free space.")

    def half_press(self, cam: Any, on: bool) -> None:
        cam.set("autofocus", 1 if on else 0)

    def refusal_hint(self, cam: Any, action: str) -> str:
        """Extra text for a refused live-view / capture (e.g. Nikon's readable prohibit reason)."""
        return ""

    def card_status(self, flat: Mapping[str, dict]) -> dict | None:
        return None


class SonyProfile(VendorProfile):
    """Verified on ILCE-7M5 fw 1.01 — behaviour here must not change without a hardware re-check."""

    @property
    def enums(self) -> Mapping[str, Mapping[str, str]]:
        return ENUMS

    @property
    def menu_only(self) -> Mapping[str, str]:
        return MENU_ONLY

    def canonical_key(self, key: str) -> str:
        return _sony_canonical_key(key)

    def enum_table(self, key: str, widget: str | None) -> Mapping[str, str] | None:
        if key in ENUMS:
            return ENUMS[key]
        if key in CANONICAL_BY_KEY and widget and WIDGET_KEY.get(widget) in ENUMS:
            return ENUMS[WIDGET_KEY[widget]]  # portable key on a Sony code, e.g. picture_style → d0fa
        return None

    def match(self, key: str, wanted: Any, choices: list[str]) -> MatchResult:
        return match_choice(key, wanted, choices)

    def translate(self, ckey: str, wanted: Any) -> tuple[Any, str | None]:
        return wanted, None                   # 500e has real Movie (M) / S&Q codes

    def movie(self, cam: Any, start: bool) -> None:
        cam.set("movie", 1 if start else 0)   # needs Movie on the Photo/Movie/S&Q switch

    def unlock_exposure(self, cam, ckey, widget, info, flat, dry_run):
        # 500e is read-only until priority key d25a = 1 (PC Remote); session-scoped (ptp re-asserts it).
        if ckey == "exposure_program" and info.get("readonly") and "d25a" in flat and not dry_run:
            try:
                cam.set("d25a", "1")
                return cam.widget(widget) if hasattr(cam, "widget") else flat[widget]
            except Exception:  # noqa: BLE001 — fall through to the readonly report
                return None
        return None

    def movie_mode(self, flat: Mapping[str, dict]) -> bool | None:
        if "500e" not in flat:
            return None
        return label_for("exposure_program", _value(flat, "500e")).startswith(("Movie", "S&Q"))

    def lock_reason(self, ckey: str, flat: Mapping[str, dict]) -> str:
        if (ctx := self.context_lock(ckey, flat)) is not None:
            return ctx
        if ckey == "exposure_program" and "d25a" in flat:
            return ("follows the mode dial until the priority key is PC Remote — camctl switches it "
                    "automatically when applying settings")
        if ckey in ("shutter", "aperture", "iso"):
            return "locked by the current exposure mode — apply exposure_program M or turn the mode dial to M"
        return DEFAULT_LOCK

    def card_status(self, flat: Mapping[str, dict]) -> dict | None:
        def num(name: str) -> int | None:
            try:
                return int(float(flat[name]["value"]))
            except (KeyError, TypeError, ValueError):
                return None
        shots, movie_s = num("d249"), num("d24a")     # MediaSLOT1_RemainingNumber / _RemainingTime
        if shots is None and movie_s is None:
            return None
        # Real A7 V dump: d24a reads 0 in stills modes with 656 shots free → count it only in Movie / S&Q.
        movie_mode = bool(self.movie_mode(flat))
        out: dict[str, Any] = {"slot": 1, "remaining_shots": shots, "remaining_movie_seconds": movie_s,
                               "warning": None}
        if shots == 0 and (movie_s == 0 or movie_s is None):
            out["warning"] = "Card slot 1 is full or missing — insert a card or free space before shooting."
        elif (shots is not None and shots < 50) or (movie_s == 0 and movie_mode):
            left = [f"{shots} shots" if shots is not None else None,
                    f"{movie_s // 60} min movie" if movie_s is not None else None]
            out["warning"] = (f"Card slot 1 nearly full ({', '.join(x for x in left if x)} left) — "
                              "offload or swap the card.")
        if "d257" in flat:                            # MediaSLOT2_RemainingNumber / _RemainingTime
            out["slot2"] = {"remaining_shots": num("d257"), "remaining_movie_seconds": num("d258")}
        return out


class CanonProfile(VendorProfile):
    def movie(self, cam: Any, start: bool) -> None:
        target = _widget(cam, "movierecordtarget")
        if target is None:
            raise CameraError("This Canon body offers no movie control over USB — set the Stills/Movie switch "
                              "to Movie and press the record button on the camera.")
        try:
            if start:
                mode = _widget(cam, "eosmoviemode")
                if mode is not None and not mode.get("readonly") and str(mode.get("value")) not in ("1", "1.0"):
                    cam.set("eosmoviemode", 1)
                if _widget(cam, "viewfinder") is not None:
                    cam.set("viewfinder", 1)          # EOS records only from movie live view
                cam.set("movierecordtarget", _pick(target.get("choices"), "card") or "Card")
            else:
                cam.set("movierecordtarget", _pick(target.get("choices"), "none") or "None")
        except CameraError as e:
            raise CameraError(self.movie_refused(cam, e, start)) from e

    def movie_refused(self, cam, err, start):
        return (f"Canon refused to {'start' if start else 'stop'} recording ({err}) — set the Stills/Movie switch "
                "to Movie, turn Wi-Fi off and check the card has free space.")

    def half_press(self, cam: Any, on: bool) -> None:
        if on:
            cam.set("autofocusdrive", 1)
        elif _widget(cam, "cancelautofocus") is not None:
            cam.set("cancelautofocus", 1)


class NikonProfile(VendorProfile):
    def _prohibit(self, cam: Any, name: str) -> str:
        info = _widget(cam, name)
        return str(info.get("value")) if info and info.get("value") not in (None, "") else ""

    def movie_refused(self, cam, err, start):
        reason = self._prohibit(cam, "movieprohibit") or str(err)
        return (f"Nikon refused to {'start' if start else 'stop'} recording ({reason}) — switch the photo/movie "
                "selector to movie, set the mode dial to P/A/S/M and insert a card with free space.")

    def half_press(self, cam: Any, on: bool) -> None:
        if on:
            cam.set("autofocusdrive", 1)

    def refusal_hint(self, cam: Any, action: str) -> str:
        reason = self._prohibit(cam, "liveviewprohibit")
        return (f" — Nikon says: {reason}. Live view needs the mode dial on P, A, S or M." if reason else
                " — Nikon allows live view only with the mode dial on P, A, S or M.")


class FujiProfile(VendorProfile):
    def unlock_exposure(self, cam, ckey, widget, info, flat, dry_run):
        # libgphoto2 resets prioritymode to Camera after each capture → claim USB control before writing.
        if dry_run or ckey not in EXPOSURE_KEYS or "prioritymode" not in flat:
            return None
        prio = _widget(cam, "prioritymode")
        if prio is None or "usb" in normalize(prio.get("value", "")):
            return None
        usb = _pick(prio.get("choices"), "usb")
        if usb is None or prio.get("readonly"):
            return None
        try:
            cam.set("prioritymode", usb)
            return cam.widget(widget) if hasattr(cam, "widget") else flat[widget]
        except Exception:  # noqa: BLE001 — the write below reports the real problem
            return None


class PanasonicProfile(VendorProfile):
    def translate(self, ckey: str, wanted: Any) -> tuple[Any, str | None]:
        if ckey == "exposure_program" and isinstance(wanted, str):
            m = _MOVIE_PROGRAM.match(wanted)
            if m:
                return m.group(1).upper(), None
            return None, (f"Mode dial → {wanted} (Panasonic exposes only the Creative Video P/A/S/M program "
                          "over USB; stills modes follow the dial)")
        return wanted, None

    def movie_refused(self, cam, err, start):
        return (f"Panasonic refused to {'start' if start else 'stop'} recording ({err}) — set USB Mode to "
                "PC(Tether) and put the camera in a mode that allows video (e.g. Creative Video).")


# -- vendor data --------------------------------------------------------------------------------------

_SONY_WIDGETS = {c.key: c.candidates for c in CANONICAL} | {
    "picture_style": ("d0fa", "d23f"), "movie": ("movie",), "bulb": ("bulb",)}

_CANON_WIDGETS = {
    "exposure_program": ("autoexposuremode",), "exposure_program_dial": ("autoexposuremodedial",),
    "shutter": ("shutterspeed",), "aperture": ("aperture",), "iso": ("iso",),
    "exposure_comp": ("exposurecompensation",), "white_balance": ("whitebalance",),
    "color_temp": ("colortemperature",), "focus_mode": ("focusmode",), "focus_area": ("afmethod",),
    "drive": ("drivemode",), "metering": ("meteringmode",),
    "image_quality": ("imageformat", "imageformatsd", "imageformatcf"), "picture_style": ("picturestyle",),
    "capture_target": ("capturetarget",), "live_view": ("viewfinder",), "movie": ("movierecordtarget",),
    "movie_mode_switch": ("eosmoviemode",), "shutter_button": ("eosremoterelease",), "bulb": ("bulb",),
}
_NIKON_WIDGETS = {
    "exposure_program": ("expprogram",), "shutter": ("shutterspeed2", "shutterspeed"), "aperture": ("f-number",),
    "iso": ("iso",), "iso_auto": ("autoiso", "isoauto"), "exposure_comp": ("exposurecompensation",),
    "white_balance": ("whitebalance",), "color_temp": ("d01e",), "focus_mode": ("focusmode", "focusmode2"),
    "focus_area": ("liveviewafmode", "autofocusarea"), "drive": ("capturemode",),
    "metering": ("exposuremetermode",), "image_quality": ("imagequality",), "picture_style": ("d200",),
    "capture_target": ("capturetarget", "recordingmedia"), "live_view": ("viewfinder",), "movie": ("movie",),
    "movie_quality": ("moviequality",), "bulb": ("bulb",),
}
_FUJI_WIDGETS = {
    "priority_key": ("prioritymode",), "exposure_program": ("expprogram",), "shutter": ("shutterspeed",),
    "aperture": ("aperture", "f-number"), "iso": ("iso",), "exposure_comp": ("exposurecompensation",),
    "white_balance": ("whitebalance",), "color_temp": ("colortemperature",), "focus_mode": ("focusmode",),
    "drive": ("capturemode",), "metering": ("exposuremetermode",), "image_quality": ("imageformat",),
    "picture_style": ("filmsimulation",), "raw_compression": ("rawcompression",), "bulb": ("bulb",),
}
_PANASONIC_WIDGETS = {
    "exposure_program": ("expmode",), "shutter": ("shutterspeed",), "aperture": ("f-number",), "iso": ("iso",),
    "exposure_comp": ("exposurecompensation",), "white_balance": ("whitebalance",),
    "color_temp": ("colortemperature",), "focus_mode": ("afmode",), "image_quality": ("imageformat",),
    "capture_target": ("capturetarget",), "live_view": ("viewfinder",), "movie": ("movie",), "bulb": ("bulb",),
}
_OLYMPUS_WIDGETS = {
    "exposure_program": ("d006",), "shutter": ("shutterspeed",), "aperture": ("aperture",), "iso": ("iso", "d1c0"),
    "exposure_comp": ("exposurecompensation",), "white_balance": ("whitebalance",),
    "color_temp": ("colortemperature",), "focus_mode": ("focusmode",), "drive": ("d009",),
    "metering": ("exposuremetermode",), "image_quality": ("imageformat",), "bulb": ("bulb",),
}

_CANON_SYNONYMS = {
    "exposure_program": {"m": ("manual",), "a": ("av", "aperture priority"), "s": ("tv", "shutter priority"),
                         "p": ("program",)},
    "focus_mode": {"af-c": ("ai servo",), "af-s": ("one shot",), "af-a": ("ai focus",)},
    "white_balance": {"shade": ("shadow",), "color_temp": ("color temperature",)},
    "metering": {"multi": ("evaluative",), "center": ("center-weighted average", "center weighted average"),
                 "spot": ("spot", "partial")},
    "drive": {"single": ("single", "single shooting"),
              "continuous_hi": ("continuous high speed", "high speed continuous shooting"),
              "continuous_mid": ("continuous", "continuous low speed"),
              "continuous_lo": ("continuous low speed", "low speed continuous shooting")},
    "image_quality": {"raw+jpeg": ("raw + l", "craw + l"), "raw": ("raw", "craw"), "jpeg": ("l",)},
}
_NIKON_SYNONYMS = {
    "metering": {"multi": ("multi spot", "matrix"), "center": ("center weighted",), "spot": ("center spot",)},
    "drive": _PTP_DRIVE,
    "image_quality": {"raw+jpeg": ("nef+fine", "nef+fine*", "nef+normal"), "raw": ("nef (raw)", "nef"),
                      "jpeg": ("jpeg fine", "jpeg fine*")},
}
_FUJI_SYNONYMS = {
    "focus_mode": {"af-c": ("continuous-servo af",), "af-s": ("single-servo af",)},
    "white_balance": {"color_temp": ("color temperature", "temperature 1", "color temperature 1")},
    "drive": _PTP_DRIVE,
    "image_quality": {"raw+jpeg": ("raw + jpeg fine", "raw + fine", "raw + jpeg normal"), "raw": ("raw",),
                      "jpeg": ("jpeg fine", "fine")},
}
_PANASONIC_SYNONYMS = {
    "focus_mode": {"af-c": ("af_c",), "af-s": ("af_s",), "af-a": ("af_f",), "mf": ("mf",)},
}
_OLYMPUS_SYNONYMS = {
    "focus_mode": {"af-c": ("c-af", "c-af+tr"), "af-s": ("s-af",), "mf": ("mf",)},
    "image_quality": {"raw+jpeg": ("large fine jpeg+raw", "lf+raw"), "raw": ("raw",),
                      "jpeg": ("large fine jpeg", "lf")},
}
_GENERIC_SYNONYMS = {"drive": _PTP_DRIVE}

_DIAL_STEP = "Mode dial → {value}"

PROFILES: dict[str, VendorProfile] = {
    "sony": SonyProfile(
        "sony", "Sony", _SONY_WIDGETS,
        setup="Camera: MENU → Network → Transfer/Remote → PC Remote Function → PC Remote: On, "
              "PC Remote Cnct Method: USB (older bodies: USB Connection → PC Remote).",
        notes=("Exposure mode is changed via priority key d25a = PC Remote; it reverts to the dial when the "
               "USB session closes.", "Card is not visible over USB in PC Remote mode."),
        tested=True, reopen_after_write=True, write_only=SONY_CONTROLS),
    "canon": CanonProfile(
        "canon", "Canon", _CANON_WIDGETS,
        setup="Camera: no USB menu setting needed — turn Wi-Fi/Bluetooth off (they block USB control). "
              "EOS bodies enter remote mode as soon as camctl connects.",
        synonyms=_CANON_SYNONYMS, capture_to_card=True,
        physical={"exposure_program": (_DIAL_STEP, "Canon bodies with a mode dial don't accept remote mode "
                                                   "changes")},
        notes=("capturetarget defaults to Internal RAM — camctl switches it to Memory card before capturing.",
               "Movie: live view on, then movierecordtarget Card (start) / None (stop).",
               "Bulb uses eosremoterelease Press Full / Release Full when the body has no BulbStart.")),
    "nikon": NikonProfile(
        "nikon", "Nikon", _NIKON_WIDGETS,
        setup="Camera: SETUP → USB → MTP/PTP; on Z bodies enable Airplane mode (Wi-Fi/Bluetooth off).",
        synonyms=_NIKON_SYNONYMS, capture_to_card=True,
        physical={"exposure_program": (_DIAL_STEP, "Nikon follows the PASM dial")},
        notes=("Live view and movie are refused unless the mode dial is on P/A/S/M; the reason is readable "
               "in 'liveviewprohibit' / 'movieprohibit'.", "Auto ISO is a separate On/Off toggle.")),
    "fuji": FujiProfile(
        "fuji", "Fujifilm", _FUJI_WIDGETS,
        setup="Camera: SET UP → CONNECTION SETTING → PC CONNECTION MODE → USB TETHER SHOOTING AUTO.",
        synonyms=_FUJI_SYNONYMS, dial_keys=("shutter", "aperture", "iso"),
        dial_message="Exposure is locked: set the shutter-speed dial, aperture ring and ISO dial to A so the "
                     "computer can set them, then run apply again.",
        lock_hints={"shutter": "set the shutter-speed dial to A so the computer can set it",
                    "aperture": "set the aperture ring to A so the computer can set it",
                    "iso": "set the ISO dial to A so the computer can set it"},
        physical={"exposure_program": ("Shutter-speed dial and aperture ring → A",
                                       "Fujifilm's exposure mode follows the dials; with both on A camctl sets "
                                       "shutter and aperture itself")},
        movie_unsupported="Fujifilm movie recording can't be started over USB (libgphoto2 2.5.34 has no movie "
                          "control for Fujifilm) — press the record button on the camera.",
        notes=("camctl sets prioritymode = USB before exposure writes (libgphoto2 resets it after each "
               "capture).", "USB Tether Shooting Auto saves captures to the computer only.")),
    "panasonic": PanasonicProfile(
        "panasonic", "Panasonic", _PANASONIC_WIDGETS,
        setup="Camera: SETUP → IN/OUT → USB Mode → PC(Tether).",
        synonyms=_PANASONIC_SYNONYMS,
        physical={"exposure_program": (_DIAL_STEP, "Panasonic exposes only the Creative Video P/A/S/M program "
                                                   "over USB")},
        notes=("Stills exposure mode follows the mode dial; expmode only sets Creative Video P/A/S/M.",
               "imageformat is reported as raw numbers.")),
    "olympus": VendorProfile(
        "olympus", "OM System/Olympus", _OLYMPUS_WIDGETS,
        setup="Camera: choose RAW/Control on the USB prompt when you plug in (or set USB Mode → RAW/Control).",
        synonyms=_OLYMPUS_SYNONYMS,
        physical={"exposure_program": (_DIAL_STEP, "OM System/Olympus follows the mode dial")},
        movie_unsupported="OM System/Olympus movie recording can't be started over USB (libgphoto2 2.5.34 has "
                          "no movie control) — press the record button on the camera.",
        notes=("libgphoto2 2.5.34 has no names for the exposure program (d006) or drive (d009) values.",)),
    "generic": VendorProfile(
        "generic", "this PTP camera", GENERIC_WIDGETS | {"movie": ("movie",)},
        setup="Camera: set the USB mode to PTP / PC control / tether.",
        synonyms=_GENERIC_SYNONYMS, reopen_after_write=True),
}
GENERIC = PROFILES["generic"]


# -- detection -----------------------------------------------------------------------------------------

def _value(flat: Mapping[str, dict], name: str) -> str:
    v = (flat.get(name) or {}).get("value")
    return str(v).strip() if v is not None else ""


def _by_manufacturer(text: str) -> str | None:
    low = text.lower()
    squashed = low.replace(" ", "")
    for needle, vendor in MANUFACTURER_MATCH.items():
        if needle in low or needle.replace(" ", "") in squashed:
            return vendor
    return None


def identify(flat: Mapping[str, dict], summary: str = "") -> tuple[str, str]:
    """→ (vendor name, evidence). Manufacturer widget first, then the PTP Vendor Extension ID in the summary,
    then manufacturer/model strings and vendor-only widget names; unknown → "generic"."""
    manufacturer = _value(flat, "manufacturer")
    if manufacturer and (v := _by_manufacturer(manufacturer)):
        return v, f"manufacturer {manufacturer!r}"
    m = re.search(r"Vendor Extension ID:\s*0x([0-9a-fA-F]+)", summary or "")
    if m and int(m.group(1), 16) in VENDOR_IDS:
        return VENDOR_IDS[int(m.group(1), 16)], f"PTP vendor extension 0x{int(m.group(1), 16):x}"
    m = re.search(r"^\s*Manufacturer:\s*(.+)$", summary or "", re.M)
    if m and (v := _by_manufacturer(m.group(1))):
        return v, f"manufacturer {m.group(1).strip()!r}"
    ext = _value(flat, "vendorextension")
    if ext and (v := _by_manufacturer(ext)):
        return v, f"vendor extension {ext!r}"
    models = [_value(flat, "cameramodel"), _value(flat, "model")]
    m = re.search(r"^\s*Model:\s*(.+)$", summary or "", re.M)
    if m:
        models.append(m.group(1).strip())
    for model in filter(None, models):
        for vendor, pattern in _MODEL_PATTERNS:
            if re.search(pattern, model, re.I):
                return vendor, f"model {model!r}"
    for vendor, names in _FINGERPRINTS:
        if any(n in flat for n in names):
            return vendor, f"{vendor}-only widget"
    return "generic", "unknown manufacturer"


def detect(flat: Mapping[str, dict], summary: str = "") -> VendorProfile:
    """Vendor profile for a flat config (PtpCamera.config()) and the libgphoto2 summary text."""
    return PROFILES[identify(flat, summary)[0]]


def _summary(cam: Any) -> str:
    try:
        return str(cam.summary()) if hasattr(cam, "summary") else ""
    except Exception:  # noqa: BLE001 — detection must never break a command
        return ""


def for_camera(cam: Any, flat: Mapping[str, dict] | None = None) -> VendorProfile:
    """Profile for a live camera: its session-detected `.vendor` if known, else detect from config + summary."""
    v = getattr(cam, "vendor", None)
    if isinstance(v, VendorProfile) and v is not GENERIC:
        return v
    return detect(cam.config() if flat is None else flat, _summary(cam))


def setup_hints() -> dict[str, str]:
    """Camera-side USB setup per vendor (for `doctor` when no camera is found)."""
    return {p.display: p.setup for name, p in PROFILES.items() if name != "generic"}


# -- capability discovery ------------------------------------------------------------------------------

def _entry(profile: VendorProfile, key: str, widget: str, info: dict) -> dict:
    table = profile.enum_table(key, widget)
    lab = (lambda v: label_for(key, v, table)) if table else (lambda v: v)
    d: dict[str, Any] = {"available": True, "widget": widget, "writable": not info.get("readonly"),
                         "value": lab(info.get("value"))}
    if "choices" in info:
        d["choices"] = [lab(c) for c in info["choices"]]
    elif "range" in info:
        d["range"] = info["range"]
    return d


def _prohibited(text: str) -> bool:
    t = normalize(text)
    return bool(t) and t not in ("0", "none") and not re.search(r"allowed|no prohibition|\bok\b", t)


def capabilities(cam: Any) -> dict:
    """Probe the live config: vendor, model, per-key availability/writability/choices, controls, locks,
    card status and camera-side setup hints. Read-only — never writes to the camera."""
    flat = cam.config()
    summary = _summary(cam)
    name, evidence = identify(flat, summary)
    profile = PROFILES[name]
    settings: dict[str, dict] = {}
    unsupported: list[str] = []
    locked: list[dict] = []
    seen: set[str] = set()
    for c in CANONICAL:
        key = c.key
        if not profile.supports(key):
            unsupported.append(key)
            continue
        if key in profile.menu_only:
            settings[key] = {"available": False, "camera_menu": profile.menu_only[key].format(value="…")}
            continue
        w = profile.resolve(key, flat)
        if w is None:
            settings[key] = {"available": False, "widget": None,
                             "note": f"this {profile.display} body does not expose it over USB"}
            continue
        if w in seen:                         # portable alias of a key already listed (Sony picture_style)
            continue
        seen.add(w)
        settings[key] = _entry(profile, key, w, flat[w])
        if flat[w].get("readonly"):
            reason = profile.lock_reason(key, flat)
            settings[key]["locked_reason"] = reason
            locked.append({"key": key, "widget": w, "reason": reason})

    controls: dict[str, dict] = {"capture": {"available": True}}
    for key in CONTROL_KEYS:
        if not profile.candidates(key):
            continue
        w = profile.control_widget(key, flat)
        controls[key] = {"available": w is not None, "widget": w}
    if profile.movie_unsupported:
        controls["movie"] = {"available": False, "widget": None, "note": profile.movie_unsupported}

    warnings: list[str] = []
    card = profile.card_status(flat)
    if card and card.get("warning"):
        warnings.append(card["warning"])
    if name == "canon" and "internal ram" in normalize(_value(flat, "capturetarget")):
        warnings.append("capturetarget is Internal RAM — camctl switches it to Memory card before each capture.")
    if name == "fuji" and "prioritymode" in flat and "usb" not in normalize(_value(flat, "prioritymode")):
        warnings.append("prioritymode is Camera — camctl claims USB control before exposure writes.")
    prohibit = {}
    for key, widget in (("movie", "movieprohibit"), ("live_view", "liveviewprohibit")):
        if widget in flat:
            prohibit[key] = _value(flat, widget)
            if _prohibited(prohibit[key]):
                warnings.append(f"{key.replace('_', ' ')} currently refused by the camera: {prohibit[key]}")

    out: dict[str, Any] = {
        "vendor": profile.name, "vendor_name": profile.display if name != "generic" else "Generic PTP",
        "detected_by": evidence, "tested_on_hardware": profile.tested,
        "manufacturer": _value(flat, "manufacturer") or None, "model": _value(flat, "cameramodel") or None,
        "firmware": _value(flat, "deviceversion") or None, "battery": _value(flat, "batterylevel") or None,
        "setup": profile.setup, "settings": settings, "unsupported": unsupported, "controls": controls,
        "locked": locked, "card": card, "warnings": warnings, "notes": list(profile.notes),
    }
    if prohibit:
        out["prohibit"] = prohibit
    return out
