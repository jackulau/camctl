"""Transport-independent operations shared by the CLI and the MCP server."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import vendors
from .paths import CONFIG_DIR
from .settings import CANONICAL_BY_KEY, ENUMS, enum_code, label_for, match_choice
from .vendors import VendorProfile

STATE_DIR = CONFIG_DIR
BACKUP_DIR = STATE_DIR / "backups"

# Widgets that are actions/volatile, never restored by `config load`.
NEVER_RESTORE = {"autofocus", "manualfocus", "capture", "bulb", "movie", "opcode", "spotfocusarea",
                 "batterylevel", "focusindication", "zoom", "focalposition", "focaldistancemeters",
                 "serialnumber", "manufacturer", "cameramodel", "deviceversion", "vendorextension",
                 "focusmagnifier",
                 "expprogram",                  # named 500e duplicate; writes always fail ("Bad parameters")
                 "d223",                        # DateTime_Settings
                 } | vendors.SONY_CONTROLS      # Sony SDIO controls (buttons / one-shot operations), not settings
# Other vendors: `expprogram` is their real exposure-mode widget (restorable when writable); these are actions.
NEVER_RESTORE_OTHER = (NEVER_RESTORE - {"expprogram"}) | {
    "eosremoterelease", "eosmoviemode", "viewfinder", "movierecordtarget", "autofocusdrive", "manualfocusdrive",
    "cancelautofocus", "autoexposuremodedial", "movieprohibit", "liveviewprohibit", "eoszoom",
    "eoszoomposition", "prioritymode"}


class Backend(Protocol):
    def config(self) -> dict[str, dict[str, Any]]: ...
    def set(self, name: str, value: Any) -> None: ...


@dataclass
class Step:
    key: str
    widget: str | None
    wanted: Any
    current: Any = None
    target: Any = None
    # ok | unchanged | would-set | camera-menu | unsupported | readonly | missing | no-match | error
    status: str = "pending"
    note: str = ""


NOT_PROBLEMS = ("ok", "would-set", "unchanged", "camera-menu", "unsupported")


@dataclass
class ApplyResult:
    steps: list[Step] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    blocked_by_dial: bool = False
    vendor: str = ""
    # key → enum table used for code → label display; None = Sony ENUMS by key (pre-vendor behaviour)
    tables: dict[str, Any] | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        for st in self.steps:
            table = ENUMS.get(st.key) if self.tables is None else self.tables.get(st.key)
            if table:
                st.current = label_for(st.key, st.current, table) if st.current is not None else None
                st.target = label_for(st.key, st.target, table) if st.target is not None else None
        out = {
            "applied": [s.__dict__ for s in self.steps if s.status in ("ok", "would-set")],
            "unchanged": [s.key for s in self.steps if s.status == "unchanged"],
            "problems": [s.__dict__ for s in self.steps if s.status not in NOT_PROBLEMS],
            "unsupported": [{"key": s.key, "note": s.note} for s in self.steps if s.status == "unsupported"],
            "manual_on_camera": self.manual,
            "blocked_by_mode_dial": self.blocked_by_dial,
        }
        if self.vendor:
            out["vendor"] = self.vendor
        return out


def _resolve_target(key: str, wanted: Any, info: dict, table: Any = None,
                    matcher: Any = match_choice) -> tuple[Any, str, bool]:
    """→ (camera value, note, exact). `table`: enum codes (Sony dXXX); `matcher`: vendor value matcher."""
    if key in ENUMS and table is None and matcher is match_choice:
        table = ENUMS[key]                      # legacy call without a vendor: Sony tables
    if table and "choices" in info:
        m = enum_code(key, wanted, info["choices"], table)
        return m.value, m.note, m.exact
    if "choices" in info:
        m = matcher(key, wanted, info["choices"])
        return m.value, m.note, m.exact
    if "range" in info:
        lo, hi, step = info["range"]
        try:
            v = float(wanted)
        except (TypeError, ValueError):
            return None, f"'{wanted}' is not a number; range is {lo:g}..{hi:g}", False
        if step:
            v = round(v / step) * step
        clamped = min(max(v, lo), hi)
        return clamped, ("" if clamped == float(wanted) else f"clamped/rounded to {clamped}"), clamped == float(wanted)
    if info.get("type") == "toggle":
        return int(bool(wanted) and str(wanted).lower() not in ("off", "false", "0")), "", True
    return wanted, "", True


def _same(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def apply_settings(cam: Backend, settings: dict[str, Any], dry_run: bool = False,
                   manual: list[str] | None = None, vendor: VendorProfile | None = None) -> ApplyResult:
    """Apply canonical/raw settings in dependency order; never raises for per-key problems.

    The camera's vendor profile (detected unless given) maps canonical keys to its widgets and values;
    keys the vendor doesn't expose over USB are reported as "unsupported", never as failures.
    """
    flat = cam.config()
    vendor = vendor or vendors.for_camera(cam, flat)
    res = ApplyResult(manual=list(manual or []), vendor=vendor.name, tables={})
    settings = vendor.expand(settings, flat)
    mode_changed = False
    for key in vendor.sort_for_apply(list(settings)):
        wanted = settings[key]
        ckey = vendor.canonical_key(key)            # raw widget names from dumps behave like their canonical key
        widget = vendor.resolve(key, flat)
        step = Step(key, widget, wanted)
        res.steps.append(step)
        if widget is None and key in vendor.menu_only:
            step.status, step.note = "camera-menu", vendor.menu_only[key].format(value=wanted)
            res.manual.append(step.note)
            continue
        if widget is None and ckey in CANONICAL_BY_KEY and not vendor.supports(ckey):
            step.status, step.note = "unsupported", f"not available on {vendor.display} over USB"
            continue
        if widget is None:
            step.status = "missing"
            step.note = "camera does not expose this over PTP; set it on the camera"
            continue
        value, camera_step = vendor.translate(ckey, wanted)
        if camera_step is not None:
            step.status, step.note = "camera-menu", camera_step
            res.manual.append(camera_step)
            continue
        table = vendor.enum_table(key, widget)
        if table:
            res.tables[key] = table
        try:
            info = cam.widget(widget) if hasattr(cam, "widget") else flat[widget]
        except Exception as e:  # noqa: BLE001 — report per key
            step.status, step.note = "error", str(e)
            continue
        fresh = vendor.unlock_exposure(cam, ckey, widget, info, flat, dry_run)   # Sony priority key, Fuji USB
        if fresh is not None:
            info = fresh
        step.current = info.get("value")
        target, note, _exact = _resolve_target(key, value, info, table, vendor.match)
        step.target, step.note = target, note
        if target is not None and _same(step.current, target):
            step.status = "unchanged"
            continue
        if info.get("readonly") and ckey in vendor.physical:     # owned by a dial: a camera step, not a failure
            step.status = "camera-menu"
            step.note = vendor.physical_step(ckey, value)
            res.manual.append(step.note)
            continue
        if target is None:
            step.status = "no-match"
            continue
        if info.get("readonly") and mode_changed and hasattr(cam, "drain_events"):
            for _ in range(6):                      # camera unlocks exposure widgets ~0.5 s after a mode change
                cam.drain_events(0.5)
                info = cam.widget(widget)
                if not info.get("readonly"):
                    break
        if info.get("readonly"):
            step.status = "readonly"
            reason = vendor.context_lock(ckey, flat) or vendor.readonly_note(ckey)   # e.g. WB not Color Temp
            step.note = (note + "; " if note else "") + reason
            if ckey in vendor.dial_keys:
                res.blocked_by_dial = True
            continue
        if dry_run:
            step.status = "would-set"
            continue
        try:
            cam.set(widget, target)
            step.status = "ok"
            flat[widget] = {**flat[widget], "value": target}   # later keys' lock reasons see this write
            mode_changed |= ckey in ("priority_key", "exposure_program")
        except Exception as e:  # noqa: BLE001 — report per key
            step.status = "error"
            step.note = str(e)
            if ckey == "exposure_program":
                res.blocked_by_dial = True
        # dependent widgets are re-read individually via cam.widget() on their turn
    if res.blocked_by_dial:
        res.manual.insert(0, vendor.dial_message)
    return res


# -- dump / load / diff -----------------------------------------------------

def dump(cam: Backend, path: Path | None = None, meta: dict | None = None) -> Path:
    flat = cam.config()
    model = flat.get("cameramodel", {}).get("value", "camera")
    vendor = vendors.for_camera(cam, flat).name
    if path is None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        path = BACKUP_DIR / f"{model}-{dt.datetime.now():%Y%m%d-%H%M%S}.yaml"
    doc = {"camctl_dump": 1, "model": model, "vendor": vendor,
           "taken": dt.datetime.now().isoformat(timespec="seconds"), **(meta or {}), "properties": flat}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(json.dumps(doc, indent=1, default=str))
    else:
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=200))
    return path


def read_settings_file(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Accepts a dump (full property map) or a preset (settings: + manual:). → (settings, manual)."""
    text = path.read_text()
    doc = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if "properties" in doc:
        props = doc["properties"]
        # dumps without a vendor field predate multi-vendor support → Sony
        skip = NEVER_RESTORE if doc.get("vendor", "sony") in ("sony", "generic") else NEVER_RESTORE_OTHER
        settings = {k: v["value"] for k, v in props.items()
                    if not v.get("readonly") and k not in skip and v.get("type") != "button"
                    and v.get("value") is not None}
        return settings, []
    if "settings" in doc:
        return dict(doc["settings"]), list(doc.get("manual", []))
    # bare original-*.json from the first probe: {widget: {value,...}}
    if all(isinstance(v, dict) and "value" in v for v in doc.values()):
        return {k: v["value"] for k, v in doc.items() if not v.get("readonly") and k not in NEVER_RESTORE}, []
    raise ValueError(f"{path}: not an camctl dump or preset")


def diff(cam: Backend, settings: dict[str, Any], vendor: VendorProfile | None = None) -> list[dict]:
    flat = cam.config()
    vendor = vendor or vendors.for_camera(cam, flat)
    out = []
    for key, wanted in settings.items():
        w = vendor.resolve(key, flat)
        cur = flat[w]["value"] if w else None
        value = vendor.translate(vendor.canonical_key(key), wanted)[0]
        target = (_resolve_target(key, value, flat[w], vendor.enum_table(key, w), vendor.match)[0]
                  if w else None)                   # labels → camera values
        if w is None or not _same(cur, target):
            out.append({"key": key, "widget": w, "camera": cur, "file": wanted,
                        "readonly": flat[w]["readonly"] if w else None})
    return out


def current_settings(cam: Backend, vendor: VendorProfile | None = None) -> dict[str, Any]:
    """Canonical view of what the camera is set to now (keys the vendor exposes; one key per widget)."""
    flat = cam.config()
    vendor = vendor or vendors.for_camera(cam, flat)
    out: dict[str, Any] = {}
    seen: set[str] = set()
    for key in CANONICAL_BY_KEY:
        w = vendor.resolve(key, flat)
        if w and w not in seen:
            seen.add(w)
            table = vendor.enum_table(key, w)
            value = label_for(key, flat[w]["value"], table) if table else flat[w]["value"]
            out[key] = {"value": value, "writable": not flat[w]["readonly"], "widget": w}
    for extra in ("batterylevel", "cameramodel", "deviceversion", "capturetarget", "prioritymode",
                  "imagestabilization", "shuttertype", "silentmode", "jpegquality", "imagesize", "aspectratio"):
        if extra in flat:
            out[extra] = {"value": flat[extra]["value"], "writable": not flat[extra]["readonly"], "widget": extra}
    return out


class Snapshot:
    """One config walk shared by several read-only probes (capabilities + current_settings); other attributes
    (summary, vendor, ...) pass through to the live camera."""

    def __init__(self, cam: Any) -> None:
        self._cam = cam
        self._flat = cam.config()

    def config(self) -> dict[str, dict[str, Any]]:
        return self._flat

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cam, name)
