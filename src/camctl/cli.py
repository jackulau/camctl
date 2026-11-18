"""camctl command line."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from . import advisor, core, presets


def _out(obj: Any, as_json: bool) -> None:
    if as_json or not isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj)
        return
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _camera(args):
    from .ptp import PtpCamera
    return PtpCamera(args.port)


def _pairs(items: list[str]) -> dict[str, str]:
    if len(items) % 2:
        raise SystemExit("error: set: expected KEY VALUE pairs")
    return dict(zip(items[::2], items[1::2]))


def _num(text: str, what: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"calc: {what} must be a number, not {text!r}") from None


def _need(args, attr: str, label: str) -> None:
    if not getattr(args, attr):
        args.parser.error(f"{args.cmd} {args.action}: {label} is required")


def _failed(d: dict, strict: bool) -> bool:
    """strict (explicit `set`): any problem fails. Presets/configs: only hard write errors."""
    return any(strict or s["status"] == "error" for s in d["problems"])


def _print_apply(res: core.ApplyResult | dict, as_json: bool, **extra: Any) -> dict:
    d = res if isinstance(res, dict) else res.as_dict()
    if as_json:
        _out({**extra, **d}, True)
        return d
    for s in d["applied"]:
        print(f"  ✓ {s['key']:<20} {s['current']!s:>18} → {s['target']}" + (f"  ({s['note']})" if s["note"] else ""))
    for k in d["unchanged"]:
        print(f"  = {k}")
    for s in d["problems"]:
        print(f"  ✗ {s['key']:<20} [{s['status']}] {s['note']}")
    for u in d.get("unsupported", []):                 # vendor has no such setting: information, not a failure
        print(f"  – {u['key']:<20} {u['note']}")
    if d["manual_on_camera"]:
        print("\nOn the camera:")
        for m in d["manual_on_camera"]:
            print(f"  □ {m}")
    return d


# -- commands ---------------------------------------------------------------

NO_CAMERA = "No PTP camera found over USB."


def _no_camera_help() -> list[str]:
    from . import vendors
    from .doctor import USB_CHECKS
    return ["Checks:", *[f"  · {c}" for c in USB_CHECKS], "Camera USB setup:",
            *[f"  {v}: {h}" for v, h in vendors.setup_hints().items()]]


def cmd_status(args) -> None:
    from . import vendors
    from .ptp import autodetect
    found = autodetect()
    if not found and not args.port:
        from .offload import find_cards
        cards = find_cards()
        if args.json:
            _out({"detected": [], "error": NO_CAMERA, "setup_hints": vendors.setup_hints(),
                  "cards_in_mass_storage_mode": [str(c) for c in cards]}, True)
        else:
            print(NO_CAMERA)
            print("\n".join(_no_camera_help()))
            if cards:
                print(f"Card(s) mounted in Mass Storage mode: {', '.join(map(str, cards))}")
        sys.exit(1)
    with _camera(args) as cam:
        snap = core.Snapshot(cam)                     # one config walk for both views
        caps = vendors.capabilities(snap)
        settings = core.current_settings(snap, vendor=vendors.PROFILES[caps["vendor"]])
    info = {k: caps[k] for k in ("vendor", "vendor_name", "model", "firmware", "battery", "tested_on_hardware",
                                 "card", "warnings", "unsupported", "locked")}
    if args.json:
        _out({"detected": found, **info, "settings": settings}, True)
        return
    facts = ", ".join(x for x in (f"fw {info['firmware']}" if info["firmware"] else "",
                                  f"battery {info['battery']}" if info["battery"] else "") if x)
    tested = "verified on hardware" if info["tested_on_hardware"] else "untested on hardware"
    print(" ".join(x for x in (info["vendor_name"], info["model"]) if x) + (f"  ({facts})" if facts else "")
          + f"  — {tested}")
    for k, v in settings.items():
        print(f"  {k:<20} {v['value']!s:<24} {'' if v['writable'] else '(locked)'}")
    card = info["card"]
    if card:
        print(_card_line(card["slot"], card))
        if "slot2" in card:
            print(_card_line(2, card["slot2"]))
    for w in info["warnings"]:
        print(f"! {w}")
    if info["unsupported"]:
        print(f"Not on {info['vendor_name']} over USB: {', '.join(info['unsupported'])}")


def _card_line(slot: int, card: dict) -> str:
    shots, movie_s = card.get("remaining_shots"), card.get("remaining_movie_seconds")
    if not shots and not movie_s:
        return f"Card slot {slot}: no card, or full"
    # Sony reports movie time 0 outside Movie mode — only show it when the camera gives a real number.
    return f"Card slot {slot}: {shots} shots" + (f", {movie_s // 60} min movie" if movie_s else "") + " left"


def cmd_settings(args) -> None:
    with _camera(args) as cam:
        cur = core.current_settings(cam)
    if args.json:
        _out(cur, True)
        return
    for k, v in cur.items():
        print(f"  {k:<20} {v['value']!s:<24} {'' if v['writable'] else '(locked)'}")


def cmd_set(args) -> None:
    pairs = _pairs(args.pairs)
    with _camera(args) as cam:
        d = _print_apply(core.apply_settings(cam, pairs, dry_run=args.dry_run), args.json)
    if _failed(d, strict=True):
        sys.exit(1)


def cmd_preset(args) -> None:
    if args.action == "list":
        allp = presets.load_all()
        if args.json:
            _out({name: {"description": p.get("description", ""), "switch": p.get("switch"),
                         "source": p.get("source")} for name, p in allp.items()}, True)
            return
        for name, p in allp.items():
            print(f"  {name:<18} {p.get('description', '')}")
        return
    _need(args, "name", "NAME")
    if args.action == "show":
        _out(presets.get(args.name), True)
        return
    if args.action == "apply":
        p = presets.get(args.name)
        backup = None
        with _camera(args) as cam:
            if not args.dry_run and not args.no_backup:
                backup = str(core.dump(cam, meta={"reason": f"before preset {args.name}"}))
            res = core.apply_settings(cam, p["settings"], dry_run=args.dry_run, manual=p.get("manual"))
        d = res.as_dict()
        if args.json:
            _print_apply({**d, "notes": p.get("notes", [])}, True, preset=args.name,
                         description=p.get("description", ""), dry_run=args.dry_run, backup=backup)
        else:
            if backup:
                print(f"backup: {backup}")
            print(f"{args.name}: {p.get('description', '')}")
            _print_apply(d, False)
            for n in p.get("notes", []):
                print(f"  · {n}")
        if _failed(d, strict=False):
            sys.exit(1)
        return
    if args.action == "save":
        with _camera(args) as cam:
            cur = core.current_settings(cam)
        keys = args.keys.split(",") if args.keys else [k for k, v in cur.items() if v["writable"]]
        settings = {k: cur[k]["value"] for k in keys if k in cur}
        path = presets.save(args.name, settings, args.description or "")
        _out({"path": str(path)} if args.json else str(path), args.json)


def cmd_config(args) -> None:
    if args.action in ("load", "diff"):
        _need(args, "file", "FILE")
    if args.action == "dump":
        with _camera(args) as cam:
            path = core.dump(cam, Path(args.file) if args.file else None)
        _out({"path": str(path)} if args.json else str(path), args.json)
    elif args.action == "load":
        settings, manual = core.read_settings_file(Path(args.file))
        backup = None
        with _camera(args) as cam:
            if not args.dry_run and not args.no_backup:
                backup = str(core.dump(cam, meta={"reason": f"before load {args.file}"}))
                if not args.json:
                    print(f"backup: {backup}")
            res = core.apply_settings(cam, settings, dry_run=args.dry_run, manual=manual)
        d = _print_apply(res, args.json, file=args.file, dry_run=args.dry_run, backup=backup)
        if _failed(d, strict=False):
            sys.exit(1)
    elif args.action == "diff":
        settings, _ = core.read_settings_file(Path(args.file))
        with _camera(args) as cam:
            _out(core.diff(cam, settings), True)
    elif args.action == "raw":
        with _camera(args) as cam:
            flat = cam.config()
        if args.filter:
            flat = {n: i for n, i in flat.items() if args.filter.lower() in (n + i["label"]).lower()}
        if args.json:
            _out(flat, True)
            return
        for name, info in flat.items():
            ro = "ro" if info["readonly"] else "rw"
            choices = f"  {info['choices'][:12]}" if info.get("choices") else ""
            print(f"  {name:<24} {ro} {info['value']!s:<20} {info['label']}{choices}")
    elif args.action == "explain":
        from .sonyptp import SonyPtp, explain
        with SonyPtp() as s:                       # raw Sony SDIO: sees props libgphoto2 hides
            rows = explain(s.props())
        if args.filter:
            f = args.filter.lower()
            rows = [r for r in rows if f in f"{r['code']} {r['name']} {r['key']}".lower()]
        if args.json:
            _out(rows, True)
            return
        for r in rows:
            opts = r.get("choices") or r.get("range") or []
            more = f"  [{', '.join(map(str, opts[:10]))}{', …' if len(opts) > 10 else ''}]" if opts else ""
            print(f"  {r['code']} {'rw' if r['writable'] else 'ro'} {r['name']:<34} {r['value']!s:<22}{more}")
        print(f"\n{len(rows)} properties. Set by canonical key (`camctl settings`) or by code with a raw value, "
              "e.g. `camctl set d23f 10`. E-range (0xE…) codes are read-only here: libgphoto2 cannot write them.")


def cmd_shoot(args) -> None:
    with _camera(args) as cam:
        files = [str(p) for p in cam.capture(Path(args.out))]
    _out({"files": files} if args.json else "\n".join(files), args.json)


def cmd_record(args) -> None:
    start = args.action == "start"
    with _camera(args) as cam:
        cam.movie(start)
    _out({"recording": start} if args.json else f"recording {'started' if start else 'stopped'}", args.json)


def cmd_liveview(args) -> None:
    with _camera(args) as cam:
        Path(args.out).write_bytes(cam.preview_jpeg())
    _out({"path": args.out} if args.json else args.out, args.json)


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# `ble` action → (commands.COMMANDS name, fixed args). Old action names (af, zoom-tele, ...) keep working.
BLE_COMMANDS = {"shoot": ("shoot", {}), "record": ("record", {}), "bulb": ("bulb", {}), "status": ("status", {}),
                "autofocus": ("autofocus", {}), "af-on": ("af_on", {}), "af": ("af_on", {}), "c1": ("c1", {}),
                "zoom": ("zoom", {}), "focus": ("focus", {}),
                "zoom-tele": ("zoom", {"direction": "tele"}), "zoom-wide": ("zoom", {"direction": "wide"}),
                "focus-in": ("focus", {"direction": "in"}), "focus-out": ("focus", {"direction": "out"})}


def _ble_args(args) -> dict[str, Any]:
    a = args.action
    if a == "shoot":
        out = {"af": not args.no_af, "hold": args.hold, "burst": args.burst}
    elif a == "record":
        out = {"action": args.record_action}
    elif a == "bulb":
        seconds = args.bulb_seconds if args.bulb_seconds is not None else args.seconds
        if seconds is None:
            args.parser.error("ble bulb: SECONDS is required")
        out = {"seconds": seconds}
    elif a in ("zoom", "focus"):
        out = {"direction": args.direction, "seconds": args.seconds, "speed": args.speed}
    elif a in ("zoom-tele", "zoom-wide", "focus-in", "focus-out", "autofocus", "af-on", "af"):
        out = {"seconds": args.seconds}
    else:
        out = {}
    return {k: v for k, v in {**out, **BLE_COMMANDS[a][1]}.items() if v is not None}


def _print_ble_result(cmd: str, res: dict[str, Any]) -> None:
    who = f"{res.get('brand', '?')} {res.get('address', '')}".strip()
    if cmd == "shoot" or (cmd == "bulb" and "released" in res):
        state = {True: "shutter released", False: "shutter did NOT release",
                 None: "sent (this camera reports no shutter feedback)"}[res.get("released")]
        print(f"{who}: {state}" + (f", focus locked: {res['focus_locked']}" if res.get("focus_locked") is not None
                                   else ""))
    elif cmd == "record":
        rec = res.get("recording")
        print(f"{who}: recording {'on' if rec else 'off' if rec is False else 'unknown'}"
              + ("" if res.get("changed", True) else " (already)"))
    elif cmd == "status":
        print(f"{who}: connected {res.get('connected')}, capabilities: {', '.join(res.get('capabilities', []))}")
        if res.get("status"):
            print(f"  status: {json.dumps(res['status'], default=str)}")
    else:
        print(f"{who}: {cmd} done")
    if res.get("hint"):
        print(f"  hint: {res['hint']}")
    if res.get("via") == "daemon":
        print("  (via the running `camctl ble serve` daemon)")


def _ble_devices() -> dict[str, Any]:
    from . import ble
    default = ble.get_device()
    return {"default": default.address if default else None,
            "devices": [{"address": d.address, "name": d.name, "brand": d.brand,
                         "default": bool(default and d.address == default.address)} for d in ble.devices()]}


def cmd_ble(args) -> None:
    from . import ble
    a = args.action
    if a == "brands":
        brands = ble.supported_brands()
        if args.json:
            _out(brands, True)
            return
        for b, d in brands.items():
            if not d["supported"]:
                print(f"  {b:<10} unsupported — {d['reason']}")
                continue
            caps = ", ".join(c + ("*" if c in d["verified"] else "?" if c in d["experimental"] else "")
                             for c in d["capabilities"])
            print(f"  {b:<10} {caps}")
        print("\n  * verified on hardware   ? experimental   (others: from published protocol sources)")
        return
    if a == "devices":
        d = _ble_devices()
        if args.json:
            _out(d, True)
            return
        if not d["devices"]:
            print("No remembered cameras — run `camctl ble pair`.")
        for x in d["devices"]:
            print(f"  {x['address']}  {x['brand']:<10} {x['name'] or ''}" + ("  [default]" if x["default"] else ""))
        return
    if a == "forget":
        ok = ble.forget(args.target)
        _out({"forgotten": ok} if args.json else (f"forgot {args.target}" if ok else f"{args.target} not remembered"),
             args.json)
        if not ok:
            sys.exit(1)
        return
    if a == "default":
        dev = ble.set_default(args.target)
        _out({"default": dev.address, "name": dev.name, "brand": dev.brand} if args.json
             else f"default: {dev.address} ({dev.brand} {dev.name or ''})".rstrip(), args.json)
        return
    asyncio.run(_ble_async(args, ble))


async def _ble_async(args, ble) -> None:
    a = args.action
    if a == "scan":
        found = await ble.scan(args.timeout, brands=args.scan_brand or None, include_unsupported=args.all_brands)
        if args.json:
            _out([f.to_dict() for f in found], True)
            return
        if not found:
            print("No cameras advertising. Turn on the camera's Bluetooth remote function (camctl ble brands).")
        for f in found:
            pm = {True: "pairing-mode", False: "", None: ""}[f.pairing_mode]
            sup = "" if f.supported else "unsupported"
            print(f"  {f.address}  {f.brand:<10} {f.name:<16} rssi={f.rssi} {pm} {sup}".rstrip())
        return
    if a == "pair":
        res = await ble.pair(args.address, args.brand, timeout=args.timeout, on_prompt=_stderr,
                             verify=not args.no_verify, allow_bt_restart=args.allow_bt_restart)
        if args.json:
            _out(res, True)
        else:
            verified = {True: "pairing survives reconnect", False: "pairing did NOT survive reconnect",
                        None: "not verified"}[res.get("verified")]
            print(f"paired with {res.get('name') or res['address']} ({res['brand']}, {res['address']}) — {verified}")
            for n in res.get("notes", []):
                print(f"  · {n}")
            if res.get("stale_bond_cleared"):
                print("  · cleared a stale macOS Bluetooth bond first")
        if res.get("verified") is False:
            sys.exit(1)
        return
    if a == "unpair":
        res = await ble.unpair(args.target, clear_bond=args.clear_bond, allow_bt_restart=args.allow_bt_restart)
        if args.json:
            _out(res, True)
        else:
            print(f"forgot {args.target}" if res["forgotten"] else f"{args.target} was not remembered")
            if res.get("bond"):
                print(f"  bond: {json.dumps(res['bond'], default=str)}")
        return
    if a == "serve":
        _stderr("BLE daemon starting — keeps the camera connected; stop with Ctrl-C.")
        await ble.serve(args.address, args.brand, socket_path=args.socket, on_prompt=_stderr, timeout=args.timeout)
        return
    cmd = BLE_COMMANDS[a][0]
    res = await ble.execute(cmd, _ble_args(args), address=args.address, brand=args.brand,
                            use_daemon=not args.no_daemon, on_prompt=_stderr, timeout=args.timeout)
    if args.json:
        _out(res, True)
    else:
        _print_ble_result(cmd, res)
    if res.get("released") is False:          # definitive: the camera reported it did not fire
        sys.exit(1)


def offload_root(scope: str, layout: str) -> str:
    """The organized layout keeps ONE archive (md5 dedup covers the whole root); the old dated layout kept camera
    media in its own folder."""
    return "Card Archive" if layout == "organized" or scope != "camera" else "A7V Offload"


def cmd_offload(args) -> None:
    from .offload import offload
    cards = [Path(args.src)] if args.src else None
    root = args.root or offload_root(args.scope, args.layout)
    log = (lambda m: print(m, file=sys.stderr, flush=True)) if args.json else print
    r = offload(cards, root_name=root, jobs=args.jobs, dry_run=args.dry_run, scope=args.scope, log=log,
                organize=args.layout == "organized", rehash=args.rehash)
    summary = r.summary()
    next_step = ("Every file is verified on Drive. To clear the card, format it IN CAMERA "
                 "(MENU → Shooting → Media → Format).")
    if args.json:
        _out({**summary, **({"next_step": next_step} if r.safe_to_format else {})}, True)
    else:
        _out(summary, True)
        if r.safe_to_format:
            print(f"\n{next_step}")
    if summary.get("failed"):
        sys.exit(1)


def _print_organize(s: dict) -> None:
    print(f"'{s['root']}': {s['files']} files " + ", ".join(f"{k}: {v}" for k, v in s["files_by_area"].items()))
    print("  found in layouts: " + ", ".join(f"{k} {v}" for k, v in sorted(s["layouts"].items())))
    print("  after organizing:")
    for dest, n in s["destinations"].items():
        print(f"    {n:6}  {dest}")
    print("  year from: " + ", ".join(f"{k} {v}" for k, v in sorted(s["year_sources"].items()))
          + f"; unknown year: {s['unknown_year']}")
    print(f"  moves: {s['moves']} (renamed for a name clash: {s['renamed_for_name_clash']}); "
          f"already in place: {s['already_in_place']}")
    print(f"  duplicates to trash: {s['duplicates_to_trash']} ({s['duplicate_bytes'] / 1e9:.2f} GB); "
          f"emptied folders to trash: {s['folders_emptied_to_trash']}; files without md5 left alone: "
          f"{s['not_touched_no_md5']}")
    if s.get("merge_not_found"):
        print(f"  merge folders not found: {', '.join(s['merge_not_found'])}")
    for m in s["sample_moves"][:15]:
        print(f"    {m['from']}  →  {m['to']}")
    for d in s["sample_duplicates"][:5]:
        print(f"    trash {d['trash']}  (keep {d['keep']})")
    if s["dry_run"]:
        print("\nDry run: nothing changed. Apply with: camctl drive organize --apply")
        return
    v = s["verification"]
    print(f"\nmoved {s['moved']}, trashed {s['trashed_duplicates']} duplicates and {s['trashed_empty_folders']} "
          f"empty folders, {s['failed']} failed; verification {'OK' if v.get('ok') else 'FAILED'} "
          f"({v.get('md5s_checked')} md5s; missing {v.get('missing_count')}, still in merge folder "
          f"{v.get('still_in_merge_folder_count')}, duplicated {v.get('duplicated_count')})")
    for f in s["failures"][:10]:
        print(f"    FAILED {f.get('op')} {f.get('from') or f.get('path')}: {f.get('error')}")
    print(f"log: {s['log']}")
    if not s["ok"]:
        print("Re-run `camctl drive organize --apply` to continue; it picks up where this run stopped.")


def cmd_drive(args) -> None:
    from . import drive
    if args.action == "organize":
        from .organize import organize
        merge = [] if args.no_merge else (args.merge or ["A7V Offload"])
        r = organize(root_name=args.root, merge=merge, dry_run=not args.apply, jobs=args.jobs, log=_stderr,
                     allow_running_offload=args.allow_running_offload)
        s = r.summary()
        if args.json:
            _out(s, True)
        else:
            _print_organize(s)
        if not r.ok:
            sys.exit(1)
        return
    if args.action == "login":
        info = sys.stderr if args.json else sys.stdout
        print("Preferred: gcloud auth login --enable-gdrive-access  (no 7-day token expiry)", file=info)
        with contextlib.redirect_stdout(info):
            drive.login()
        print("saved", file=info)
    d = drive.Drive()
    a = d.about()
    user = a["user"]["emailAddress"]
    _out({"mode": d.mode, "user": user} if args.json else f"{d.mode}: {user}", args.json)


def cmd_calc(args) -> None:
    try:
        if args.what == "shutter":
            r = advisor.shutter_for_fps(_num(args.a, "FPS"), _num(args.b or "180", "ANGLE"))
        elif args.what == "slowmo":
            r = advisor.slowmo(_num(args.a, "SRC_FPS"), _num(args.b or "24", "TIMELINE"))
        else:
            r = advisor.nd_for(args.a, args.b or "1/50")
    except ValueError as e:
        msg = str(e)
        raise ValueError(msg if msg.startswith("calc:") else f"calc {args.what}: {msg}") from None
    _out(r, True)


def cmd_doctor(args) -> None:
    """Read-only checks of USB, Bluetooth and Drive. --fix only ever stops ptpcamerad (macOS relaunches it)."""
    from . import doctor
    report = asyncio.run(doctor.run(lambda: _camera(args), port=args.port, fix=args.fix, ble_scan=args.ble_scan,
                                    scan_timeout=args.timeout))
    _out(report if args.json else doctor.render(report), args.json)
    if not report["ok"]:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="camctl", description="Camera control (Sony, Canon, Nikon, Fujifilm, Panasonic, "
                                 "OM System) over USB / Wi-Fi PTP and Bluetooth LE")
    ap.add_argument("--port", help="usb:BUS,DEV or ptpip:IP (default: autodetect USB)")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    ap.add_argument("--debug", action="store_true", help="show full tracebacks")
    # --json also accepted after the subcommand; SUPPRESS keeps it from overwriting the global flag.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def parser(name: str, **kw) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], **kw)
        p.set_defaults(parser=p)
        return p

    parser("status").set_defaults(fn=cmd_status)
    parser("settings", help="current settings (canonical names)").set_defaults(fn=cmd_settings)

    p = parser("set", help="camctl set shutter 1/50 aperture 2.8 movie_frame_rate 24p")
    p.add_argument("pairs", nargs="+")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_set)

    p = parser("preset")
    p.add_argument("action", choices=["list", "show", "apply", "save"])
    p.add_argument("name", nargs="?", help="preset name (required for show/apply/save)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--keys", help="save: comma list of keys (default: all writable)")
    p.add_argument("--description")
    p.set_defaults(fn=cmd_preset)

    p = parser("config", help="dump / load / diff / explain full camera config")
    p.add_argument("action", choices=["dump", "load", "diff", "raw", "explain"])
    p.add_argument("file", nargs="?", help="dump: output path (optional); load/diff: dump or preset file (required)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--filter")
    p.set_defaults(fn=cmd_config)

    p = parser("shoot", help="capture a still and download it")
    p.add_argument("-o", "--out", default="captures")
    p.set_defaults(fn=cmd_shoot)

    p = parser("record")
    p.add_argument("action", choices=["start", "stop"])
    p.set_defaults(fn=cmd_record)

    p = parser("liveview", help="save one live-view JPEG")
    p.add_argument("-o", "--out", default="liveview.jpg")
    p.set_defaults(fn=cmd_liveview)

    p = parser("ble", help="Bluetooth remote: shutter/AF/record/zoom/focus (no settings), any supported brand")
    p.add_argument("--address", help="camera address or remembered name (default: the default camera)")
    p.add_argument("--brand", help="sony | canon | fujifilm | nikon | gopro | panasonic (default: detect)")
    p.add_argument("--timeout", type=float, default=6.0, help="scan timeout in seconds")
    # Same options after the action; SUPPRESS keeps them from overwriting values given before it.
    ble_opts = argparse.ArgumentParser(add_help=False)
    ble_opts.add_argument("--address", default=argparse.SUPPRESS, help="camera address or remembered name")
    ble_opts.add_argument("--brand", default=argparse.SUPPRESS, help="camera brand (default: detect)")
    ble_opts.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="scan timeout in seconds")
    ble_opts.add_argument("--no-daemon", action="store_true", help="connect directly even if `ble serve` runs")
    bsub = p.add_subparsers(dest="action", required=True, metavar="ACTION")

    def bp(name: str, help: str | None = None, **kw) -> argparse.ArgumentParser:
        q = bsub.add_parser(name, parents=[common, ble_opts], **({"help": help} if help else {}), **kw)
        q.set_defaults(parser=q)
        return q

    q = bp("scan", "list advertising cameras of every brand (brand, supported, pairing mode)")
    q.add_argument("--all-brands", action=argparse.BooleanOptionalAction, default=True,
                   help="include brands camctl can't drive (default); --no-all-brands: supported only")
    q.add_argument("--only", dest="scan_brand", action="append", metavar="BRAND", help="only this brand (repeatable)")
    q = bp("pair", "pair with a camera in pairing mode, remember it as default and verify it survives a reconnect")
    q.add_argument("--allow-bt-restart", action="store_true",
                   help="clear a stale macOS bond by power-cycling Bluetooth (drops other BT devices ~10 s)")
    q.add_argument("--no-verify", action="store_true", help="skip the reconnect check")
    q = bp("shoot", "release the shutter (half-press AF first unless --no-af)")
    q.add_argument("--no-af", action="store_true")
    q.add_argument("--hold", type=float, help="hold the release this many seconds")
    q.add_argument("--burst", type=float, help="continuous shooting for this many seconds")
    q = bp("record", "movie record: toggle (default), start or stop")
    q.add_argument("record_action", nargs="?", choices=["toggle", "start", "stop"], default="toggle")
    q = bp("bulb", "bulb exposure of SECONDS (camera in Bulb)")
    q.add_argument("bulb_seconds", nargs="?", type=float, metavar="SECONDS")
    q.add_argument("--seconds", type=float, help=argparse.SUPPRESS)
    for name, dirs in (("zoom", ["tele", "wide"]), ("focus", ["in", "out"])):
        q = bp(name, f"power {name} {'/'.join(dirs)} for --seconds")
        q.add_argument("direction", choices=dirs)
        q.add_argument("--seconds", type=float, default=0.5)
        q.add_argument("--speed", type=int, help="1-127 (default 32)")
    for name, help_ in (("autofocus", "half-press AF for --seconds"), ("af-on", "hold AF-ON for --seconds"),
                        ("af", None), ("zoom-tele", None), ("zoom-wide", None), ("focus-in", None),
                        ("focus-out", None)):
        q = bp(name, help_)
        q.add_argument("--seconds", type=float, default=0.5)
    bp("c1", "press the C1 button")
    bp("status", "connect and report status and capabilities")
    bp("devices", "remembered cameras")
    bp("brands", "supported brands and what each can do")
    for name, help_ in (("forget", "forget a remembered camera"), ("default", "make a remembered camera the default")):
        bp(name, help_).add_argument("target", metavar="ADDRESS_OR_NAME")
    q = bp("unpair", "forget a camera; --clear-bond also removes the Mac's Bluetooth bond")
    q.add_argument("target", metavar="ADDRESS_OR_NAME")
    q.add_argument("--clear-bond", action="store_true")
    q.add_argument("--allow-bt-restart", action="store_true",
                   help="allow power-cycling Bluetooth to clear the bond (drops other BT devices ~10 s)")
    q = bp("serve", "foreground daemon holding the link open; other ble commands use it automatically")
    q.add_argument("--socket", help="unix socket path (default ~/.config/camctl/ble.sock)")
    p.set_defaults(fn=cmd_ble)

    p = parser("offload", help="copy card → Google Drive with md5 verification")
    p.add_argument("--scope", choices=["camera", "other", "all"], default="camera")
    p.add_argument("--src", help="card mount or local copy (default: auto-detect /Volumes)")
    p.add_argument("--root", help="Drive root folder name")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--rehash", action="store_true",
                   help="re-read every file; by default files unchanged since the last offload reuse their md5")
    p.add_argument("--layout", choices=["organized", "dated"], default="organized",
                   help="organized (default): straight into the `drive organize` year/type layout under "
                        "'Card Archive'; dated: old <date>/<card>/… layout (camera root 'A7V Offload')")
    p.set_defaults(fn=cmd_offload)

    p = parser("drive", help="login | whoami | organize (sort the card archive by year, then type)")
    p.add_argument("action", choices=["login", "whoami", "organize"])
    p.add_argument("--root", default="Card Archive", help="organize: archive folder (default 'Card Archive')")
    p.add_argument("--merge", action="append", metavar="FOLDER",
                   help="organize: also move this folder's files in (repeatable; default 'A7V Offload')")
    p.add_argument("--no-merge", action="store_true", help="organize: don't merge any other folder")
    p.add_argument("--apply", action="store_true", help="organize: make the changes (default: dry run)")
    p.add_argument("--jobs", type=int, default=8, help="organize: parallel Drive requests")
    p.add_argument("--allow-running-offload", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_drive)

    p = parser("calc", help="shutter FPS [ANGLE] | slowmo SRC_FPS [TIMELINE] | nd METERED [TARGET]")
    p.add_argument("what", choices=["shutter", "slowmo", "nd"])
    p.add_argument("a")
    p.add_argument("b", nargs="?")
    p.set_defaults(fn=cmd_calc)

    p = parser("doctor", help="check USB camera, Bluetooth and Drive; exit 1 on problems")
    p.add_argument("--fix", action="store_true", help="stop macOS ptpcamerad if it holds the camera")
    p.add_argument("--ble-scan", action="store_true", help="also scan for Bluetooth cameras")
    p.add_argument("--timeout", type=float, default=6.0, help="Bluetooth scan seconds")
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 — one clean line for CLI users
        if args.debug:
            raise
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e      # KeyError str() adds quotes
        sys.exit(f"error: {' '.join(str(msg).split()) or type(e).__name__}")


if __name__ == "__main__":
    main()
