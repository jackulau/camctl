import json

import pytest
import yaml

from camctl import core


def by_key(steps):
    return {s["key"]: s for s in steps}


def test_dry_run_reports_without_writing(cam):
    res = core.apply_settings(cam, {"white_balance": "daylight", "focus_mode": "AF-A", "movie_setting": "100M 4:2:2 10bit"},
                              dry_run=True).as_dict()
    applied = by_key(res["applied"])
    assert applied["white_balance"]["status"] == "would-set"
    assert applied["white_balance"]["target"] == "Daylight"
    assert applied["movie_setting"]["target"] == "100M 4:2:2 10bit"     # enum codes shown as labels
    assert applied["movie_setting"]["current"] == "50M 4:2:0 8bit"
    assert res["unchanged"] == ["focus_mode"]
    assert cam.writes == []


def test_dry_run_readonly_exposure_blocks_by_dial(cam):
    res = core.apply_settings(cam, {"exposure_program": "M", "shutter": "1/50"}, dry_run=True)
    d = res.as_dict()
    assert d["blocked_by_mode_dial"] is True
    problems = by_key(d["problems"])
    assert problems["exposure_program"]["status"] == "readonly"
    assert problems["shutter"]["status"] == "readonly"
    assert "mode dial" in d["manual_on_camera"][0]
    assert cam.writes == []                         # dry run never touches d25a


def test_log_shooting_is_camera_menu_not_a_problem(cam):
    res = core.apply_settings(cam, {"log_shooting": "On", "log_gamut": "S-Gamut3.Cine"}, dry_run=True,
                              manual=["Mode dial → M"])
    d = res.as_dict()
    steps = {s.key: s for s in res.steps}
    assert steps["log_shooting"].status == "camera-menu"
    assert steps["log_gamut"].status == "camera-menu"
    assert d["problems"] == []
    assert d["manual_on_camera"][0] == "Mode dial → M"
    assert any("Log Shooting: On" in m for m in d["manual_on_camera"])
    assert any("Color Gamut: S-Gamut3.Cine" in m for m in d["manual_on_camera"])


def test_apply_unlocks_mode_via_priority_key_then_sets_exposure(cam):
    res = core.apply_settings(cam, {"shutter": "1/50", "exposure_program": "M", "iso": 800, "aperture": 2.8})
    d = res.as_dict()
    assert d["problems"] == [], d["problems"]
    assert d["blocked_by_mode_dial"] is False
    assert cam.writes[:2] == [("d25a", "1"), ("500e", "1")]      # priority key before the mode write
    assert cam.flat["shutterspeed"]["value"] == "1/50"
    assert cam.flat["iso"]["value"] == "800"
    assert cam.flat["f-number"]["value"] == "f/2.8"
    assert by_key(d["applied"])["exposure_program"]["target"] == "M"


def test_missing_and_no_match(cam):
    d = core.apply_settings(cam, {"creative_look": "ST", "white_balance": "purple"}, dry_run=True).as_dict()
    problems = by_key(d["problems"])
    assert problems["creative_look"]["status"] == "missing"
    assert problems["white_balance"]["status"] == "no-match"


def test_range_rounding(cam):
    d = core.apply_settings(cam, {"color_temp": "5620"}, dry_run=True).as_dict()
    step = by_key(d["applied"])["color_temp"]
    assert step["target"] == 5600 and "rounded" in step["note"]


def test_set_error_is_reported_per_key(cam):
    from camctl.ptp import CameraError

    def boom(name, value):
        raise CameraError("focusmode: Bad parameters")
    cam.set = boom
    d = core.apply_settings(cam, {"focus_mode": "AF-C"}).as_dict()
    assert by_key(d["problems"])["focus_mode"]["status"] == "error"


def test_read_settings_file_preset(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump({"description": "x", "settings": {"shutter": "1/50", "iso": 800},
                                 "manual": ["Mode dial → M"]}))
    settings, manual = core.read_settings_file(p)
    assert settings == {"shutter": "1/50", "iso": 800}
    assert manual == ["Mode dial → M"]


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_dump_then_read_settings_file(cam, tmp_path, suffix):
    path = core.dump(cam, tmp_path / f"dump{suffix}", meta={"reason": "test"})
    doc = json.loads(path.read_text()) if suffix == ".json" else yaml.safe_load(path.read_text())
    assert doc["camctl_dump"] == 1 and doc["model"] == "ILCE-7M5" and doc["reason"] == "test"
    settings, manual = core.read_settings_file(path)
    assert manual == []
    assert settings["whitebalance"] == "Automatic"
    assert settings["d242"] == "63"
    for excluded in ("shutterspeed", "500e", "cameramodel", "batterylevel", "movie"):   # readonly / never-restore
        assert excluded not in settings


def test_dump_default_path_goes_to_backup_dir(cam):
    path = core.dump(cam)
    assert path.parent == core.BACKUP_DIR and path.name.startswith("ILCE-7M5-")


def test_read_settings_file_bare_probe_and_invalid(tmp_path):
    bare = tmp_path / "original.json"
    bare.write_text(json.dumps({"iso": {"value": "800", "readonly": False},
                                "serialnumber": {"value": "123", "readonly": False}}))
    assert core.read_settings_file(bare) == ({"iso": "800"}, [])
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: 1\n")
    with pytest.raises(ValueError):
        core.read_settings_file(bad)


def test_diff_raw_dump_values(cam):
    out = core.diff(cam, {"whitebalance": "Automatic", "d242": "54", "nope": 1})
    keys = {d["key"] for d in out}
    assert keys == {"d242", "nope"}


def test_diff_preset_labels_match_camera(cam):
    cam.flat["d242"]["value"] = "54"
    cam.flat["f-number"]["value"] = "f/2.8"
    assert core.diff(cam, {"movie_setting": "100M 4:2:2 10bit", "aperture": 2.8}) == []


def test_current_settings(cam):
    cur = core.current_settings(cam)
    assert cur["exposure_program"] == {"value": "Intelligent Auto", "writable": False, "widget": "500e"}
    assert cur["movie_setting"]["value"] == "50M 4:2:0 8bit"
    assert cur["subject_recognition"]["value"] == "Off"
    assert cur["cameramodel"]["value"] == "ILCE-7M5"
    assert "log_shooting" not in cur
