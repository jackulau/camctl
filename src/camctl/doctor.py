"""`camctl doctor` and the MCP `camera_doctor` tool: one read-only health check of USB, Bluetooth and Drive.

Never writes camera settings and never connects over Bluetooth (a scan only listens, and only with ble_scan=True).
fix=True only stops macOS ptpcamerad, which macOS relaunches on demand. Nothing here prints: callers render the
report (the MCP server's stdout is its JSON-RPC channel).

Report: {"ok", "problems", "warnings", "usb": {...}, "ble": {...}, "drive": {...}}. ok is False only for problems
that stop camctl working (no camera, camera won't open, card full, Bluetooth scan failing); warnings don't fail it.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

from . import core, vendors

# Apps that open the camera over USB and keep it (macOS ptpcamerad backs Image Capture / Photos imports).
HOLDER_PATTERN = "ptpcamerad|Image Capture|Imaging Edge|Imaging Capture|^Photos$"
USB_CHECKS = (
    "Use a data-capable USB cable (charge-only cables show no camera) plugged straight into the Mac.",
    "Camera on and awake, USB mode set for computer control (per-vendor menus below).",
    "Quit apps that grab the camera: Image Capture, Photos, Imaging Edge / Imaging Capture, Lightroom/Capture One "
    "tethering; `camctl doctor --fix` stops macOS ptpcamerad.",
)
LOW_BATTERY = 15


def _holders() -> list[str]:
    r = subprocess.run(["pgrep", "-l", HOLDER_PATTERN], capture_output=True, text=True)
    return [h for h in r.stdout.splitlines() if h.strip()]


def _battery_percent(text: Any) -> int | None:
    m = re.search(r"(\d+)\s*%", str(text or ""))
    return int(m.group(1)) if m else None


def camera_summary(caps: dict) -> dict[str, Any]:
    """The doctor's view of vendors.capabilities(): identity, what can be set, what can't and why."""
    s = caps["settings"]
    writable = sorted(k for k, v in s.items() if v.get("writable"))
    out = {k: caps.get(k) for k in ("vendor", "vendor_name", "detected_by", "tested_on_hardware", "manufacturer",
                                    "model", "firmware", "battery", "setup")}
    out.update({
        "writable": writable, "writable_count": len(writable),
        "locked": [{"key": x["key"], "reason": x["reason"]} for x in caps["locked"]],
        "unsupported": list(caps["unsupported"]),
        "not_exposed": sorted(k for k, v in s.items() if v.get("available") is False and "camera_menu" not in v),
        "camera_menu": {k: v["camera_menu"] for k, v in s.items() if "camera_menu" in v},
        "controls": {k: bool(v.get("available")) for k, v in caps["controls"].items()},
        "card": caps["card"], "warnings": list(caps["warnings"]), "prohibit": dict(caps.get("prohibit") or {}),
        "notes": list(caps["notes"]),
    })
    return out


def _usb(camera: Callable[[], Any], port: str | None, fix: bool, problems: list[str],
         warnings: list[str]) -> dict[str, Any]:
    from .offload import find_cards
    from .ptp import autodetect

    out: dict[str, Any] = {"camera_holders": _holders(), "fix_notes": []}
    if fix and any("ptpcamerad" in h for h in out["camera_holders"]):
        from .ptp import release_macos_holder
        stopped = release_macos_holder()
        out["stopped_ptpcamerad"] = stopped
        out["fix_notes"].append("stopped ptpcamerad (macOS restarts it on next plug; run again if needed)" if stopped
                                else "could not stop ptpcamerad (try: pkill -KILL ptpcamerad)")
    if fix and any("ptpcamerad" not in h for h in out["camera_holders"]):
        out["fix_notes"].append("Quit Image Capture / Imaging Edge yourself — doctor --fix only stops ptpcamerad")
    out["cards"] = [str(c) for c in find_cards()]
    out["ptp_cameras"] = [list(c) for c in autodetect()]
    out["camera"] = None
    holder_tip = (" — another app may hold it: run `camctl doctor --fix` or quit Image Capture / Photos"
                  if out["camera_holders"] else "")
    if out["ptp_cameras"] or port:
        try:
            with camera() as cam:
                out["camera"] = camera_summary(vendors.capabilities(core.Snapshot(cam)))
        except Exception as e:  # noqa: BLE001 — reported, never raised
            out["error"] = " ".join(str(e).split()) or type(e).__name__
            problems.append(f"USB camera found but it could not be opened: {out['error']}{holder_tip}")
    if out["camera"] is None:
        out["checks"] = list(USB_CHECKS)
        out["setup_hints"] = vendors.setup_hints()
        if not out["ptp_cameras"] and not port:
            if out["cards"]:
                warnings.append(f"No PTP camera, but a card is mounted ({', '.join(out['cards'])}): fine for "
                                "`camctl offload`; switch the camera's USB mode back to control it.")
            else:
                problems.append("No USB camera detected — see the checks and per-vendor USB setup.")
        return out
    cam = out["camera"]
    card = cam["card"] or {}
    for w in cam["warnings"]:
        if card.get("remaining_shots") == 0 and not card.get("remaining_movie_seconds") and w == card.get("warning"):
            problems.append(w)
        else:
            warnings.append(w)
    pct = _battery_percent(cam["battery"])
    if pct is not None and pct < LOW_BATTERY:
        warnings.append(f"Battery at {pct}% — charge or swap it before a long session.")
    return out


async def _ble(scan: bool, timeout: float, problems: list[str], warnings: list[str]) -> dict[str, Any]:
    from . import ble
    from .ble import bond

    default = ble.get_device()
    devices = [{"address": d.address, "name": d.name, "brand": d.brand,
                "default": bool(default and d.address == default.address)} for d in ble.devices()]
    out: dict[str, Any] = {"devices": devices, "default": default.address if default else None}
    try:
        out["daemon"] = await ble.daemon_status()
    except Exception as e:  # noqa: BLE001 — a broken socket must not break doctor
        out["daemon"] = None
        warnings.append(f"BLE daemon socket error: {e}")
    if out["daemon"] and out["daemon"].get("last_error"):
        warnings.append(f"BLE daemon: {out['daemon']['last_error']}")
    out["blueutil"] = bond.find_blueutil()
    needs_bond = any(ble.DRIVERS[d["brand"]].needs_bond for d in devices if d["brand"] in ble.DRIVERS)
    if out["blueutil"] is None and needs_bond:
        warnings.append("blueutil not installed — `brew install blueutil` lets `camctl ble pair --allow-bt-restart` "
                        "clear a stale macOS Bluetooth bond by itself.")
    if scan:
        try:
            out["scan"] = [f.to_dict() for f in await ble.scan(timeout)]
        except Exception as e:  # noqa: BLE001
            out["scan_error"] = " ".join(str(e).split()) or type(e).__name__
            problems.append(f"Bluetooth scan failed: {out['scan_error']}")
    return out


def _drive(warnings: list[str]) -> dict[str, Any]:
    try:
        from .drive import Drive
        return {"ok": True, "mode": Drive().mode}
    except Exception as e:  # noqa: BLE001
        msg = " ".join(str(e).split()) or type(e).__name__
        warnings.append(f"Drive not authorised, so `camctl offload` can't upload: {msg}")
        return {"ok": False, "error": msg}


async def run(camera: Callable[[], Any], *, port: str | None = None, fix: bool = False, ble_scan: bool = False,
              scan_timeout: float = 6.0) -> dict[str, Any]:
    """Full report. `camera` returns a fresh, unopened camera session (context manager)."""
    problems: list[str] = []
    warnings: list[str] = []
    usb = _usb(camera, port, fix, problems, warnings)
    ble = await _ble(ble_scan, scan_timeout, problems, warnings)
    drive = _drive(warnings)
    return {"ok": not problems, "problems": problems, "warnings": warnings, "usb": usb, "ble": ble, "drive": drive}


# -- text rendering (CLI) ----------------------------------------------------------------------------------

def _yes(v: Any) -> str:
    return {True: "yes", False: "no", None: "—"}.get(v, str(v))


def render(r: dict[str, Any]) -> str:
    usb, ble, drive = r["usb"], r["ble"], r["drive"]
    lines = ["USB"]
    cam = usb.get("camera")
    if cam:
        ident = " ".join(x for x in (cam["vendor_name"], cam["model"]) if x)
        facts = ", ".join(x for x in (f"fw {cam['firmware']}" if cam["firmware"] else "",
                                      f"battery {cam['battery']}" if cam["battery"] else "") if x)
        tested = "verified on hardware" if cam["tested_on_hardware"] else "untested on hardware"
        lines.append(f"  ✓ {ident}" + (f" ({facts})" if facts else "") + f" — {tested}; {cam['detected_by']}")
        lines.append(f"    {cam['writable_count']} writable settings, {len(cam['locked'])} locked")
        for x in cam["locked"]:
            lines.append(f"      {x['key']:<20} {x['reason']}")
        if cam["unsupported"]:
            lines.append(f"    not on {cam['vendor_name']} over USB: {', '.join(cam['unsupported'])}")
        if cam["not_exposed"]:
            lines.append(f"    not exposed by this body: {', '.join(cam['not_exposed'])}")
        for k, step in cam["camera_menu"].items():
            lines.append(f"    camera menu only: {k} ({step})")
        for k, v in cam["prohibit"].items():
            lines.append(f"    {k.replace('_', ' ')} prohibit: {v or 'none'}")
    else:
        if usb.get("error"):
            lines.append(f"  ✗ camera found but not usable: {usb['error']}")
        elif usb["ptp_cameras"]:
            lines.append(f"  ✗ camera detected ({usb['ptp_cameras'][0][0]}) but not usable")
        else:
            lines.append("  ✗ no USB camera")
        lines.append("  Checks:")
        lines += [f"    · {c}" for c in usb.get("checks", [])]
        lines.append("  Camera USB setup:")
        lines += [f"    {v}: {h}" for v, h in usb.get("setup_hints", {}).items()]
    if usb["camera_holders"]:
        lines.append("  Processes that can grab the camera:")
        lines += [f"    {h}" for h in usb["camera_holders"]]
    lines += [f"  {n}" for n in usb.get("fix_notes", [])]
    lines.append(f"  Mounted camera cards: {', '.join(usb['cards']) or 'none'}")

    lines.append("Bluetooth")
    if ble["devices"]:
        for d in ble["devices"]:
            lines.append(f"  {d['address']}  {d['brand']:<10} {d['name'] or ''}" + ("  [default]" if d["default"] else ""))
    else:
        lines.append("  no remembered cameras (camctl ble pair)")
    dm = ble.get("daemon")
    lines.append("  daemon: " + (f"running (pid {dm.get('pid')}) for {dm.get('brand')} {dm.get('address')}, "
                                 f"connected: {_yes(dm.get('connected'))}" if dm else "not running (camctl ble serve)"))
    lines.append(f"  blueutil: {ble['blueutil'] or 'not installed'}")
    if "scan" in ble:
        lines.append(f"  scan: {len(ble['scan'])} camera(s)")
        for f in ble["scan"]:
            pm = " pairing-mode" if f.get("pairing_mode") else ""
            sup = "" if f.get("supported") else " (unsupported)"
            lines.append(f"    {f['address']}  {f['brand']:<10} {f['name']}  rssi={f['rssi']}{pm}{sup}")

    lines.append(f"Drive: {drive['mode'] + ' OK' if drive['ok'] else drive['error']}")
    for w in r["warnings"]:
        lines.append(f"! {w}")
    for p in r["problems"]:
        lines.append(f"✗ {p}")
    lines.append("All good." if r["ok"] else f"{len(r['problems'])} problem(s).")
    return "\n".join(lines)
