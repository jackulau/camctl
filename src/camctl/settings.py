"""Canonical setting names and value matching.

libgphoto2 exposes camera properties under its own widget names ("f-number",
"shutterspeed", ...) with camera-specific choice strings ("f/2.8", "1/250",
"Auto ISO"). Presets use short canonical keys and human values; this module
resolves both against whatever the connected camera actually reports.

The candidates below are Sony's (the verified body). Other vendors' widget names,
value spellings and quirks live in vendors.py, which reuses these keys.

Any key that is not canonical is treated as a raw gphoto2 widget name, so a
preset can target properties discovered with `camctl config dump`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction


@dataclass(frozen=True)
class CanonicalKey:
    key: str
    candidates: tuple[str, ...]          # gphoto2 widget names, most likely first
    labels: tuple[str, ...] = ()         # fallback: match on widget label
    description: str = ""


# Order matters when applying: priority/mode first, then values whose choice lists depend on them
# (format → frame rate → bitrate; WB mode → Kelvin). Sony hex codes (dXXX) are PTP property codes,
# verified against an ILCE-7M5 fw 1.01 dump; libgphoto2 2.5.34 has no names for them.
CANONICAL: tuple[CanonicalKey, ...] = (
    CanonicalKey("priority_key", ("d25a",), (), "0 Camera / 1 PC Remote: who owns exposure mode."),
    CanonicalKey("exposure_program", ("500e", "expprogram", "exposureprogram"), ("exposure program",),
                 "P/A/S/M, Movie (P/A/S/M), S&Q. Remote change needs priority_key=PC Remote (auto-set); "
                 "lasts for the USB session — off-tether the physical dial rules."),
    CanonicalKey("exposure_program_dial", (), (), "Mode-dial position (read-only; Canon autoexposuremodedial)."),
    CanonicalKey("movie_format", ("d241", "movieformat"), (), "XAVC S 4K | XAVC HS 4K | XAVC S-I 4K | XAVC S HD"),
    CanonicalKey("movie_frame_rate", ("d286",), (), "24p | 30p | 60p | 120p ..."),
    CanonicalKey("movie_setting", ("d242", "moviequality"), (),
                 "Bitrate + sampling, e.g. '100M 4:2:2 10bit', or '4:2:2 10bit' for the best available."),
    CanonicalKey("movie_quality", (), (), "Movie size/frame-rate as one setting (Nikon moviequality)."),
    CanonicalKey("rec_media_movie", ("d160",), (), "Slot1 | Slot2 | Simultaneous"),
    CanonicalKey("rec_media_still", ("d15f",), (), "Slot1 | Slot2 | Simultaneous | Sort"),
    CanonicalKey("image_quality", ("imagequality", "imageformat"), ("image quality", "image format"),
                 "Still file format, e.g. RAW+JPEG."),
    CanonicalKey("raw_compression", ("d288", "rawcompression"), (),
                 "RAW file type: Lossless Compressed | Compressed (HQ) | Compressed | Uncompressed."),
    CanonicalKey("shutter", ("shutterspeed", "shutterspeed2"), ("shutter speed",), "Shutter speed, e.g. 1/250."),
    CanonicalKey("aperture", ("f-number", "aperture"), ("f-number", "aperture"), "Aperture, e.g. 2.8."),
    CanonicalKey("iso_auto", (), (), "Separate Auto ISO On/Off toggle (Nikon); preset iso: auto sets it."),
    CanonicalKey("iso", ("iso", "isospeed"), ("iso speed", "iso"), "ISO value or 'auto'."),
    CanonicalKey("iso_auto_min", ("d1b6",), (), "Auto ISO lower limit."),
    CanonicalKey("iso_auto_max", ("d1b7",), (), "Auto ISO upper limit."),
    CanonicalKey("exposure_comp", ("exposurecompensation", "exposurebiascompensation"),
                 ("exposure compensation", "exposure bias"), "Exposure compensation in EV."),
    CanonicalKey("white_balance", ("whitebalance",), ("white balance",), "auto | daylight | cloudy | color_temp | ..."),
    CanonicalKey("color_temp", ("colortemperature", "whitebalancecolortemperature"),
                 ("color temperature",), "Kelvin; requires white_balance=color_temp."),
    CanonicalKey("focus_mode", ("focusmode",), ("focus mode",), "AF-S | AF-C | AF-A | DMF | MF."),
    CanonicalKey("focus_area", ("focusarea", "focusareamode"), ("focus area",), "Wide | Zone | Tracking ..."),
    CanonicalKey("subject_recognition", ("d157",), (), "Subject Recognition in AF: On | Off."),
    CanonicalKey("recognition_target", ("d158",), (), "Auto | Human | Animal+Bird | Animal | Bird | ..."),
    CanonicalKey("eye_select", ("d159",), (), "Right/Left Eye Select: Auto | Right | Left."),
    CanonicalKey("metering", ("exposuremetermode", "meteringmode"), ("metering mode", "exposure metering mode"),
                 "Multi | Center | Spot ..."),
    CanonicalKey("drive", ("capturemode", "stillcapturemode", "drivemode"), ("still capture mode", "drive mode"),
                 "Single | Continuous Hi | Continuous Mid ..."),
    CanonicalKey("anti_flicker", ("d133",), (), "Anti-flicker shooting: On | Off."),
    CanonicalKey("steadyshot", ("imagestabilization", "d0d9"), ("image stabilization",), "Still SteadyShot On/Off."),
    CanonicalKey("steadyshot_movie", ("d0da",), (), "Off | Standard | Active | Dynamic Active."),
    CanonicalKey("picture_profile", ("d23f", "pictureprofile"), ("picture profile",),
                 "Off | PP1..PP6 | PP10 (HLG2) | PP11 | LUT1..LUT4. A7 V has no S-Log PPs (7-9)."),
    CanonicalKey("creative_look", ("d0fa",), (), "ST | PT | NT | VV | VV2 | FL | IN | SH | BW | SE | FL2 | FL3."),
    # Sony candidates (d0fa, d23f) live in vendors.py so WIDGET_KEY / `config explain` keep their own keys.
    CanonicalKey("picture_style", (), (),
                 "Portable look: Sony Creative Look, Canon Picture Style, Nikon Picture Control, Fuji Film Simulation."),
    CanonicalKey("monitor_lut", ("d04d",), (), "A7 V Log preview LUT on screen: On | Off (camera turns it on with Log)."),
    CanonicalKey("gamma_display_assist", ("d1fe",), (), "Show Log footage as Rec.709 on screen: On | Off."),
    CanonicalKey("gamma_display_assist_type", ("d1ff",), (), "Auto | S-Log3→709 | HLG(BT.2020) | HLG(709)."),
    CanonicalKey("capture_target", ("capturetarget",), (), "Where captures are stored: Memory card | Internal RAM."),
    # Menu-only on A7 V fw 1.01: property codes e0e3/e0e4 are not advertised over PTP and writes are ignored.
    CanonicalKey("log_shooting", (), (), "Off | On (Flexible ISO). Camera menu only."),
    CanonicalKey("log_gamut", (), (), "S-Gamut3.Cine | S-Gamut3. Camera menu only."),
)

# Settings the camera does not expose remotely → reported as a camera-menu step, never as a failure.
MENU_ONLY = {
    # A7 V help guide: the item only exists with the Photo/Movie/S&Q switch on Movie or S&Q.
    "log_shooting": ("Switch → Movie, then MENU → Shooting → Image Quality/Rec → Log Shooting Setting → "
                     "Log Shooting: {value}"),
    "log_gamut": "Switch → Movie, then MENU → Shooting → Image Quality/Rec → Log Shooting Setting → Color Gamut: {value}",
}

# code → label for Sony hex properties (value strings as libgphoto2 reports them).
ENUMS: dict[str, dict[str, str]] = {
    "priority_key": {"0": "Camera", "1": "PC Remote"},
    # 500e values verified live on ILCE-7M5 fw 1.01 (writing "1" → M, shutter/ISO unlock).
    "exposure_program": {"294912": "Intelligent Auto", "65538": "P", "131075": "A", "196612": "S", "1": "M",
                         "491604": "Movie (Auto)", "491600": "Movie (P)", "491601": "Movie (A)",
                         "491602": "Movie (S)", "491603": "Movie (M)", "622685": "S&Q (Auto)",
                         "622681": "S&Q (P)", "622682": "S&Q (A)", "622683": "S&Q (S)", "622684": "S&Q (M)"},
    "movie_format": {"8": "XAVC S 4K", "9": "XAVC S HD", "11": "XAVC HS 4K", "14": "XAVC S-I 4K",
                     "15": "XAVC S-I HD"},
    "movie_frame_rate": {"1": "120p", "2": "100p", "3": "60p", "4": "50p", "5": "30p", "6": "25p", "7": "24p",
                         "8": "23.98p", "9": "29.97p", "10": "59.94p"},
    "movie_setting": {str(k): v for k, v in {
        38: "600M 4:2:2 10bit", 39: "500M 4:2:2 10bit", 40: "400M 4:2:0 10bit", 41: "300M 4:2:2 10bit",
        42: "280M 4:2:2 10bit", 43: "250M 4:2:2 10bit", 44: "240M 4:2:2 10bit", 45: "222M 4:2:2 10bit",
        46: "200M 4:2:2 10bit", 47: "200M 4:2:0 10bit", 48: "200M 4:2:0 8bit", 49: "185M 4:2:2 10bit",
        50: "150M 4:2:0 10bit", 51: "150M 4:2:0 8bit", 52: "140M 4:2:2 10bit", 53: "111M 4:2:2 10bit",
        54: "100M 4:2:2 10bit", 55: "100M 4:2:0 10bit", 56: "100M 4:2:0 8bit", 57: "93M 4:2:2 10bit",
        58: "89M 4:2:2 10bit", 59: "75M 4:2:0 10bit", 60: "60M 4:2:0 8bit", 61: "50M 4:2:2 10bit",
        62: "50M 4:2:0 10bit", 63: "50M 4:2:0 8bit", 64: "45M 4:2:0 10bit", 65: "30M 4:2:0 10bit",
        66: "25M 4:2:0 8bit", 67: "16M 4:2:0 8bit"}.items()},
    "rec_media_movie": {"1": "Slot1", "2": "Slot2", "257": "Simultaneous"},
    "rec_media_still": {"1": "Slot1", "2": "Slot2", "257": "Simultaneous", "258": "Sort"},
    "subject_recognition": {"1": "Off", "2": "On"},
    "recognition_target": {"65535": "Auto", "1": "Human", "2": "Animal+Bird", "3": "Animal", "4": "Bird",
                           "5": "Insect", "6": "Car/Train", "7": "Plane"},
    "eye_select": {"1": "Auto", "2": "Right", "3": "Left"},
    "anti_flicker": {"1": "Off", "2": "On"},
    "steadyshot_movie": {"1": "Off", "2": "Standard", "3": "Active", "4": "Dynamic Active"},
    "picture_profile": {"0": "Off", **{str(i): f"PP{i}" for i in (1, 2, 3, 4, 5, 6, 10, 11)},
                        **{str(64 + i): f"LUT{i}" for i in range(1, 5)}},
    "monitor_lut": {"1": "Off", "2": "On"},
    "gamma_display_assist": {"1": "Off", "2": "On"},
    "gamma_display_assist_type": {"1": "Auto", "771": "S-Log3→709", "1025": "HLG(BT.2020)", "1026": "HLG(709)"},
    "creative_look": {"1": "ST", "2": "PT", "3": "NT", "4": "VV", "5": "VV2", "6": "FL", "7": "IN", "8": "SH",
                      "9": "BW", "10": "SE", "11": "FL2", "12": "FL3",
                      **{str(256 + i): f"Custom{i}" for i in range(1, 7)}},
    "log_shooting": {"1": "Off", "1281": "On (Flexible ISO)"},
    "log_gamut": {"1": "S-Gamut3.Cine", "2": "S-Gamut3"},
    # Sony CrRAWFileCompressionType 0-6. The A7 V offers 1/6/7 = its three menu entries (help guide: Lossless
    # Comp, Compressed(HQ), Compressed), so 7 is its Lossless code; 6 cut remaining shots vs 1/7 (2026-09-28).
    "raw_compression": {"0": "Uncompressed", "1": "Compressed", "2": "Lossless Compressed",
                        "3": "Lossless Compressed (S)", "4": "Lossless Compressed (M)",
                        "5": "Lossless Compressed (L)", "6": "Compressed (HQ)", "7": "Lossless Compressed"},
}

# Changing these can re-populate other widgets' choice lists → re-read config after writing.
REFLOW_KEYS = {"priority_key", "exposure_program", "movie_format", "movie_frame_rate", "white_balance"}


def enum_code(key: str, wanted: object, choices: list[str],
              table: dict[str, str] | None = None) -> MatchResult:
    """Translate a human label to the Sony code, restricted to codes the camera offers right now."""
    table = ENUMS[key] if table is None else table
    want = normalize(wanted)
    if str(wanted) in choices:
        return MatchResult(str(wanted), True)
    alias = {"on": "on (flexible iso)", "true": "on", "false": "off", "dual": "simultaneous",
             "both": "simultaneous", "s-gamut3cine": "s-gamut3.cine", "cine": "s-gamut3.cine",
             "lossless": "lossless compressed", "compressed hq": "compressed (hq)",
             "compressed(hq)": "compressed (hq)"}
    labels = {normalize(v) for v in table.values()}
    if want not in labels and alias.get(want) in labels:
        want = alias[want]
    exact = [c for c, label in table.items() if normalize(label) == want]
    avail = [c for c in exact if c in choices]
    if avail:
        return MatchResult(avail[0], True)
    if exact:
        offered = [table.get(c, c) for c in choices]
        return MatchResult(None, False, f"'{wanted}' not offered in current mode; camera offers {offered}",
                           alternatives=offered)
    # Partial pattern → best bitrate among matches, e.g. "4:2:2 10bit" → highest-Mbps 4:2:2 10bit available.
    # (whole leading token only: "50M" must not pick "250M ...")
    partial = [c for c in choices if c in table and re.search(r"(?:^|\s)" + re.escape(want), normalize(table[c]))]
    if partial:
        best = max(partial, key=lambda c: float(re.match(r"(\d+)", table[c]).group(1))
                   if re.match(r"\d", table[c]) else 0)
        return MatchResult(best, True, f"chose {table[best]}")
    offered = [table.get(c, c) for c in choices]
    return MatchResult(None, False, f"'{wanted}' unknown; camera offers {offered}", alternatives=offered)


def label_for(key: str, code: object, table: dict[str, str] | None = None) -> str:
    return (ENUMS.get(key, {}) if table is None else table).get(str(code), str(code))


CANONICAL_BY_KEY = {c.key: c for c in CANONICAL}
APPLY_ORDER = {c.key: i for i, c in enumerate(CANONICAL)}
# Raw widget name → canonical key, so dump files (raw names) get the same ordering and mode handling.
WIDGET_KEY: dict[str, str] = {}
for _c in CANONICAL:
    for _w in _c.candidates:
        WIDGET_KEY.setdefault(_w, _c.key)
# Continuous numeric settings where snapping to the nearest camera value is meaningful (not enum codes).
NUMERIC_KEYS = {"shutter", "aperture", "iso", "iso_auto_min", "iso_auto_max", "exposure_comp"}
LOG_KEYS = {"shutter", "aperture", "iso", "iso_auto_min", "iso_auto_max"}


def canonical_key(key: str) -> str:
    return key if key in CANONICAL_BY_KEY else WIDGET_KEY.get(key, key)

# Human value → acceptable choice spellings (all lower-case, compared after normalisation).
SYNONYMS: dict[str, dict[str, tuple[str, ...]]] = {
    "focus_mode": {
        "af-c": ("af-c", "continuous af", "af continuous", "automatic continuous", "continuous", "af-c continuous"),
        "af-s": ("af-s", "single af", "one shot", "automatic", "single"),
        "af-a": ("af-a", "automatic af", "af automatic"),
        "dmf": ("dmf", "direct manual focus"),
        "mf": ("manual", "mf", "manual focus"),
    },
    "white_balance": {
        "auto": ("automatic", "auto", "awb", "auto white balance"),
        "daylight": ("daylight", "sunny", "fine weather"),
        "cloudy": ("cloudy", "cloud"),
        "shade": ("shade",),
        "tungsten": ("tungsten", "incandescent"),
        "flash": ("flash",),
        "color_temp": ("color temperature", "choose color temperature", "colour temperature", "kelvin", "c.temp"),
        "custom": ("preset 1", "custom 1", "custom"),
    },
    "metering": {
        "multi": ("multi", "multi spot", "multi-segment", "matrix", "evaluative", "multi pattern"),
        "center": ("center", "center weighted", "center-weighted", "centre weighted", "center weighted avg"),
        "spot": ("spot", "spot standard"),
        "highlight": ("highlight",),
        "average": ("entire screen avg", "average"),
    },
    "drive": {
        "single": ("single", "single shooting", "single frame"),
        "continuous_hi": ("continuous hi", "continuous high", "cont. shooting hi", "continuous shooting hi"),
        "continuous_mid": ("continuous mid", "continuous medium", "cont. shooting mid", "continuous shooting mid"),
        "continuous_lo": ("continuous lo", "continuous low", "cont. shooting lo", "continuous shooting lo"),
    },
    "image_quality": {
        "raw+jpeg": ("raw+jpeg", "raw + jpeg", "raw & jpeg", "raw+jpg", "raw+jpeg fine", "raw+jpeg extra fine"),
        "raw": ("raw", "raw only"),
        "jpeg": ("jpeg", "jpg", "jpeg fine", "fine", "extra fine"),
        "raw+heif": ("raw+heif", "raw + heif", "raw & heif"),
        "heif": ("heif",),
    },
    "raw_compression": {
        "lossless": ("lossless compressed", "lossless", "lossless (l)", "lossless compressed l"),
        "compressed_hq": ("compressed (hq)", "compressed(hq)", "compressed hq"),
        "compressed": ("compressed",),
        "uncompressed": ("uncompressed",),
    },
    "picture_profile": {
        "off": ("off", "none"),
    },
}


def normalize(value: object) -> str:
    s = str(value).strip().lower()
    s = s.replace("_", " ").replace("–", "-")
    return re.sub(r"\s+", " ", s)


def _parse_number(text: str) -> float | None:
    """Parse '1/250', '0.5', '2.8', 'f/2.8', 'F4', '5600K', '+0.7', '1/250s' → float."""
    s = normalize(text)
    s = re.sub(r"^f/?\s*", "", s)                # aperture prefixes
    s = re.sub(r"\s*(k|s|sec|ev|\")$", "", s)    # unit suffixes
    s = s.replace(" ", "")
    if not s:
        return None
    try:
        v = float(Fraction(s)) if "/" in s else float(s)
    except (ValueError, ZeroDivisionError):
        return None
    return v if math.isfinite(v) else None


@dataclass
class MatchResult:
    value: str | float | int | None
    exact: bool
    note: str = ""
    alternatives: list[str] = field(default_factory=list)


def match_choice(key: str, wanted: object, choices: list[str]) -> MatchResult:
    """Pick the camera choice string that best represents `wanted`.

    Tries, in order: exact normalised match, synonym table, 'auto' keyword,
    numeric equality (1% tolerance), nearest numeric (flagged as inexact).
    """
    if not choices:
        return MatchResult(None, False, "widget has no choices")
    if isinstance(wanted, bool):                 # YAML 1.1: unquoted on/off load as booleans
        wanted = "on" if wanted else "off"
    want = normalize(wanted)
    norm = {normalize(c): c for c in choices}

    if want in norm:
        return MatchResult(norm[want], True)

    for canon, spellings in SYNONYMS.get(key, {}).items():
        if want == normalize(canon) or want in spellings:
            for sp in (normalize(canon), *spellings):
                if sp in norm:
                    return MatchResult(norm[sp], True)
            for n, orig in norm.items():         # partial: "color temperature" in "choose color temperature"
                if any(sp in n for sp in spellings):
                    return MatchResult(orig, True)

    if want in ("auto", "automatic", "auto iso", "iso auto"):
        for n, orig in norm.items():
            if "auto" in n:
                return MatchResult(orig, True)

    target = _parse_number(want)
    if target is not None:
        numeric = [(c, _parse_number(c)) for c in choices]
        numeric = [(c, v) for c, v in numeric if v is not None]
        for c, v in numeric:
            if math.isclose(v, target, rel_tol=0.01, abs_tol=1e-9):
                return MatchResult(c, True)
        if numeric and canonical_key(key) in NUMERIC_KEYS:
            # Nearest on a log scale (stops) for exposure values, linear otherwise.
            log_scale = canonical_key(key) in LOG_KEYS and target > 0
            def dist(v: float) -> float:
                if log_scale and v > 0:
                    return abs(math.log2(v) - math.log2(target))
                return abs(v - target)
            best, _ = min(numeric, key=lambda cv: dist(cv[1]))
            return MatchResult(best, False, f"no exact '{wanted}', snapped to nearest '{best}'")

    return MatchResult(None, False, f"'{wanted}' not among choices",
                       alternatives=choices[:40])


def resolve_widget(key: str, flat: dict[str, dict], candidates: tuple[str, ...] | None = None) -> str | None:
    """Map a canonical key (or raw widget name) to a widget name present in `flat`.

    `candidates` overrides the (Sony) default widget names, e.g. with a vendor profile's.
    """
    if key in flat:
        return key
    canon = CANONICAL_BY_KEY.get(key)
    if canon is None:
        return None
    for name in (canon.candidates if candidates is None else candidates):
        if name in flat:
            return name
    for name, info in flat.items():
        if normalize(info.get("label", "")) in canon.labels:
            return name
    return None


def sort_for_apply(keys: list[str]) -> list[str]:
    return sorted(keys, key=lambda k: APPLY_ORDER.get(canonical_key(k), len(APPLY_ORDER)))
