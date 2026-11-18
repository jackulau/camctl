import asyncio
import json
from pathlib import Path

import pytest

from camctl import mcp_server as srv
from camctl.ptp import CameraError

from .conftest import FakeCamera

EXPECTED_TOOLS = {"camera_status", "list_presets", "show_preset", "apply_preset", "set_settings", "dump_config",
                  "load_config", "diff_config", "capture", "record", "liveview_jpeg", "ble_scan", "ble_shoot",
                  "ble_record", "offload", "calc_shutter", "calc_slowmo", "calc_nd",
                  "camera_doctor", "ble_pair", "ble_command", "ble_devices", "ble_brands", "drive_organize"}


@pytest.fixture
def fake(monkeypatch):
    cam = FakeCamera()
    monkeypatch.setattr(srv, "_camera", lambda: cam)
    return cam


def test_tools_registered():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    assert EXPECTED_TOOLS <= set(tools)
    assert tools["offload"].description


def test_guide_resource():
    resources = asyncio.run(srv.mcp.list_resources())
    assert any(str(r.uri) == "camctl://guide" for r in resources)
    text = srv.guide_markdown()
    assert "### cinematic-24" in text and "- [ ] Mode dial → M" in text
    for fact in ("d25a", "500e", "session-scoped", "Diff. Set for Still/Mv", "S-Gamut3.Cine", "7-9",
                 "focus can't lock"):
        assert fact in text


def test_call_tool_through_server():
    result = asyncio.run(srv.mcp.call_tool("calc_shutter", {"fps": 24}))
    blocks = getattr(result, "content", result)          # mcp 2.x CallToolResult | 1.x content list
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    assert json.loads(blocks[0].text)["shutter"] == "1/50"


def test_camera_error_becomes_error_dict(monkeypatch):
    def unavailable():
        raise CameraError("no camera found — check USB-C cable")
    monkeypatch.setattr(srv, "_camera", unavailable)
    assert srv.set_settings({"shutter": "1/50"}) == {"error": "no camera found — check USB-C cable"}
    assert "error" in srv.camera_status()
    assert "error" in srv.capture()


def test_camera_status(fake):
    # now vendors.capabilities() + ok (the raw libgphoto2 summary text was dropped: long, and vendor/model/
    # firmware/battery are reported as fields)
    r = srv.camera_status()
    assert r["ok"] is True and r["vendor"] == "sony" and r["model"] == "ILCE-7M5" and r["battery"] == "80%"
    assert r["settings"]["exposure_program"]["value"] == "Intelligent Auto"
    assert r["settings"]["shutter"]["writable"] is False and "mode dial" in r["settings"]["shutter"]["locked_reason"]
    assert "iso_auto" in r["unsupported"] and r["warnings"] == []
    assert fake.opened == fake.closed == 1          # session never kept between calls
    json.dumps(r)


def test_camera_status_without_camera_gives_setup_hints(monkeypatch):
    def unavailable():
        raise CameraError("no camera")
    monkeypatch.setattr(srv, "_camera", unavailable)
    r = srv.camera_status()
    assert r["ok"] is False and r["error"] == "no camera"
    assert {"Sony", "Canon", "Nikon", "Fujifilm", "Panasonic"} <= set(r["setup_hints"])


def test_set_settings_dry_run(fake):
    r = srv.set_settings({"white_balance": "daylight"}, dry_run=True)
    assert r["applied"][0]["status"] == "would-set" and fake.writes == []
    r = srv.set_settings({"white_balance": "daylight"})
    assert r["applied"][0]["status"] == "ok" and fake.flat["whitebalance"]["value"] == "Daylight"
    json.dumps(r)


def test_presets_tools(fake):
    assert "cinematic-24" in srv.list_presets()
    assert srv.show_preset("photo")["settings"]["shutter"] == "1/250"
    assert "unknown preset" in srv.show_preset("nope")["error"]


def test_apply_preset_dry_run_no_backup(fake):
    r = srv.apply_preset("cinematic-24", dry_run=True)
    assert r["backup"] is None and r["dry_run"] is True
    assert any("Log Shooting: On" in m for m in r["manual_on_camera"])
    assert fake.writes == []
    json.dumps(r)


def test_apply_preset_backs_up_first(fake):
    r = srv.apply_preset("easy-video")
    assert r["backup"] and Path(r["backup"]).exists()
    assert fake.flat["500e"]["value"] == "491603"                 # Movie (M) via d25a unlock


def test_dump_load_diff(fake, tmp_path):
    path = tmp_path / "d.yaml"
    assert srv.dump_config(str(path)) == {"path": str(path)}
    fake.flat["whitebalance"]["value"] = "Shade"
    assert srv.diff_config(str(path))["differences"][0]["key"] == "whitebalance"
    r = srv.load_config(str(path))                                # dry run by default
    assert r["dry_run"] is True and fake.flat["whitebalance"]["value"] == "Shade"
    r = srv.load_config(str(path), dry_run=False)
    assert fake.flat["whitebalance"]["value"] == "Automatic" and Path(r["backup"]).exists()
    assert "error" in srv.load_config(str(tmp_path / "missing.yaml"))


def test_capture_default_dir(fake, monkeypatch, tmp_path):
    seen = {}

    def cap(dest):
        seen["dest"] = dest
        return [dest / "DSC0001.ARW", dest / "DSC0001.JPG"]
    fake.capture = cap
    monkeypatch.setattr(srv, "DEFAULT_CAPTURE_DIR", tmp_path / "pics")
    assert srv.capture() == {"files": [str(tmp_path / "pics" / "DSC0001.ARW"), str(tmp_path / "pics" / "DSC0001.JPG")]}
    srv.capture(str(tmp_path / "x"))
    assert seen["dest"] == tmp_path / "x"


def test_record_and_liveview(fake):
    assert srv.record(True) == {"recording": True} and fake.flat["movie"]["value"] == 1
    img = srv.liveview_jpeg()
    assert isinstance(img, srv.Image) and img.data.startswith(b"\xff\xd8")


def test_calc_tools():
    assert srv.calc_slowmo(60, 24)["clip_speed_percent"] == 40.0
    assert srv.calc_nd("1/1600")["nd_factor"] == "ND32"
    assert "error" in srv.calc_nd("fast")


def test_offload_defaults_are_safe(monkeypatch, capsys):
    import camctl.offload as off
    calls = {}

    class R:
        safe_to_format = False

        def summary(self):
            return {"files": 3}

    def fake_offload(cards, root_name, jobs, dry_run, scope, log, organize, rehash):
        calls.update(cards=cards, root_name=root_name, dry_run=dry_run, scope=scope, organize=organize,
                     rehash=rehash)
        log("progress line")
        return R()
    monkeypatch.setattr(off, "offload", fake_offload)
    r = srv.offload()
    assert r == {"dry_run": True, "root": "Card Archive", "layout": "organized", "files": 3}
    assert calls["dry_run"] is True and calls["cards"] is None and calls["organize"] is True
    assert srv.offload(scope="other")["root"] == "Card Archive"
    assert srv.offload(layout="dated")["root"] == "A7V Offload" and calls["organize"] is False
    assert srv.offload(scope="all", layout="dated")["root"] == "Card Archive"
    assert "error" in srv.offload(scope="bogus")
    assert "error" in srv.offload(layout="bogus")
    out = capsys.readouterr()
    assert out.out == "" and "progress line" in out.err                # stdout is the MCP channel


class FakeBle:
    """Records ble.execute / pair calls; prompts go through on_prompt like the real drivers."""

    def __init__(self, monkeypatch, result=None, error=None):
        import camctl.ble as ble
        self.calls = []
        self.result = result if result is not None else {"brand": "sony", "address": "AA", "ok": True}
        self.error = error

        async def execute(cmd, args=None, **kw):
            self.calls.append(("execute", cmd, args, kw))
            if kw.get("on_prompt"):
                kw["on_prompt"]("connecting to AA")
            if self.error:
                raise self.error
            return {**self.result, "via": "direct"}

        async def pair(address=None, brand=None, **kw):
            self.calls.append(("pair", address, brand, kw))
            kw["on_prompt"]("press OK on the camera")
            if self.error:
                raise self.error
            return {"brand": "canon", "address": "CC", "name": "EOS R6", "bonded": False, "verified": True,
                    "extra": {}, "notes": []}
        monkeypatch.setattr(ble, "execute", execute)
        monkeypatch.setattr(ble, "pair", pair)


def test_ble_shoot_and_record_use_execute(monkeypatch, capsys):
    fb = FakeBle(monkeypatch, {"brand": "sony", "address": "AA", "released": False, "hint": "focus did not lock"})
    r = asyncio.run(srv.ble_shoot(af=False, burst=2.0))
    assert r["released"] is False and r["hint"] == "focus did not lock" and r["prompts"] == ["connecting to AA"]
    assert fb.calls[0][1:3] == ("shoot", {"af": False, "burst": 2.0})
    asyncio.run(srv.ble_record(address="CC", brand="canon", action="start"))
    assert fb.calls[1][1:3] == ("record", {"action": "start"})
    assert fb.calls[1][3]["address"] == "CC" and fb.calls[1][3]["brand"] == "canon"
    assert capsys.readouterr().out == ""                                # prompts go to stderr, never stdout


def test_ble_command_filters_args_per_command(monkeypatch):
    fb = FakeBle(monkeypatch)
    asyncio.run(srv.ble_command("zoom", seconds=1.0, direction="tele", speed=40))
    asyncio.run(srv.ble_command("c1", seconds=3.0, direction="in"))
    asyncio.run(srv.ble_command("bulb", seconds=30))
    assert [c[1:3] for c in fb.calls] == [("zoom", {"seconds": 1.0, "direction": "tele", "speed": 40}),
                                         ("c1", {}), ("bulb", {"seconds": 30})]


def test_ble_errors_keep_prompts(monkeypatch):
    import camctl.ble as ble
    FakeBle(monkeypatch, error=ble.BondRestartRequired("run: blueutil --unpair aa-bb"))
    r = asyncio.run(srv.ble_pair(brand="canon"))
    assert r == {"error": "run: blueutil --unpair aa-bb", "type": "BondRestartRequired",
                 "prompts": ["press OK on the camera"]}


def test_ble_pair_defaults_never_restart_bluetooth(monkeypatch):
    fb = FakeBle(monkeypatch)
    r = asyncio.run(srv.ble_pair())
    assert r["address"] == "CC" and r["prompts"] == ["press OK on the camera"]
    assert fb.calls[0][3]["allow_bt_restart"] is False and fb.calls[0][3]["verify"] is True
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    props = (getattr(tools["ble_pair"], "input_schema", None) or tools["ble_pair"].inputSchema)["properties"]
    assert props["allow_bt_restart"]["default"] is False
    assert "disconnects EVERY Bluetooth device" in tools["ble_pair"].description


def test_ble_scan_devices_brands(monkeypatch):
    import camctl.ble as ble
    seen = {}

    async def scan(timeout, brands=None, include_unsupported=True):
        seen.update(timeout=timeout, brands=brands, include_unsupported=include_unsupported)
        return [ble.Found("AA", "ILCE-7M5", -50, True), ble.Found("OM", "OM-1", -60, None, brand="omsystem")]
    monkeypatch.setattr(ble, "scan", scan)
    r = asyncio.run(srv.ble_scan(0.1))
    assert [(c["brand"], c["supported"]) for c in r["cameras"]] == [("sony", True), ("omsystem", False)]
    assert seen == {"timeout": 0.1, "brands": None, "include_unsupported": True}
    asyncio.run(srv.ble_scan(0.1, brands=["canon"], supported_only=True))
    assert seen["brands"] == ["canon"] and seen["include_unsupported"] is False

    assert asyncio.run(srv.ble_devices()) == {"default": None, "devices": [], "daemon": None}
    ble.remember("AA", "ILCE-7M5", "sony")
    ble.remember("CC", "EOS R6", "canon", default=False)
    d = asyncio.run(srv.ble_devices())
    assert d["default"] == "AA" and [x["default"] for x in d["devices"]] == [True, False]
    assert "extra" not in d["devices"][0]
    brands = srv.ble_brands()
    assert brands["sony"]["supported"] and not brands["omsystem"]["supported"]


def test_camera_doctor_tool(fake, monkeypatch):
    import camctl.ptp as ptp
    import camctl.drive as drive

    class NoDrive:
        def __init__(self):
            raise RuntimeError("no creds")
    monkeypatch.setattr(ptp, "autodetect", lambda: [("USB PTP Class Camera", "usb:002,001")])
    monkeypatch.setattr(drive, "Drive", NoDrive)
    monkeypatch.setattr("camctl.doctor._holders", lambda: [])
    monkeypatch.setattr("camctl.offload.find_cards", lambda: [])
    r = asyncio.run(srv.camera_doctor())
    assert r["ok"] is True and r["usb"]["camera"]["vendor"] == "sony" and r["usb"]["camera"]["writable_count"] > 0
    assert any("Drive" in w for w in r["warnings"]) and fake.opened == fake.closed == 1
    json.dumps(r)
