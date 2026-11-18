from camctl.settings import enum_code, label_for, match_choice, resolve_widget, sort_for_apply

from .conftest import SHUTTERS, widget_map


def test_match_choice_exact_and_numeric():
    assert match_choice("shutter", "1/250", SHUTTERS).value == "1/250"
    m = match_choice("aperture", "2.8", ["f/2", "f/2.8", "f/4"])
    assert (m.value, m.exact) == ("f/2.8", True)
    assert match_choice("aperture", "F4", ["f/2", "f/2.8", "f/4"]).value == "f/4"


def test_match_choice_auto_and_synonyms():
    flat = widget_map()
    assert match_choice("iso", "auto", flat["iso"]["choices"]).value == "Auto ISO"
    assert match_choice("white_balance", "color_temp", flat["whitebalance"]["choices"]).value == \
        "Choose Color Temperature"
    assert match_choice("white_balance", "auto", flat["whitebalance"]["choices"]).value == "Automatic"
    assert match_choice("focus_mode", "AF-C", flat["focusmode"]["choices"]).value == "AF-C"
    assert match_choice("focus_mode", "mf", flat["focusmode"]["choices"]).value == "Manual"


def test_match_choice_snaps_to_nearest_stop():
    m = match_choice("shutter", "1/240", SHUTTERS)
    assert m.value == "1/250" and not m.exact and "snapped" in m.note


def test_match_choice_no_match():
    m = match_choice("white_balance", "purple", ["Automatic", "Daylight"])
    assert m.value is None and m.alternatives == ["Automatic", "Daylight"]


def test_enum_code_movie_setting():
    choices = widget_map()["d242"]["choices"]
    m = enum_code("movie_setting", "100M 4:2:2 10bit", choices)
    assert (m.value, m.exact) == ("54", True)


def test_enum_code_partial_picks_highest_bitrate():
    m = enum_code("movie_setting", "4:2:2 10bit", ["54", "61", "63"])   # 100M 4:2:2, 50M 4:2:2, 50M 4:2:0 8bit
    assert m.value == "54"


def test_enum_code_subject_recognition_on_is_not_log_label():
    m = enum_code("subject_recognition", "On", ["2", "1"])
    assert m.value == "2"
    assert enum_code("subject_recognition", "Off", ["2", "1"]).value == "1"


def test_enum_code_on_alias_for_log_shooting():
    assert enum_code("log_shooting", "On", ["1", "1281"]).value == "1281"


def test_enum_code_raw_code_and_not_offered():
    assert enum_code("movie_setting", "63", ["54", "63"]).value == "63"
    m = enum_code("movie_setting", "600M 4:2:2 10bit", ["54", "63"])
    assert m.value is None and "not offered" in m.note and "100M 4:2:2 10bit" in m.alternatives


def test_enum_code_exposure_program_labels():
    choices = widget_map()["500e"]["choices"]
    assert enum_code("exposure_program", "M", choices).value == "1"
    assert enum_code("exposure_program", "Movie (M)", choices).value == "491603"


def test_label_for():
    assert label_for("movie_setting", "54") == "100M 4:2:2 10bit"
    assert label_for("movie_setting", 999) == "999"
    assert label_for("shutter", "1/50") == "1/50"


def test_resolve_widget():
    flat = widget_map()
    assert resolve_widget("shutter", flat) == "shutterspeed"
    assert resolve_widget("exposure_program", flat) == "500e"
    assert resolve_widget("d242", flat) == "d242"               # raw widget name passes through
    assert resolve_widget("log_shooting", flat) is None
    assert resolve_widget("not_a_key", flat) is None


def test_sort_for_apply_mode_first():
    order = sort_for_apply(["shutter", "color_temp", "white_balance", "exposure_program", "jpegquality"])
    assert order[0] == "exposure_program"
    assert order.index("white_balance") < order.index("color_temp")
    assert order[-1] == "jpegquality"
