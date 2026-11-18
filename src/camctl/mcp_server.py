"""MCP server (stdio) exposing camctl to an MCP client.

Tools mirror the CLI and return JSON-serialisable dicts. Every camera tool opens a fresh PTP
session and closes it before returning: only one process can hold the camera, so the server
never keeps it between calls. Set CAMCTL_PORT (usb:BUS,DEV or ptpip:IP) to skip USB autodetect.

Nothing here may write to stdout — that is the JSON-RPC channel.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
from pathlib import Path
from typing import Any, Callable, Literal

try:                                    # mcp >= 2: FastMCP was renamed MCPServer
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image

from . import advisor, core, presets, vendors
from .ptp import CameraError

DEFAULT_CAPTURE_DIR = Path.home() / "Pictures" / "camctl"

mcp = FastMCP(
    "camctl",
    instructions=(
        "Control cameras (Sony verified on an A7 V; Canon, Nikon, Fujifilm, Panasonic, OM System via libgphoto2, "
        "untested on hardware) over USB PTP, Bluetooth LE remotes, and offload cards to Google Drive. "
        "Start with camera_doctor or camera_status: they report the vendor, which settings are writable, locked "
        "(with the reason) or unsupported on that brand. Read the camctl://guide resource: some settings are "
        "camera-menu-only and come back as an on-camera checklist, and unsupported keys are information, not "
        "failures. Prefer dry_run=True before applying presets or loading configs."
    ),
)


def _camera():
    """Fresh, unopened PTP session; used as a context manager so it is always closed."""
    from .ptp import PtpCamera
    return PtpCamera(os.environ.get("CAMCTL_PORT") or None)


def _error(e: BaseException) -> dict:
    if isinstance(e, CameraError):
        return {"error": str(e)}
    if isinstance(e, KeyError) and e.args:
        return {"error": str(e.args[0])}
    return {"error": f"{type(e).__name__}: {e}"}


def _safe(fn: Callable) -> Callable:
    """Turn exceptions into {"error": ...} so the model sees a usable message instead of a crash."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — reported to the client
                return _error(e)
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — reported to the client
            return _error(e)
    return wrapper


def _path(p: str) -> Path:
    return Path(p).expanduser()


def _log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# -- status / settings --------------------------------------------------------

@mcp.tool()
@_safe
def camera_status() -> dict:
    """Connected camera: vendor, model, firmware, battery, every canonical setting (value, writable, choices,
    locked_reason), keys unsupported on this vendor, controls, card status and warnings. ok=false when no camera."""
    try:
        with _camera() as cam:
            return {"ok": True, **vendors.capabilities(cam)}
    except CameraError as e:
        out: dict[str, Any] = {"ok": False, **_error(e), "setup_hints": vendors.setup_hints()}
        from .offload import find_cards
        cards = find_cards()
        if cards:
            out["cards_in_mass_storage_mode"] = [str(c) for c in cards]
        return out


@mcp.tool()
@_safe
async def camera_doctor(ble_scan: bool = False, fix: bool = False) -> dict:
    """Health check of everything (same data as `camctl doctor --json`): USB camera + capability summary (writable
    count, locked keys with reasons, unsupported keys, card warnings, prohibit reasons) or per-vendor USB setup
    hints when none is found; remembered Bluetooth cameras, BLE daemon, blueutil; Drive auth. ok=false lists
    `problems`. ble_scan=true also listens for Bluetooth cameras (~6 s). fix=true stops macOS ptpcamerad if it
    holds the camera (macOS relaunches it on demand); it never changes camera settings."""
    from . import doctor
    return await doctor.run(_camera, port=os.environ.get("CAMCTL_PORT") or None, fix=fix, ble_scan=ble_scan)


@mcp.tool()
@_safe
def set_settings(settings: dict[str, str | int | float], dry_run: bool = False) -> dict:
    """Apply settings, e.g. {"shutter": "1/50", "aperture": 2.8, "iso": 800, "movie_frame_rate": "24p"}.

    Keys are canonical names (see camctl://guide) or raw gphoto2 widget names. Values are matched
    against what the camera currently offers; inexact matches are noted per key.
    """
    with _camera() as cam:
        return core.apply_settings(cam, settings, dry_run=dry_run).as_dict()


# -- presets --------------------------------------------------------------------

@mcp.tool()
@_safe
def list_presets() -> dict:
    """Built-in and user presets with descriptions."""
    return {name: {"description": p.get("description", ""), "switch": p.get("switch"), "source": p.get("source")}
            for name, p in presets.load_all().items()}


@mcp.tool()
@_safe
def show_preset(name: str) -> dict:
    """Full preset: settings, on-camera manual checklist and notes."""
    return presets.get(name)


@mcp.tool()
@_safe
def apply_preset(name: str, dry_run: bool = False, backup: bool = True) -> dict:
    """Apply a preset. Unless dry_run, the full camera config is backed up first (backup=True).

    The result lists applied keys, problems and `manual_on_camera` steps the user must do on the body.
    """
    p = presets.get(name)
    backup_path = None
    with _camera() as cam:
        if not dry_run and backup:
            backup_path = str(core.dump(cam, meta={"reason": f"before preset {name}"}))
        res = core.apply_settings(cam, p["settings"], dry_run=dry_run, manual=p.get("manual"))
    return {"preset": name, "description": p.get("description", ""), "dry_run": dry_run, "backup": backup_path,
            **res.as_dict(), "notes": p.get("notes", [])}


# -- config dump / load / diff --------------------------------------------------

@mcp.tool()
@_safe
def dump_config(path: str | None = None) -> dict:
    """Save the full camera property map (YAML, or JSON if path ends in .json). Default: ~/.config/camctl/backups/."""
    with _camera() as cam:
        return {"path": str(core.dump(cam, _path(path) if path else None))}


@mcp.tool()
@_safe
def load_config(path: str, dry_run: bool = True, backup: bool = True) -> dict:
    """Restore a dump or preset file onto the camera. dry_run defaults to True for safety."""
    settings, manual = core.read_settings_file(_path(path))
    backup_path = None
    with _camera() as cam:
        if not dry_run and backup:
            backup_path = str(core.dump(cam, meta={"reason": f"before load {path}"}))
        res = core.apply_settings(cam, settings, dry_run=dry_run, manual=manual)
    return {"file": path, "dry_run": dry_run, "backup": backup_path, **res.as_dict()}


@mcp.tool()
@_safe
def diff_config(path: str) -> dict:
    """Keys whose camera value differs from a dump or preset file."""
    settings, _ = core.read_settings_file(_path(path))
    with _camera() as cam:
        return {"file": path, "differences": core.diff(cam, settings)}


# -- actions --------------------------------------------------------------------

@mcp.tool()
@_safe
def capture(dest_dir: str | None = None) -> dict:
    """Take a still and download every file it produces (RAW+JPEG → 2). Default dir ~/Pictures/camctl.

    In AF-S/AF-C the shutter only releases once focus locks — use MF/DMF if capture keeps failing.
    """
    dest = _path(dest_dir) if dest_dir else DEFAULT_CAPTURE_DIR
    with _camera() as cam:
        files = cam.capture(dest)
    return {"files": [str(f) for f in files]}


@mcp.tool()
@_safe
def record(start: bool) -> dict:
    """Start (true) or stop (false) movie recording. The Photo/Movie switch must be on Movie."""
    with _camera() as cam:
        cam.movie(start)
    return {"recording": start}


@mcp.tool()
@_safe
def liveview_jpeg() -> Image:
    """One live-view frame as a JPEG image."""
    with _camera() as cam:
        data = cam.preview_jpeg()
    return Image(data=data, format="jpeg")


# -- Bluetooth remote ------------------------------------------------------------

def _prompts() -> tuple[list[str], Callable[[str], None]]:
    """Collect camera prompts ("press OK on the camera") into the result; stderr too — never stdout."""
    msgs: list[str] = []

    def on_prompt(msg: str) -> None:
        msgs.append(msg)
        _log_stderr(msg)
    return msgs, on_prompt


async def _ble(call: Callable[[Callable[[str], None]], Any]) -> dict:
    """Run a BLE API call; the result (or error) carries the prompts shown during it."""
    from .ble import BleError
    msgs, on_prompt = _prompts()
    try:
        res = await call(on_prompt)
    except (BleError, ValueError) as e:
        return {"error": str(e), "type": type(e).__name__, **({"prompts": msgs} if msgs else {})}
    return {**res, **({"prompts": msgs} if msgs else {})}


@mcp.tool()
@_safe
async def ble_scan(timeout: float = 6.0, brands: list[str] | None = None, supported_only: bool = False) -> dict:
    """Listen for Bluetooth cameras of every brand (Sony, Canon, Fujifilm, Nikon, GoPro, Panasonic; OM System is
    listed as unsupported). Each result: address, name, brand, rssi, pairing_mode, supported."""
    from . import ble
    found = await ble.scan(timeout, brands=brands or None, include_unsupported=not supported_only)
    return {"cameras": [f.to_dict() for f in found]}


@mcp.tool()
@_safe
async def ble_pair(address: str | None = None, brand: str | None = None, allow_bt_restart: bool = False,
                   verify: bool = True, timeout: float = 6.0) -> dict:
    """Pair with a camera in Bluetooth pairing mode, remember it as the default and verify the pairing survives a
    reconnect. Tell the user to start pairing on the camera first and to press OK there when asked; the
    camera prompts are returned in `prompts`.

    allow_bt_restart: if macOS holds a stale bond the camera discarded (CBError 14), camctl can clear it by
    power-cycling the Mac's Bluetooth with blueutil — that briefly disconnects EVERY Bluetooth device (keyboard,
    mouse, headphones) for ~10 s, so only pass true after the user agrees. With false you get the exact command.
    """
    from . import ble
    return await _ble(lambda on_prompt: ble.pair(address, brand, timeout=timeout, on_prompt=on_prompt,
                                                 verify=verify, allow_bt_restart=allow_bt_restart))


@mcp.tool()
@_safe
async def ble_shoot(address: str | None = None, af: bool = True, timeout: float = 6.0, brand: str | None = None,
                    hold: float | None = None, burst: float | None = None) -> dict:
    """Fire the shutter over Bluetooth (half-press AF first unless af=False; hold/burst in seconds). Works
    without USB. released: true = camera confirmed, false = it reported NOT firing (read `hint`), null = this
    camera gives no shutter feedback. Uses the `camctl ble serve` daemon when running."""
    from . import ble
    args = {k: v for k, v in {"af": af, "hold": hold, "burst": burst}.items() if v is not None}
    return await _ble(lambda on_prompt: ble.execute("shoot", args, address=address, brand=brand,
                                                    on_prompt=on_prompt, timeout=timeout))


@mcp.tool()
@_safe
async def ble_record(address: str | None = None, timeout: float = 6.0, brand: str | None = None,
                     action: Literal["toggle", "start", "stop"] = "toggle") -> dict:
    """Movie recording over Bluetooth: toggle (default), start or stop. recording is null (with a hint) when
    the camera doesn't report its state."""
    from . import ble
    return await _ble(lambda on_prompt: ble.execute("record", {"action": action}, address=address, brand=brand,
                                                    on_prompt=on_prompt, timeout=timeout))


@mcp.tool()
@_safe
async def ble_command(command: Literal["status", "bulb", "autofocus", "zoom", "focus", "af_on", "c1"],
                      seconds: float | None = None, direction: str | None = None, speed: int | None = None,
                      address: str | None = None, brand: str | None = None, timeout: float = 6.0) -> dict:
    """Other Bluetooth remote commands: status; bulb (seconds, required); autofocus / af_on (seconds);
    zoom (direction tele|wide) / focus (direction in|out) with seconds and speed 1-127; c1. What each brand
    supports: ble_brands."""
    from . import ble
    allowed = ble.COMMANDS[command]
    args = {k: v for k, v in {"seconds": seconds, "direction": direction, "speed": speed}.items()
            if v is not None and k in allowed}
    return await _ble(lambda on_prompt: ble.execute(command, args, address=address, brand=brand,
                                                    on_prompt=on_prompt, timeout=timeout))


@mcp.tool()
@_safe
async def ble_devices() -> dict:
    """Remembered Bluetooth cameras (address, name, brand, default) and the persistent-link daemon's status."""
    from . import ble
    default = ble.get_device()
    return {"default": default.address if default else None,
            "devices": [{"address": d.address, "name": d.name, "brand": d.brand,
                         "default": bool(default and d.address == default.address)} for d in ble.devices()],
            "daemon": await ble.daemon_status()}


@mcp.tool()
@_safe
def ble_brands() -> dict:
    """Per brand: Bluetooth capabilities, which are verified on hardware / experimental, feedback, camera menus."""
    from . import ble
    return ble.supported_brands()


# -- offload -----------------------------------------------------------------------

@mcp.tool()
@_safe
def offload(scope: Literal["camera", "other", "all"] = "camera", dry_run: bool = True, root: str | None = None,
            src: str | None = None, jobs: int = 8, layout: Literal["organized", "dated"] = "organized",
            rehash: bool = False) -> dict:
    """Copy a mounted card to Google Drive with md5 verification (never deletes from the card).

    scope: camera (Sony media) | other (non-camera files) | all. dry_run defaults to True: it reports
    what would upload; pass dry_run=false explicitly to upload. layout: organized (default) files each upload
    straight into the drive_organize year/type layout under "Card Archive"; dated = old <date>/<card>/… layout
    (root "A7V Offload" for camera). root overrides the folder. src: card mount point (default: auto-detect).
    Files unchanged since a previous offload (same size + modification time on the same card) reuse their cached
    md5 instead of being re-read; rehash=true reads every file again.
    """
    from .cli import offload_root
    from .offload import offload as run_offload
    if scope not in ("camera", "other", "all"):
        return {"error": f"scope must be camera | other | all, not {scope!r}"}
    if layout not in ("organized", "dated"):
        return {"error": f"layout must be organized | dated, not {layout!r}"}
    root_name = root or offload_root(scope, layout)
    cards = [_path(src)] if src else None
    r = run_offload(cards, root_name=root_name, jobs=jobs, dry_run=dry_run, scope=scope, log=_log_stderr,
                    organize=layout == "organized", rehash=rehash)
    out = {"dry_run": dry_run, "root": root_name, "layout": layout, **r.summary()}
    if r.safe_to_format:
        out["next_step"] = ("Every file is verified on Drive. To clear the card, format it IN CAMERA "
                            "(MENU → Shooting → Media → Format).")
    return out


@mcp.tool()
@_safe
def drive_organize(apply: bool = False, root: str = "Card Archive", merge: list[str] = ["A7V Offload"],
                   jobs: int = 8) -> dict:
    """Sort the Drive card archive by year, then type: <YYYY>/Photos, <YYYY>/Videos, <YYYY>/Camera/RAW|JPEG|Videos,
    plus the user's kept card folders (e.g. Documents/), a music folder (subpaths kept) and Other/<card path>;
    folder names come from ~/.config/camctl/organize.yaml.

    apply defaults to False: a dry run returning counts per destination, duplicates and sample moves. Show the
    user that plan before passing apply=true. Applying only moves files (metadata; no re-upload), puts duplicate
    copies (same md5) and emptied folders in Drive trash (recoverable 30 days), then re-walks the archive and checks
    every file is present exactly once (ok=false otherwise; re-running continues). Refuses while an offload runs.
    merge: other folders whose files move into root (the merge folder is trashed only once empty).
    """
    from .organize import organize
    r = organize(root_name=root, merge=list(merge), dry_run=not apply, jobs=jobs, log=_log_stderr)
    return r.summary()


# -- calculators ---------------------------------------------------------------------

@mcp.tool()
@_safe
def calc_shutter(fps: float, angle: float = 180.0) -> dict:
    """Shutter-angle rule → nearest real shutter speed (24p → 1/50, 60p → 1/125)."""
    return advisor.shutter_for_fps(fps, angle)


@mcp.tool()
@_safe
def calc_slowmo(capture_fps: float, timeline_fps: float = 24) -> dict:
    """Slow-motion factor when conforming capture_fps footage to a timeline_fps timeline."""
    return advisor.slowmo(capture_fps, timeline_fps)


@mcp.tool()
@_safe
def calc_nd(metered_shutter: str, target_shutter: str = "1/50") -> dict:
    """ND strength to go from the shutter the meter wants (e.g. 1/1000) to the one you keep (1/50)."""
    return advisor.nd_for(metered_shutter, target_shutter)


# -- guide resource --------------------------------------------------------------------

FACTS = """\
## Brands and transports

- **USB / Wi-Fi PTP (settings, capture, record, live view):** Sony is verified on an A7 V (ILCE-7M5, fw 1.01).
  Canon, Nikon, Fujifilm, Panasonic and OM System/Olympus go through libgphoto2 and are **untested on hardware**.
  The vendor is detected automatically; presets use canonical keys that each vendor profile translates.
- **Result semantics:** `unsupported` = that brand has no such setting over USB (information, not a failure);
  `camera-menu` steps land in `manual_on_camera`; `problems` are real failures. `camera_status` gives every
  key's `writable` flag and a `locked_reason` (mode dial, white balance not Color Temp, movie-only, ...).
- **Camera-side USB setup:** Sony PC Remote: On (USB); Canon Wi-Fi/Bluetooth off; Nikon USB MTP/PTP (Z: airplane
  mode); Fujifilm USB Tether Shooting Auto; Panasonic USB Mode PC(Tether); OM System RAW/Control.
  `camera_doctor` returns these when no camera is found.
- **Bluetooth LE remote (no settings):** Sony verified; Canon, Fujifilm, Nikon from Furble-confirmed protocols;
  GoPro from the Open GoPro spec; Panasonic experimental; OM System unsupported (no public protocol). Pair once
  with `ble_pair` (camera in pairing mode, press OK on the camera). `ble_shoot` → `released` true/false/null
  (null = brand gives no feedback; false = it reported not firing, read `hint`). A stale macOS bond (CBError 14)
  needs `allow_bt_restart=true`, which briefly drops every Bluetooth device — ask the user first.
  `camctl ble serve` keeps the link open; BLE tools use it automatically when it runs.

## Verified A7 V (ILCE-7M5, fw 1.01) facts

- **Remote exposure mode** needs priority key `d25a=1` (PC Remote) *before* writing mode `500e`
  (camctl does this automatically). It is **session-scoped**: once USB disconnects, the physical
  mode dial rules again.
- **Log Shooting / S-Gamut3.Cine** and **Diff. Set for Still/Mv** are **camera-menu-only** — not exposed
  over PTP. The Picture Profile list has no S-Log PPs (7-9). camctl reports these as on-camera checklist items.
- **Photo and movie share exposure and white balance** unless *Diff. Set for Still/Mv* is enabled on the camera.
- **color_temp** is read-only until white_balance is Color Temp; **steadyshot_movie** only in Movie mode.
- **AF-S / AF-C capture fails if focus can't lock** (lens cap, too dark, nothing in the AF area) — use MF/DMF.
- Canonical setting keys: {keys}.
"""


def guide_markdown() -> str:
    from .settings import CANONICAL
    lines = ["# camctl guide", "", "## Presets", ""]
    for name, p in presets.load_all().items():
        lines.append(f"### {name}" + (f" ({p['switch']})" if p.get("switch") else ""))
        if p.get("description"):
            lines.append(p["description"])
        if p.get("manual"):
            lines.append("")
            lines.append("On the camera:")
            lines += [f"- [ ] {m}" for m in p["manual"]]
        lines.append("")
    lines.append(FACTS.format(keys=", ".join(f"`{c.key}`" for c in CANONICAL)))
    return "\n".join(lines)


@mcp.resource("camctl://guide", name="guide",
              description="Presets, on-camera checklists, brand support and verified A7 V facts",
              mime_type="text/markdown")
def guide() -> str:
    return guide_markdown()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
