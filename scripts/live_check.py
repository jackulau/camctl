"""Hardware smoke test: run with the camera on USB (PC Remote: On, USB).

    uv run python scripts/live_check.py [--record] [--capture]

Read-mostly: every preset is dry-run only; the photo preset is applied for real at the end so the camera is
left ready for stills. --record writes a ~2 s clip to the card, --capture writes one still (MF, no AF wait).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from camctl import core, presets
from camctl.ptp import PtpCamera
from camctl.sonyptp import SonyPtp, explain

results: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        try:
            detail = fn() or ""
            results.append((name, True, str(detail)))
        except Exception as e:  # noqa: BLE001 — report and continue
            results.append((name, False, f"{type(e).__name__}: {e}"))
        return fn
    return deco


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--capture", action="store_true")
    args = ap.parse_args()

    @check("sdio explain")
    def _():
        with SonyPtp() as s:
            rows = explain(s.props())
        mode = next(r["value"] for r in rows if r["code"] == "500e")
        return f"{len(rows)} props, dial/mode={mode}"

    with PtpCamera() as cam:
        @check("status")
        def _():
            return cam.summary().splitlines()[1]

        for name in presets.load_all():
            @check(f"preset dry-run {name}")
            def _(name=name):
                p = presets.get(name)
                d = core.apply_settings(cam, p["settings"], dry_run=True, manual=p.get("manual")).as_dict()
                bad = [f"{s['key']}:{s['status']}" for s in d["problems"] if s["status"] != "readonly"]
                if bad:
                    raise AssertionError(", ".join(bad))
                return f"{len(d['applied'])} would change"

        @check("liveview")
        def _():
            jpg = cam.preview_jpeg()
            assert jpg[:2] == b"\xff\xd8", "not a JPEG"
            return f"{len(jpg)} bytes"

        if args.capture:
            @check("capture (MF)")
            def _():
                fm = cam.widget("focusmode")["value"]
                for w, v in (("d25a", "1"), ("500e", "1"), ("focusmode", "Manual")):
                    cam.set(w, v)
                try:
                    return [p.name for p in cam.capture(Path(tempfile.mkdtemp()))]
                finally:
                    cam.set("focusmode", fm)

        if args.record:
            @check("record 2 s")
            def _():
                cam.set("d25a", "1")
                cam.set("500e", "491603")
                cam.drain_events(1.0)
                cam.movie(True)
                time.sleep(2)
                cam.movie(False)
                return "started+stopped"

        @check("apply photo preset")
        def _():
            p = presets.get("photo")
            d = core.apply_settings(cam, p["settings"]).as_dict()
            if d["problems"]:
                raise AssertionError(d["problems"])
            return f"{len(d['applied'])} changed, {len(d['unchanged'])} already set"

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
