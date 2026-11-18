"""CLI doctor / status / apply output and the multi-brand `ble` commands. Fakes only: no USB, Bluetooth or Drive."""

from __future__ import annotations

import json

import pytest

from camctl import ble, cli, doctor

from .conftest import FakeCamera
from .test_review_ui import run, run_json


class OkDrive:
    mode = "gcloud"


class NoDrive:
    def __init__(self):
        raise RuntimeError("no creds")


@pytest.fixture
def env(monkeypatch):
    """Doctor environment: no camera holders, no cards, Drive authorised, no USB camera (tests add one)."""
    import camctl.drive as drive
    import camctl.offload as off
    state = {"cards": []}
    monkeypatch.setattr(doctor, "_holders", lambda: [])
    monkeypatch.setattr(off, "find_cards", lambda: state["cards"])
    monkeypatch.setattr(drive, "Drive", OkDrive)
    return state


def with_camera(monkeypatch, cam=None):
    import camctl.ptp as ptp
    cam = cam or FakeCamera()
    monkeypatch.setattr(ptp, "autodetect", lambda: [("USB PTP Class Camera", "usb:002,001")])
    monkeypatch.setattr(cli, "_camera", lambda args: cam)
    return cam


# -- doctor ---------------------------------------------------------------------------------------------

def test_doctor_without_camera_prints_vendor_setup_and_fails(env, capsys):
    code, out, _ = run(["doctor"], capsys)
    assert code == 1
    for vendor in ("Sony", "Canon", "Nikon", "Fujifilm", "Panasonic", "OM System/Olympus"):
        assert f"{vendor}:" in out
    assert "data-capable" in out and "Image Capture" in out and "PC(Tether)" in out
    assert "no remembered cameras" in out and "daemon: not running" in out and "Drive: gcloud OK" in out
    code, data, _ = run_json(["--json", "doctor"], capsys)
    assert code == 1 and data["ok"] is False and "No USB camera" in data["problems"][0]
    assert data["usb"]["camera"] is None and "Canon" in data["usb"]["setup_hints"]
    assert data["ble"] == {"devices": [], "default": None, "daemon": None,
                           "blueutil": data["ble"]["blueutil"]}


def test_doctor_with_sony_camera_is_ok(env, monkeypatch, capsys):
    cam = with_camera(monkeypatch)
    code, data, _ = run_json(["--json", "doctor"], capsys)
    assert code == 0 and data["ok"] is True and data["problems"] == []
    c = data["usb"]["camera"]
    assert (c["vendor"], c["model"], c["battery"], c["tested_on_hardware"]) == ("sony", "ILCE-7M5", "80%", True)
    assert c["writable_count"] == len(c["writable"]) > 0 and "white_balance" in c["writable"]
    assert {x["key"] for x in c["locked"]} >= {"shutter", "exposure_program"}
    assert "iso_auto" in c["unsupported"] and "log_shooting" in c["camera_menu"]
    assert cam.opened == cam.closed == 1 and cam.writes == []           # read-only, session closed
    code, out, _ = run(["doctor"], capsys)
    assert code == 0 and "✓ Sony ILCE-7M5" in out and "verified on hardware" in out and "All good." in out
    assert "shutter" in out and "mode dial to M" in out


def test_doctor_full_card_is_a_problem_low_battery_a_warning(env, monkeypatch, capsys):
    cam = FakeCamera()
    cam.flat["manufacturer"] = {"label": "", "readonly": True, "type": "text", "value": "Sony Corporation"}
    cam.flat["d249"] = {"label": "", "readonly": True, "type": "text", "value": "0"}
    cam.flat["d24a"] = {"label": "", "readonly": True, "type": "text", "value": "0"}
    cam.flat["batterylevel"]["value"] = "10%"
    with_camera(monkeypatch, cam)
    code, data, _ = run_json(["--json", "doctor"], capsys)
    assert code == 1 and any("full or missing" in p for p in data["problems"])
    assert any("Battery at 10%" in w for w in data["warnings"])


def test_doctor_camera_that_wont_open(env, monkeypatch, capsys):
    import camctl.ptp as ptp
    monkeypatch.setattr(ptp, "autodetect", lambda: [("USB PTP Class Camera", "usb:002,001")])
    monkeypatch.setattr(doctor, "_holders", lambda: ["123 ptpcamerad"])

    def claimed(args):
        raise ptp.CameraError("Could not claim the USB device")
    monkeypatch.setattr(cli, "_camera", claimed)
    code, data, _ = run_json(["--json", "doctor"], capsys)
    assert code == 1 and "could not be opened: Could not claim" in data["problems"][0]
    assert "doctor --fix" in data["problems"][0] and data["usb"]["setup_hints"]


def test_doctor_card_in_mass_storage_is_only_a_warning(env, capsys):
    env["cards"] = ["/Volumes/Untitled"]
    code, data, _ = run_json(["--json", "doctor"], capsys)
    assert code == 0 and data["ok"] and any("Mass Storage" in w or "card is mounted" in w for w in data["warnings"])


def test_doctor_drive_is_a_warning(env, monkeypatch, capsys):
    import camctl.drive as drive
    with_camera(monkeypatch)
    monkeypatch.setattr(drive, "Drive", NoDrive)
    code, data, _ = run_json(["--json", "doctor"], capsys)
    assert code == 0 and data["drive"] == {"ok": False, "error": "no creds"}
    assert any("no creds" in w for w in data["warnings"])


def test_doctor_ble_section_and_scan(env, monkeypatch, capsys):
    with_camera(monkeypatch)
    ble.remember("AA", "ILCE-7M5", "sony")
    monkeypatch.setattr(ble.bond, "find_blueutil", lambda: None)
    seen = {}

    async def scan(timeout, **kw):
        seen["timeout"] = timeout
        return [ble.Found("AA", "ILCE-7M5", -50, True), ble.Found("OM", "OM-1", -70, None, brand="omsystem")]
    monkeypatch.setattr(ble, "scan", scan)
    code, data, _ = run_json(["--json", "doctor", "--ble-scan", "--timeout", "2"], capsys)
    assert code == 0 and seen == {"timeout": 2.0}
    assert data["ble"]["devices"] == [{"address": "AA", "name": "ILCE-7M5", "brand": "sony", "default": True}]
    assert [(f["brand"], f["supported"]) for f in data["ble"]["scan"]] == [("sony", True), ("omsystem", False)]
    assert any("brew install blueutil" in w for w in data["warnings"])  # Sony needs a bond → blueutil matters

    async def off(timeout, **kw):
        raise ble.BluetoothUnavailable("Bluetooth is off; turn it on in Control Centre.")
    monkeypatch.setattr(ble, "scan", off)
    code, data, _ = run_json(["--json", "doctor", "--ble-scan"], capsys)
    assert code == 1 and "Bluetooth scan failed: Bluetooth is off" in data["problems"][0]


def test_doctor_never_scans_bluetooth_unless_asked(env, monkeypatch, capsys):
    async def boom(*a, **k):
        raise AssertionError("scanned without --ble-scan")
    monkeypatch.setattr(ble, "scan", boom)
    code, data, _ = run_json(["--json", "doctor"], capsys)
    assert "scan" not in data["ble"]


# -- status / apply output ------------------------------------------------------------------------------

def test_status_shows_vendor_card_and_unsupported(monkeypatch, capsys):
    cam = with_camera(monkeypatch)
    cam.flat["d249"] = {"label": "", "readonly": True, "type": "text", "value": "20"}
    cam.flat["d24a"] = {"label": "", "readonly": True, "type": "text", "value": "3000"}
    code, data, _ = run_json(["--json", "status"], capsys)
    assert code == 0 and data["vendor"] == "sony" and data["model"] == "ILCE-7M5"
    assert "iso_auto" in data["unsupported"] and "nearly full" in data["warnings"][0]
    assert data["settings"]["exposure_program"]["value"] == "Intelligent Auto"
    code, out, _ = run(["status"], capsys)
    assert out.startswith("Sony ILCE-7M5") and "! Card slot 1 nearly full" in out
    assert "Not on Sony over USB:" in out and "iso_auto" in out


def test_status_without_camera_is_vendor_neutral(capsys):
    code, out, _ = run(["status"], capsys)
    assert code == 1 and "No PTP camera found over USB." in out and "PC Remote" in out and "PC(Tether)" in out
    code, data, _ = run_json(["--json", "status"], capsys)
    assert "Fujifilm" in data["setup_hints"]


def test_set_prints_unsupported_distinctly_and_exits_zero(monkeypatch, capsys):
    with_camera(monkeypatch)
    code, out, _ = run(["set", "iso_auto", "on", "white_balance", "daylight"], capsys)
    assert code == 0
    line = next(ln for ln in out.splitlines() if "iso_auto" in ln)
    assert line.lstrip().startswith("–") and "not available on Sony over USB" in line and "✗" not in out
    code, data, _ = run_json(["--json", "set", "iso_auto", "on"], capsys)
    assert code == 0 and data["unsupported"] == [{"key": "iso_auto", "note": "not available on Sony over USB"}]


# -- ble ------------------------------------------------------------------------------------------------

@pytest.fixture
def executed(monkeypatch):
    calls = []
    state = {"result": {"brand": "sony", "address": "AA", "ok": True}}

    async def execute(cmd, args=None, **kw):
        calls.append({"cmd": cmd, "args": args, **kw})
        kw["on_prompt"]("connecting")
        return {**state["result"], "via": "direct"}
    monkeypatch.setattr(ble, "execute", execute)
    return calls, state


@pytest.mark.parametrize("argv,cmd,args", [
    (["shoot"], "shoot", {"af": True}),
    (["shoot", "--no-af", "--hold", "1.5"], "shoot", {"af": False, "hold": 1.5}),
    (["shoot", "--burst", "3"], "shoot", {"af": True, "burst": 3.0}),
    (["record"], "record", {"action": "toggle"}),
    (["record", "stop"], "record", {"action": "stop"}),
    (["bulb", "30"], "bulb", {"seconds": 30.0}),
    (["bulb", "--seconds", "2"], "bulb", {"seconds": 2.0}),                 # pre-multi-brand spelling
    (["zoom", "tele", "--seconds", "1", "--speed", "40"], "zoom", {"direction": "tele", "seconds": 1.0, "speed": 40}),
    (["focus", "out"], "focus", {"direction": "out", "seconds": 0.5}),
    (["zoom-wide", "--seconds", "2"], "zoom", {"direction": "wide", "seconds": 2.0}),   # old action names
    (["focus-in"], "focus", {"direction": "in", "seconds": 0.5}),
    (["af", "--seconds", "2"], "af_on", {"seconds": 2.0}),
    (["af-on"], "af_on", {"seconds": 0.5}),
    (["autofocus"], "autofocus", {"seconds": 0.5}),
    (["c1"], "c1", {}),
    (["status"], "status", {}),
])
def test_ble_commands_map_to_execute(argv, cmd, args, executed, capsys):
    calls, _ = executed
    code, data, err = run_json(["--json", "ble", *argv], capsys)
    assert code == 0 and calls[0]["cmd"] == cmd and calls[0]["args"] == args
    assert calls[0]["use_daemon"] is True and data["via"] == "direct" and "connecting" in err


def test_ble_address_brand_before_or_after_action(executed, capsys):
    calls, _ = executed
    run(["ble", "--address", "AA", "--brand", "canon", "shoot"], capsys)
    run(["ble", "shoot", "--address", "BB", "--no-daemon", "--timeout", "3"], capsys)
    assert (calls[0]["address"], calls[0]["brand"], calls[0]["timeout"]) == ("AA", "canon", 6.0)
    assert (calls[1]["address"], calls[1]["brand"], calls[1]["use_daemon"], calls[1]["timeout"]) == \
        ("BB", None, False, 3.0)


def test_ble_bulb_needs_seconds(executed, capsys):
    code, _, err = run(["ble", "bulb"], capsys)
    assert code == 2 and "SECONDS is required" in err and executed[0] == []


@pytest.mark.parametrize("released,code,needle", [(True, 0, "shutter released"),
                                                  (False, 1, "did NOT release"),
                                                  (None, 0, "no shutter feedback")])
def test_ble_shoot_exit_code_only_fails_on_definite_no_fire(released, code, needle, executed, capsys):
    _, state = executed
    state["result"] = {"brand": "sony", "address": "AA", "released": released, "focus_locked": False,
                       "hint": "focus did not lock — use MF"}
    rc, out, _ = run(["ble", "shoot"], capsys)
    assert rc == code and needle in out and "hint: focus did not lock" in out


def test_ble_record_unknown_state_prints_hint_and_succeeds(executed, capsys):
    _, state = executed
    state["result"] = {"brand": "canon", "address": "CC", "recording": None, "changed": True,
                       "hint": "Canon does not report recording state; check the camera."}
    code, out, _ = run(["ble", "record", "start"], capsys)
    assert code == 0 and "recording unknown" in out and "does not report" in out


def test_ble_scan_all_brands(monkeypatch, capsys):
    seen = {}

    async def scan(timeout, brands=None, include_unsupported=True):
        seen.update(timeout=timeout, brands=brands, include_unsupported=include_unsupported)
        return [ble.Found("AA", "ILCE-7M5", -40, True), ble.Found("OM", "OM-1", -60, None, brand="omsystem")]
    monkeypatch.setattr(ble, "scan", scan)
    code, data, _ = run_json(["--json", "ble", "scan"], capsys)
    assert code == 0 and seen == {"timeout": 6.0, "brands": None, "include_unsupported": True}
    assert [(d["brand"], d["supported"], d["pairing_mode"]) for d in data] == [("sony", True, True),
                                                                              ("omsystem", False, None)]
    code, out, _ = run(["ble", "scan", "--no-all-brands", "--only", "canon", "--timeout", "2"], capsys)
    assert seen == {"timeout": 2.0, "brands": ["canon"], "include_unsupported": False}
    assert "sony" in out and "pairing-mode" in out and "unsupported" in out


def test_ble_pair_options_and_unverified_exit(monkeypatch, capsys):
    seen = {}
    result = {"brand": "nikon", "address": "NN", "name": "Z 6_2", "bonded": False, "verified": False,
              "extra": {}, "notes": ["dev id saved"]}

    async def pair(address=None, brand=None, **kw):
        seen.update(address=address, brand=brand, **kw)
        return result
    monkeypatch.setattr(ble, "pair", pair)
    code, out, _ = run(["ble", "pair", "--brand", "nikon", "--allow-bt-restart", "--no-verify"], capsys)
    assert seen["brand"] == "nikon" and seen["allow_bt_restart"] is True and seen["verify"] is False
    assert code == 1 and "did NOT survive reconnect" in out and "dev id saved" in out


def test_ble_pair_stale_bond_error_is_one_line_with_the_command(monkeypatch, capsys):
    async def pair(*a, **kw):
        raise ble.BondRestartRequired("Run `blueutil --power 0; sleep 3; blueutil --power 1` then pair again, "
                                      "or pass --allow-bt-restart.")
    monkeypatch.setattr(ble, "pair", pair)
    code, _, err = run(["ble", "pair"], capsys)
    assert code == 1 and err.strip().startswith("error: Run `blueutil --power 0") and "\n" not in err.strip()


def test_ble_devices_default_forget(capsys):
    code, out, _ = run(["ble", "devices"], capsys)
    assert code == 0 and "No remembered cameras" in out
    ble.remember("AA", "ILCE-7M5", "sony")
    ble.remember("CC", "EOS R6", "canon", default=False)
    code, data, _ = run_json(["--json", "ble", "devices"], capsys)
    assert data["default"] == "AA" and [d["address"] for d in data["devices"]] == ["AA", "CC"]
    code, data, _ = run_json(["--json", "ble", "default", "EOS R6"], capsys)
    assert code == 0 and data["default"] == "CC" and ble.get_device().address == "CC"
    code, out, _ = run(["ble", "forget", "CC"], capsys)
    assert code == 0 and "forgot CC" in out and [d.address for d in ble.devices()] == ["AA"]
    code, _, _ = run(["ble", "forget", "CC"], capsys)
    assert code == 1
    code, _, err = run(["ble", "default", "nope"], capsys)
    assert code == 1 and "No remembered camera" in err


def test_ble_unpair_and_serve_forward_options(monkeypatch, capsys):
    seen = {}

    async def unpair(target, **kw):
        seen["unpair"] = (target, kw)
        return {"forgotten": True, "bond": {"cleared": True, "mac": "0c-80"}}

    async def serve(address=None, brand=None, **kw):
        seen["serve"] = (address, brand, kw)
    monkeypatch.setattr(ble, "unpair", unpair)
    monkeypatch.setattr(ble, "serve", serve)
    code, data, _ = run_json(["--json", "ble", "unpair", "ILCE-7M5", "--clear-bond", "--allow-bt-restart"], capsys)
    assert code == 0 and data["bond"]["cleared"] and \
        seen["unpair"] == ("ILCE-7M5", {"clear_bond": True, "allow_bt_restart": True})
    code, out, err = run(["ble", "serve", "--brand", "gopro", "--socket", "/tmp/x.sock"], capsys)
    address, brand, kw = seen["serve"]
    assert code == 0 and (address, brand, kw["socket_path"]) == (None, "gopro", "/tmp/x.sock")
    assert out == "" and "daemon" in err                                      # foreground chatter on stderr


def test_ble_brands(capsys):
    code, data, _ = run_json(["--json", "ble", "brands"], capsys)
    assert code == 0 and data["sony"]["supported"] and "shoot" in data["sony"]["verified"]
    assert data["omsystem"]["supported"] is False
    code, out, _ = run(["ble", "brands"], capsys)
    assert "shoot*" in out and "omsystem" in out and "unsupported" in out
    json.dumps(data)


def test_status_card_lines_hide_stills_mode_movie_zero_and_show_empty_slot2(monkeypatch, capsys):
    cam = with_camera(monkeypatch)
    for k, v in {"d249": "9120", "d24a": "0", "d257": "0", "d258": "0"}.items():
        cam.flat[k] = {"label": "", "readonly": True, "type": "text", "value": v}
    code, out, _ = run(["status"], capsys)
    assert code == 0 and "Card slot 1: 9120 shots left" in out and "min movie" not in out
    assert "Card slot 2: no card, or full" in out
