"""CLI / MCP / BLE / preset regressions found in review. No camera, BLE or Drive: fakes only."""

import asyncio
import json
import re
import subprocess

import pytest

from camctl import advisor, ble, cli, core, doctor, presets
from camctl import mcp_server as srv
from camctl.ptp import CameraError

from .conftest import FakeCamera


# -- CLI helpers -----------------------------------------------------------------

@pytest.fixture
def fake(monkeypatch):
    cam = FakeCamera()
    cam.capture = lambda dest: [dest / "DSC0001.ARW", dest / "DSC0001.JPG"]
    monkeypatch.setattr(cli, "_camera", lambda args: cam)
    return cam


@pytest.fixture
def no_camera(monkeypatch):
    def boom(args):
        raise AssertionError("camera must not be opened")
    monkeypatch.setattr(cli, "_camera", boom)


def run(argv, capsys):
    """→ (exit code, stdout, stderr)"""
    code = 0
    try:
        cli.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if isinstance(e.code, str):
            print(e.code, file=__import__("sys").stderr)
    out = capsys.readouterr()
    return code, out.out, out.err


def run_json(argv, capsys):
    code, out, err = run(argv, capsys)
    return code, json.loads(out), err


# -- CLI argument validation -----------------------------------------------------

@pytest.mark.parametrize("argv", [["preset", "apply"], ["preset", "show"], ["preset", "save"],
                                  ["config", "load"], ["config", "diff"]])
def test_missing_name_or_file_is_a_usage_error(argv, capsys, no_camera):
    code, out, err = run(argv, capsys)
    assert code == 2
    assert "required" in err and "NoneType" not in err and "None" not in err.replace("NoneType", "")


def test_set_odd_args_rejected_before_opening_camera(capsys, no_camera):
    code, _, err = run(["set", "shutter"], capsys)
    assert code != 0 and "KEY VALUE" in err


@pytest.mark.parametrize("argv", [["calc", "shutter", "abc"], ["calc", "shutter", "0"], ["calc", "shutter", "-24"],
                                  ["calc", "shutter", "24", "400"], ["calc", "slowmo", "60", "0"],
                                  ["calc", "nd", "fast"], ["calc", "nd", "1/0"], ["calc", "nd", "1/1000", "-1"]])
def test_calc_bad_input_one_clean_line(argv, capsys):
    code, out, err = run(argv, capsys)
    assert code != 0 and out == ""
    line = err.strip()
    assert "\n" not in line and line.startswith("error: calc")
    for junk in ("division by zero", "math domain", "Fraction(", "could not convert"):
        assert junk not in err


def test_unknown_preset_message_is_not_repr_quoted(capsys):
    code, _, err = run(["preset", "show", "nope"], capsys)
    assert code == 1 and err.startswith("error: unknown preset 'nope'")


def test_multiline_errors_become_one_line(monkeypatch, capsys):
    def boom(args):
        raise CameraError("[-105] Unknown model\n  check the cable")
    monkeypatch.setattr(cli, "_camera", boom)
    code, _, err = run(["settings"], capsys)
    assert code == 1 and err.strip() == "error: [-105] Unknown model check the cable"


def test_empty_exception_message_names_the_type(monkeypatch, capsys):
    def boom(args):
        raise TimeoutError()
    monkeypatch.setattr(cli, "_camera", boom)
    code, _, err = run(["settings"], capsys)
    assert code == 1 and err.strip() == "error: TimeoutError"


# -- CLI --json is valid JSON everywhere --------------------------------------------

def test_preset_list_json(capsys):
    code, data, _ = run_json(["--json", "preset", "list"], capsys)
    assert code == 0 and "cinematic-24" in data and data["photo"]["description"]


def test_json_flag_accepted_after_subcommand(capsys):
    code, data, _ = run_json(["calc", "nd", "1/1000", "--json"], capsys)
    assert code == 0 and data["nd_factor"] == "ND20"


def test_preset_apply_json_is_one_document(fake, capsys):
    code, data, _ = run_json(["--json", "preset", "apply", "cinematic-24"], capsys)
    assert code == 0
    assert data["preset"] == "cinematic-24" and data["backup"] and data["notes"]
    assert any("Log Shooting: On" in m for m in data["manual_on_camera"])


def test_preset_apply_dry_run_json(fake, capsys):
    code, data, _ = run_json(["preset", "apply", "easy-video", "--dry-run", "--json"], capsys)
    assert code == 0 and data["backup"] is None and data["dry_run"] is True and fake.writes == []


def test_config_dump_load_json(fake, capsys, tmp_path):
    path = tmp_path / "d.yaml"
    code, data, _ = run_json(["--json", "config", "dump", str(path)], capsys)
    assert code == 0 and data == {"path": str(path)}
    code, data, _ = run_json(["--json", "config", "load", str(path)], capsys)
    assert code == 0 and data["backup"] and data["file"] == str(path)
    code, data, _ = run_json(["--json", "config", "raw", "--filter", "white"], capsys)
    assert code == 0 and "whitebalance" in data


def test_actions_json(fake, capsys, tmp_path):
    code, data, _ = run_json(["--json", "shoot", "-o", str(tmp_path)], capsys)
    assert data == {"files": [str(tmp_path / "DSC0001.ARW"), str(tmp_path / "DSC0001.JPG")]}
    code, data, _ = run_json(["--json", "record", "start"], capsys)
    assert data == {"recording": True}
    code, data, _ = run_json(["--json", "liveview", "-o", str(tmp_path / "lv.jpg")], capsys)
    assert data == {"path": str(tmp_path / "lv.jpg")}
    code, data, _ = run_json(["--json", "preset", "save", "mine", "--keys", "whitebalance"], capsys)
    assert data["path"].endswith("mine.yaml")


def test_status_without_camera(monkeypatch, capsys, no_camera, tmp_path):
    import camctl.offload as off
    import camctl.ptp as ptp
    monkeypatch.setattr(ptp, "autodetect", lambda: [])
    monkeypatch.setattr(off, "find_cards", lambda: [tmp_path / "Untitled"])
    code, data, _ = run_json(["--json", "status"], capsys)
    assert code == 1 and data["detected"] == [] and data["cards_in_mass_storage_mode"] == [str(tmp_path / "Untitled")]
    code, out, _ = run(["status"], capsys)
    assert code == 1 and "No PTP camera" in out


def _fake_offload(monkeypatch, failed: int, calls: dict):
    import camctl.offload as off

    class R:
        safe_to_format = not failed

        def summary(self):
            return {"files": 2, "failed": failed}

    def fake(cards, root_name, jobs, dry_run, scope, log, organize, rehash):
        calls.update(dry_run=dry_run, root=root_name, organize=organize, rehash=rehash)
        log("progress line")
        return R()
    monkeypatch.setattr(off, "offload", fake)


def test_offload_json_keeps_stdout_clean(monkeypatch, capsys):
    calls = {}
    _fake_offload(monkeypatch, 0, calls)
    code, data, err = run_json(["--json", "offload", "--dry-run"], capsys)
    assert code == 0 and data["files"] == 2 and "next_step" in data and calls["dry_run"] is True
    assert "progress line" in err
    assert calls["organize"] is True and calls["root"] == "Card Archive"     # one archive: dedup spans all of it
    assert calls["rehash"] is False                                          # cached md5s reused by default
    run_json(["--json", "offload", "--dry-run", "--rehash"], capsys)
    assert calls["rehash"] is True
    run_json(["--json", "offload", "--dry-run", "--layout", "dated"], capsys)
    assert calls["organize"] is False and calls["root"] == "A7V Offload"


def test_offload_failures_exit_nonzero(monkeypatch, capsys):
    _fake_offload(monkeypatch, 1, {})
    code, data, _ = run_json(["--json", "offload"], capsys)
    assert code == 1 and data["failed"] == 1


def test_set_problem_exits_nonzero(fake, capsys):
    code, data, _ = run_json(["--json", "set", "no_such_widget", "1"], capsys)
    assert code == 1 and data["problems"][0]["status"] == "missing"
    code, data, _ = run_json(["--json", "set", "white_balance", "daylight"], capsys)
    assert code == 0 and data["applied"][0]["status"] == "ok"


# -- doctor --fix ------------------------------------------------------------------

def _doctor(monkeypatch, capsys, holders: str, killall_rc: int = 0):
    import camctl.offload as off
    import camctl.ptp as ptp
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        out = holders if cmd[0] == "pgrep" else ""
        return subprocess.CompletedProcess(cmd, killall_rc if cmd[0] == "killall" else 0, out, "")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    monkeypatch.setattr(ptp, "release_macos_holder",
                        lambda: ran.append(["pkill", "-KILL", "-x", "ptpcamerad"]) or killall_rc == 0)
    monkeypatch.setattr(ptp, "autodetect", lambda: [])
    monkeypatch.setattr(off, "find_cards", lambda: [])
    import camctl.drive as drive

    class NoDrive:
        def __init__(self):
            raise RuntimeError("no creds")
    monkeypatch.setattr(drive, "Drive", NoDrive)
    code, out, _ = run(["doctor", "--fix"], capsys)
    return code, out, ran


def test_doctor_fix_only_stops_ptpcamerad(monkeypatch, capsys):
    code, out, ran = _doctor(monkeypatch, capsys, "123 ptpcamerad\n456 Image Capture\n")
    assert [c[0] for c in ran] == ["pgrep", "pkill"] and ran[1] == ["pkill", "-KILL", "-x", "ptpcamerad"]
    assert "stopped ptpcamerad" in out and "Quit Image Capture" in out


def test_doctor_fix_does_not_claim_success_when_nothing_to_stop(monkeypatch, capsys):
    code, out, ran = _doctor(monkeypatch, capsys, "456 Image Capture\n")
    assert all(c[0] != "killall" for c in ran) and "stopped ptpcamerad" not in out


def test_doctor_fix_reports_killall_failure(monkeypatch, capsys):
    code, out, ran = _doctor(monkeypatch, capsys, "123 ptpcamerad\n", killall_rc=1)
    assert "stopped ptpcamerad" not in out and "could not stop ptpcamerad" in out


# -- ble CLI ------------------------------------------------------------------------

def test_ble_pair_passes_the_selected_camera_to_pair(monkeypatch, capsys):
    # pair() owns scanning/choosing/remembering now (multi-brand); the CLI must forward its options untouched
    seen = {}

    async def fake_pair(address=None, brand=None, **kw):
        seen.update(address=address, brand=brand, **kw)
        kw["on_prompt"]("press OK on the camera")
        return {"brand": "sony", "address": "BB", "name": "ILCE-7M5", "bonded": True, "verified": True,
                "extra": {}, "notes": []}
    monkeypatch.setattr(ble, "pair", fake_pair)
    code, data, err = run_json(["--json", "ble", "pair", "--address", "BB"], capsys)
    assert code == 0 and data["address"] == "BB" and data["name"] == "ILCE-7M5"
    assert seen["address"] == "BB" and seen["verify"] is True and seen["allow_bt_restart"] is False
    assert "press OK on the camera" in err                          # prompts never pollute --json stdout


# -- advisor validation ------------------------------------------------------------

@pytest.mark.parametrize("call", [lambda: advisor.shutter_for_fps(0), lambda: advisor.shutter_for_fps(-24),
                                  lambda: advisor.shutter_for_fps(24, 0), lambda: advisor.shutter_for_fps(24, 400),
                                  lambda: advisor.slowmo(60, 0), lambda: advisor.slowmo(0, 24),
                                  lambda: advisor.nd_for("fast"), lambda: advisor.nd_for("1/0"),
                                  lambda: advisor.nd_for("1/1000", "-1")])
def test_advisor_rejects_bad_input_with_valueerror(call):
    with pytest.raises(ValueError) as ei:
        call()
    assert "division" not in str(ei.value) and "domain" not in str(ei.value)


def test_pal_rates_respect_shutter_angle():
    # the old PAL special case ignored the angle and produced non-existent speeds like 1/52
    r = advisor.shutter_for_fps(25, 172.8)
    assert int(r["shutter"].split("/")[1]) in advisor.STANDARD_SHUTTERS


# -- MCP ------------------------------------------------------------------------------

def _content(result):
    blocks = getattr(result, "content", result)
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return blocks


def test_liveview_through_server(monkeypatch):
    cam = FakeCamera()
    monkeypatch.setattr(srv, "_camera", lambda: cam)
    blocks = _content(asyncio.run(srv.mcp.call_tool("liveview_jpeg", {})))
    assert blocks[0].type == "image"
    assert (getattr(blocks[0], "mime_type", None) or blocks[0].mimeType) == "image/jpeg"

    def bad():
        raise CameraError("no camera")
    monkeypatch.setattr(srv, "_camera", bad)
    blocks = _content(asyncio.run(srv.mcp.call_tool("liveview_jpeg", {})))
    assert json.loads(blocks[0].text) == {"error": "no camera"}


def test_set_settings_accepts_numbers_from_llm(monkeypatch):
    cam = FakeCamera()
    monkeypatch.setattr(srv, "_camera", lambda: cam)
    blocks = _content(asyncio.run(srv.mcp.call_tool(
        "set_settings", {"settings": {"color_temp": 5600, "white_balance": "color_temp"}, "dry_run": True})))
    data = json.loads(blocks[0].text)
    assert "error" not in data and {s["key"] for s in data["applied"]} >= {"white_balance"}


def test_offload_schema_is_safe_for_llm():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    props = (getattr(tools["offload"], "input_schema", None) or tools["offload"].inputSchema)["properties"]
    assert props["dry_run"]["default"] is True
    assert set(props["scope"]["enum"]) == {"camera", "other", "all"}


# Tools that legitimately succeed with no camera / Bluetooth / Drive (they report state, not perform an action).
NO_HARDWARE_OK = {"list_presets", "camera_doctor", "ble_devices", "ble_brands"}
ARGS = {"set_settings": {"settings": {"shutter": "1/50"}}, "show_preset": {"name": "nope"},
        "apply_preset": {"name": "photo"}, "load_config": {"path": "/nonexistent.yaml"},
        "diff_config": {"path": "/nonexistent.yaml"}, "record": {"start": True},
        "calc_shutter": {"fps": 0}, "calc_slowmo": {"capture_fps": 60, "timeline_fps": 0},
        "calc_nd": {"metered_shutter": "fast"}, "ble_scan": {"timeout": 0}, "offload": {"scope": "camera"},
        "ble_command": {"command": "c1"}}


def test_every_tool_returns_json_on_error_and_never_writes_stdout(monkeypatch, capsys):
    import camctl.offload as off

    def no_cam():
        raise CameraError("no camera")

    async def no_ble(timeout=6.0, **kw):
        raise ble.BluetoothUnavailable("bluetooth off")

    class NoDrive:
        def __init__(self):
            raise RuntimeError("no creds")

    def no_drive(*a, **k):
        raise FileNotFoundError("No Sony card mounted")
    monkeypatch.setattr(srv, "_camera", no_cam)
    monkeypatch.setattr(ble, "scan", no_ble)
    monkeypatch.setattr(ble.api, "scan", no_ble)       # pair/execute resolve cameras through api.scan
    monkeypatch.setattr(off, "offload", no_drive)
    monkeypatch.setattr(off, "find_cards", lambda: [])
    import camctl.drive as drive
    monkeypatch.setattr(drive, "Drive", NoDrive)
    for t in asyncio.run(srv.mcp.list_tools()):
        blocks = _content(asyncio.run(srv.mcp.call_tool(t.name, ARGS.get(t.name, {}))))
        if t.name in NO_HARDWARE_OK:
            data = json.loads(blocks[0].text)
            assert "error" not in data, t.name
            if t.name == "camera_doctor":
                assert data["ok"] is False and data["problems"]
            continue
        data = json.loads(blocks[0].text)
        assert isinstance(data.get("error"), str) and data["error"], t.name
        assert "division" not in data["error"], t.name
    assert capsys.readouterr().out == ""


# -- BLE protocol ----------------------------------------------------------------------

class FakeChar:
    def __init__(self, short, props):
        self.uuid = f"8000{short}-ff00-ffff-ffff-ffffffffffff"
        self.properties = props


class FakeService:
    def __init__(self, write_props=("write",)):
        self.characteristics = [FakeChar("ff01", list(write_props)), FakeChar("ff02", ["notify"])]


class FakeServices:
    def __init__(self, svc):
        self.svc = svc

    def get_service(self, uuid):
        return self.svc if uuid.lower() == ble.REMOTE_SERVICE else None


class FakeBleakClient:
    """Records writes; answers focus half-press with a focus-acquired notification like the camera."""

    instances: list = []

    def __init__(self, address, timeout=10.0, write_props=("write",), focus_lock=True, fail_disconnect=False):
        self.address, self.writes, self.connected = address, [], False
        self.services = FakeServices(FakeService(write_props))
        self.focus_lock, self.fail_disconnect, self.cb = focus_lock, fail_disconnect, None
        FakeBleakClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False
        if self.fail_disconnect:
            raise OSError("already disconnected")

    async def start_notify(self, char, cb):
        self.cb = cb

    async def write_gatt_char(self, char, data, response=False):
        if not self.connected:
            raise OSError("not connected")
        self.writes.append((bytes(data), response))
        if bytes(data) == b"\x01\x07" and self.focus_lock and self.cb:
            self.cb(char, bytearray(b"\x02\x3f\x20"))


@pytest.fixture
def bleak(monkeypatch):
    import bleak as real
    FakeBleakClient.instances.clear()
    opts = {}

    def factory(address, timeout=10.0):
        return FakeBleakClient(address, timeout, **opts)
    monkeypatch.setattr(real, "BleakClient", factory)
    monkeypatch.setattr(ble.asyncio, "sleep", _nosleep)
    return opts


async def _nosleep(_s=0):
    return None


def _run_remote(coro_fn):
    async def go():
        async with ble.BleRemote("AA") as r:
            await coro_fn(r)
            return r
    return asyncio.run(go())


def test_shoot_sequence_waits_for_focus(bleak):
    r = _run_remote(lambda r: r.shoot())
    writes = FakeBleakClient.instances[0].writes
    assert [w for w, _ in writes] == [b"\x01\x07", b"\x01\x09", b"\x01\x08", b"\x01\x06"]
    assert r.status["focus"] is True
    assert not FakeBleakClient.instances[0].connected


def test_write_response_follows_characteristic_properties(bleak):
    _run_remote(lambda r: r.shoot())
    assert all(resp is True for _, resp in FakeBleakClient.instances[0].writes)
    bleak["write_props"] = ("write-without-response",)
    _run_remote(lambda r: r.shoot())
    assert all(resp is False for _, resp in FakeBleakClient.instances[1].writes)


def test_zoom_and_focus_bytes(bleak):
    _run_remote(lambda r: r.zoom("tele", 0.1))
    _run_remote(lambda r: r.zoom("wide", 0.1))
    _run_remote(lambda r: r.focus("in", 0.1))
    _run_remote(lambda r: r.focus("out", 0.1))
    w = [[x for x, _ in c.writes] for c in FakeBleakClient.instances]
    assert w[0] == [b"\x02\x45\x30", b"\x02\x44\x00"]     # tele press 0x10–0x8F, release
    assert w[1] == [b"\x02\x47\x30", b"\x02\x46\x00"]
    assert w[2] == [b"\x02\x6b\x20", b"\x02\x6a\x00"]     # near
    assert w[3] == [b"\x02\x6d\x20", b"\x02\x6c\x00"]     # far
    assert ble.zoom_cmd("tele", 500, True) == b"\x02\x45\x8f" and ble.focus_cmd("in", 0, True)[2] == 1


def test_record_toggle_and_bulb(bleak):
    _run_remote(lambda r: r.record_toggle())
    _run_remote(lambda r: r.bulb(2))
    w = [[x for x, _ in c.writes] for c in FakeBleakClient.instances]
    assert w[0] == [b"\x01\x0f", b"\x01\x0e"]
    assert w[1] == [b"\x01\x07", b"\x01\x09", b"\x01\x08", b"\x01\x06"]


def test_interrupted_hold_still_releases(bleak, monkeypatch):
    async def cancelled_sleep(s=0):
        if s >= 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(ble.asyncio, "sleep", cancelled_sleep)
    for action in (lambda r: r.bulb(5), lambda r: r.zoom("tele", 5), lambda r: r.press("afon_down", "afon_up", 5)):
        with pytest.raises(asyncio.CancelledError):
            _run_remote(action)
    w = [[x for x, _ in c.writes] for c in FakeBleakClient.instances]
    assert w[0][-2:] == [b"\x01\x08", b"\x01\x06"]
    assert w[1][-1] == b"\x02\x44\x00"
    assert w[2][-1] == b"\x01\x14"


def test_disconnect_error_does_not_mask_result(bleak):
    bleak["fail_disconnect"] = True
    r = _run_remote(lambda r: r.shoot())
    assert r.status["focus"] is True


def test_scan_filters_out_sony_headphones(monkeypatch):
    import bleak as real

    class Dev:
        def __init__(self, address, name):
            self.address, self.name = address, name

    class Adv:
        def __init__(self, mfg, name, rssi):
            self.manufacturer_data, self.local_name, self.rssi = {0x012D: mfg}, name, rssi

    class Scanner:
        def __init__(self, cb):
            self.cb = cb

        async def __aenter__(self):
            self.cb(Dev("CAM", None), Adv(bytes.fromhex("03006400453122ef00"), "ILCE-7M5", -60))
            self.cb(Dev("HP", None), Adv(bytes.fromhex("040022ef0102"), "WH-1000XM5", -30))
            self.cb(Dev("OTHER", None), Adv(b"", None, -20))
            return self

        async def __aexit__(self, *exc):
            pass
    monkeypatch.setattr(real, "BleakScanner", Scanner)
    monkeypatch.setattr(ble.asyncio, "sleep", _nosleep)
    found = asyncio.run(ble.scan(0))
    assert [f.address for f in found] == ["CAM"] and found[0].pairing_mode is True
    assert ble._pairing_flag(bytes.fromhex("030064004531" "22af00")) is False


# -- presets ----------------------------------------------------------------------------

ALL = presets._builtin()
VIDEO_FPS_SHUTTER = {"24p": "1/50", "30p": "1/60", "60p": "1/125", "120p": "1/250"}


def _text(p):
    return json.dumps(p, ensure_ascii=False).lower()


@pytest.mark.parametrize("name", list(ALL))
def test_preset_single_card(name):
    p = ALL[name]
    assert "simultaneous" not in _text(p) and "dual" not in _text(p) and "second card" not in _text(p)
    assert str(p["settings"].get("rec_media_movie", "")).lower() != "simultaneous"


@pytest.mark.parametrize("name", [n for n, p in ALL.items() if "movie_frame_rate" in p["settings"]])
def test_video_presets_follow_180_degree_rule(name):
    s = ALL[name]["settings"]
    assert s["shutter"] == VIDEO_FPS_SHUTTER[s["movie_frame_rate"]]
    fps = float(s["movie_frame_rate"].rstrip("p"))
    assert advisor.shutter_for_fps(fps)["shutter"] == s["shutter"]


@pytest.mark.parametrize("name", list(ALL))
def test_log_and_display_assist_consistency(name):
    s = ALL[name]["settings"]
    log = str(s.get("log_shooting", "Off")).lower().startswith("on")
    if log:
        assert str(s["iso"]).isdigit() and int(s["iso"]) >= 800
        assert s.get("monitor_lut") == "On" and s.get("gamma_display_assist") == "Off"   # one preview LUT only
    else:
        assert s.get("gamma_display_assist") == "Off", "non-Log presets must clear the S-Log3 view LUT"
        assert "gamma_display_assist_type" not in s
        assert "log_gamut" not in s


LOG_STEP = re.compile(r"log shooting\s*(:|→)|s-gamut3\.cine/s-log3|color gamut", re.I)


@pytest.mark.parametrize("name", list(ALL))
def test_manual_does_not_duplicate_auto_steps(name, cam):
    p = ALL[name]
    manual = p.get("manual", [])
    if any(k in p["settings"] for k in ("log_shooting", "log_gamut")):
        assert not [m for m in manual if LOG_STEP.search(m)], "core already adds the Log camera-menu steps"
    if "movie_frame_rate" in p["settings"]:
        assert not [m for m in manual if "rec frame rate" in m.lower()], "frame rate is set over USB"
    res = core.apply_settings(cam, p["settings"], dry_run=True, manual=manual)
    assert len(res.manual) == len(set(res.manual))
