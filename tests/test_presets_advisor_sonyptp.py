import re
import struct

import pytest

from camctl import advisor, presets
from camctl.settings import CANONICAL_BY_KEY, MENU_ONLY
from camctl.sonyptp import DT_STR, pack_value, parse_props

WIDGET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# -- presets -------------------------------------------------------------------

def test_builtin_presets_well_formed():
    allp = presets.load_all()
    assert {"photo", "cinematic-24", "slowmo-60", "easy-video", "low-light"} <= set(allp)
    for name, p in allp.items():
        assert p["source"] == "builtin"
        assert p.get("description"), name
        assert isinstance(p["settings"], dict) and p["settings"], name
        for key in p["settings"]:
            assert key in CANONICAL_BY_KEY or WIDGET_NAME.match(key), f"{name}: odd key {key!r}"
        assert all(isinstance(m, str) for m in p.get("manual", [])), name


def test_menu_only_keys_are_canonical():
    assert set(MENU_ONLY) <= set(CANONICAL_BY_KEY)


def test_user_preset_overrides_and_get(tmp_path):
    presets.save("mine", {"shutter": "1/100"}, "my preset", manual=["do X"])
    allp = presets.load_all()
    assert allp["mine"]["settings"] == {"shutter": "1/100"}
    assert allp["mine"]["source"].endswith("mine.yaml")
    with pytest.raises(KeyError):
        presets.get("does-not-exist")


# -- advisor ---------------------------------------------------------------------

@pytest.mark.parametrize("fps,shutter", [(24, "1/50"), (30, "1/60"), (60, "1/125"), (120, "1/250"),
                                         (25, "1/50"), (50, "1/100")])
def test_shutter_for_fps(fps, shutter):
    assert advisor.shutter_for_fps(fps)["shutter"] == shutter


def test_shutter_angle():
    r = advisor.shutter_for_fps(24, 90)
    assert r["shutter"] == "1/100" and r["ideal"] == "1/96"


def test_slowmo():
    r = advisor.slowmo(60, 24)
    assert r["slowdown"] == 2.5 and r["clip_speed_percent"] == 40.0
    assert advisor.slowmo(120)["clip_speed_percent"] == 20.0


def test_nd_for():
    r = advisor.nd_for("1/1600", "1/50")
    assert r["stops"] == 5.0 and r["nd_factor"] == "ND32" and r["optical_density"] == 1.5
    assert advisor.nd_for("1/25", "1/50")["nd_factor"] == 1
    assert advisor.nd_for("0.5s", "2")["stops"] == 2.0


# -- sonyptp DPD parsing -----------------------------------------------------------

U8, U16, U32 = 0x0002, 0x0004, 0x0006


def dpd(code, dt, getset, enabled, default, current, form=None):
    rec = struct.pack("<HHBB", code, dt, getset, enabled) + pack_value(default, dt) + pack_value(current, dt)
    if form is None:
        return rec + b"\x00"
    kind, *lists = form
    if kind == "range":
        return rec + b"\x01" + b"".join(pack_value(v, dt) for v in lists[0])
    body = b"\x02"
    for values in lists:                    # enum list, then optional settable list
        body += struct.pack("<H", len(values)) + b"".join(pack_value(v, dt) for v in values)
    return rec + body


def test_parse_props_round_trip():
    records = [
        dpd(0xD242, U16, 1, 1, 63, 54, ("enum", [54, 61, 63], [54, 63])),
        dpd(0xE0E3, U32, 1, 1, 1, 1281, ("enum", [1, 1281], [1, 1281])),
        dpd(0xD1B6, U32, 1, 2, 100, 200, ("range", [100, 25600, 1])),
        dpd(0xD25A, U8, 1, 1, 0, 1, ("enum", [0, 1], [0, 1])),
        dpd(0xD20E, DT_STR, 0, 1, "", "ILCE-7M5"),
    ]
    blob = struct.pack("<II", len(records), 0) + b"".join(records)
    props = parse_props(blob)
    assert list(props) == [0xD242, 0xE0E3, 0xD1B6, 0xD25A, 0xD20E]

    p = props[0xD242]
    assert (p.datatype, p.default, p.current, p.form) == (U16, 63, 54, "enum")
    assert p.values == [54, 61, 63] and p.settable_values == [54, 63] and p.writable

    log = props[0xE0E3]
    assert log.current == 1281 and log.values == [1, 1281] and log.settable_values == [1, 1281]

    rng = props[0xD1B6]
    assert rng.form == "range" and rng.values == [100, 25600, 1] and not rng.writable   # display-only

    assert props[0xD25A].current == 1 and props[0xD25A].values == [0, 1]

    s = props[0xD20E]
    assert s.current == "ILCE-7M5" and s.form == "none" and not s.writable


def test_parse_props_enum_without_second_list():
    rec = dpd(0xD157, U16, 1, 1, 1, 2, ("enum", [1, 2]))
    nxt = dpd(0xD158, U16, 1, 1, 1, 1, ("enum", [1, 2, 3]))
    props = parse_props(struct.pack("<II", 2, 0) + rec + nxt)
    assert props[0xD157].values == [1, 2] and props[0xD157].settable_values == []
    assert props[0xD158].values == [1, 2, 3]


def test_pack_value_formats():
    assert pack_value(54, U16) == b"\x36\x00"
    assert pack_value("1281", U32) == struct.pack("<I", 1281)
    assert pack_value("AB", DT_STR) == bytes([3]) + "AB\x00".encode("utf-16-le")


# -- explain: the Sony name table is optional (not shipped) ------------------------------------------

def _explain_props():
    rec = [dpd(0x500A, U16, 1, 1, 2, 32772, ("enum", [2, 32772], [2, 32772])),
           dpd(0xD157, U16, 1, 1, 1, 2, ("enum", [1, 2]))]
    return parse_props(struct.pack("<II", len(rec), 0) + b"".join(rec))


def test_explain_works_without_the_sony_table():
    from camctl.sonyptp import explain
    rows = {r["code"]: r for r in explain(_explain_props())}       # conftest points the table at a missing file
    assert rows["500a"]["value"] == "AF-C" and rows["500a"]["choices"] == ["AF-S", "AF-C"]   # camctl's own labels
    assert rows["d157"]["name"] == "?" and rows["d157"]["value"] == "2"


def test_explain_uses_a_local_sony_table(monkeypatch, tmp_path):
    from camctl.sonyptp import explain
    table = tmp_path / "sony_props.json"
    table.write_text('{"d157": {"name": "SomeProp", "values": {"1": "Off", "2": "On"}}}')
    monkeypatch.setenv("CAMCTL_SONY_PROPS", str(table))
    rows = {r["code"]: r for r in explain(_explain_props())}
    assert rows["d157"]["name"] == "SomeProp" and rows["d157"]["value"] == "On"


def test_explain_reports_a_corrupt_sony_table(monkeypatch, tmp_path):
    from camctl.sonyptp import PtpError, explain
    table = tmp_path / "sony_props.json"
    table.write_text("{not json")
    monkeypatch.setenv("CAMCTL_SONY_PROPS", str(table))
    with pytest.raises(PtpError, match="sony_props.json"):
        explain(_explain_props())
