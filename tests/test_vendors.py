"""Multi-vendor layer: detection, per-vendor presets, capture/movie hooks, capabilities (fakes only, no USB).

Vendor fakes are built from libgphoto2 2.5.34 widget names and value formats (config.c); only Sony is
verified on hardware.
"""

from __future__ import annotations

import types

import gphoto2 as gp
import pytest
import yaml

from camctl import core, presets, vendors
from camctl.ptp import CameraError, PtpCamera

from .conftest import SHUTTERS, FakeCamera, widget_map


def text(value, ro=True):
    return {"label": "", "readonly": ro, "type": "text", "value": value}


def radio(value, choices, ro=False):
    return {"label": "", "readonly": ro, "type": "radio", "value": value, "choices": list(choices)}


def toggle(value=0, ro=False):
    return {"label": "", "readonly": ro, "type": "toggle", "value": value}


def rng(value, lo, hi, step, ro=False):
    return {"label": "", "readonly": ro, "type": "range", "value": value, "range": [lo, hi, step]}


def by_key(steps):
    return {s["key"]: s for s in steps}


def statuses(res):
    return {s.key: s.status for s in res.steps}


# -- vendor fakes --------------------------------------------------------------------------------------

class VendorFake(FakeCamera):
    SUMMARY = ""

    def __init__(self, flat) -> None:
        super().__init__()
        self.flat = flat

    def summary(self) -> str:
        return self.SUMMARY


CANON_SHUTTERS = ["bulb", "30", "15", "1", "0.5", "0.3", "1/25", "1/50", "1/60", "1/125", "1/250", "1/500"]


class FakeCanon(VendorFake):
    SUMMARY = "Manufacturer: Canon Inc.\nModel: Canon EOS R6\nVendor Extension ID: 0xb (1.0)\n"

    def __init__(self, mode: str = "Manual", movie_mode_widget: bool = False) -> None:
        modes = ["P", "TV", "AV", "Manual", "Bulb"]
        flat = {
            "manufacturer": text("Canon Inc."), "cameramodel": text("Canon EOS R6"),
            "batterylevel": text("100%"),
            "autoexposuremode": radio(mode, modes, ro=True),          # dial body: follows the dial
            "autoexposuremodedial": radio(mode, modes, ro=True),
            "shutterspeed": radio("1/125", CANON_SHUTTERS, ro=mode in ("AV", "P")),
            "aperture": radio("4", ["1.8", "2.8", "4", "5.6", "8"], ro=mode in ("TV", "P")),
            "iso": radio("Auto", ["Auto", "100", "200", "400", "800", "1600", "3200", "6400"]),
            "exposurecompensation": radio("0", ["-1", "-0.7", "-0.3", "0", "0.3", "0.7", "1"]),
            "whitebalance": radio("Auto", ["Auto", "Daylight", "Shadow", "Cloudy", "Tungsten", "Flash",
                                           "Color Temperature"]),
            "colortemperature": radio("5200", ["2500", "3200", "4000", "5200", "5600", "6500"]),
            "focusmode": radio("One Shot", ["One Shot", "AI Focus", "AI Servo", "Manual"]),
            "afmethod": radio("LiveFace", ["LiveFace", "LiveSpotAF", "Live"]),
            "drivemode": radio("Single", ["Single", "Continuous high speed", "Continuous low speed", "Continuous",
                                          "Timer 10 sec"]),
            "meteringmode": radio("Evaluative", ["Evaluative", "Partial", "Spot", "Center-weighted average"]),
            "imageformat": radio("L", ["L", "M", "S1", "RAW", "RAW + L", "cRAW + L"]),
            "picturestyle": radio("Standard", ["Auto", "Standard", "Portrait", "Neutral"]),
            "capturetarget": radio("Internal RAM", ["Internal RAM", "Memory card"]),
            "viewfinder": toggle(0),
            "movierecordtarget": radio("None", ["Card", "None"]),
            "eosremoterelease": radio("None", ["None", "Press Half", "Press Full", "Release Half", "Release Full"]),
            "bulb": toggle(0),
        }
        if movie_mode_widget:
            flat["eosmoviemode"] = toggle(0)
        super().__init__(flat)


class FakeNikon(VendorFake):
    SUMMARY = "Manufacturer: Nikon Corporation\nModel: Z 6_2\nVendor Extension ID: 0xa (1.0)\n"

    def __init__(self, prohibit: str = "") -> None:
        super().__init__({
            "manufacturer": text("Nikon Corporation"), "cameramodel": text("Z 6_2"),
            "expprogram": radio("A", ["M", "P", "S", "A"], ro=True),
            "shutterspeed2": radio("1/60", ["30", "1", "1/2", "1/50", "1/60", "1/125", "1/250", "1/500", "Bulb"]),
            "shutterspeed": radio("0.0166s", ["30.0000s", "0.0200s", "0.0166s", "0.0040s", "Bulb"]),
            "f-number": radio("f/4", ["f/2.8", "f/4", "f/5.6"]),
            "iso": radio("400", ["100", "200", "400", "800", "1600", "3200", "6400"]),
            "autoiso": radio("Off", ["On", "Off"]),
            "exposurecompensation": radio("0", ["-1", "0", "1"]),
            "whitebalance": radio("Automatic", ["Automatic", "Daylight", "Fluorescent", "Tungsten", "Flash",
                                                "Cloudy", "Shade", "Choose Color Temperature", "Preset"]),
            "d01e": rng(5000.0, 2500.0, 10000.0, 10.0),
            "focusmode": radio("AF-S", ["Manual", "AF-S", "AF-C", "AF-A", "AF-F"]),
            "liveviewafmode": radio("Wide-area AF", ["Pinpoint AF", "Wide-area AF", "Auto-area AF"]),
            "capturemode": radio("Single Shot", ["Single Shot", "Continuous Low Speed", "Continuous High Speed",
                                                 "Quiet Release", "Self-timer"]),
            "exposuremetermode": radio("Multi Spot", ["Center Weighted", "Multi Spot", "Center Spot", "Highlight"]),
            "imagequality": radio("JPEG Fine", ["JPEG Basic", "JPEG Normal", "JPEG Fine", "NEF (Raw)", "NEF+Basic",
                                                "NEF+Normal", "NEF+Fine"]),
            "d200": radio("Standard", ["Auto", "Standard", "Neutral", "Vivid"]),
            "capturetarget": radio("Internal RAM", ["Internal RAM", "Memory card"]),
            "viewfinder": toggle(0), "movie": toggle(0),
            "moviequality": radio("3840x2160; 30p", ["3840x2160; 30p", "3840x2160; 24p", "1920x1080; 60p"]),
            "movieprohibit": text(prohibit), "liveviewprohibit": text(prohibit),
            "bulb": toggle(0),
        })
        self.prohibit = prohibit

    def set(self, name, value):
        if name in ("movie", "viewfinder") and self.prohibit:
            raise CameraError(f"{name}={value!r}: [-1] Unspecified error")
        super().set(name, value)


FUJI_EXPOSURE = ("shutterspeed", "aperture", "iso")


class FakeFuji(VendorFake):
    SUMMARY = "Manufacturer: FUJIFILM\nModel: X-T5\nVendor Extension ID: 0xe (1.0)\n"

    def __init__(self, dials_on_a: bool = True) -> None:
        super().__init__({
            "manufacturer": text("FUJIFILM"), "cameramodel": text("X-T5"),
            "prioritymode": radio("Camera", ["Camera", "USB"]),
            "expprogram": radio("P", ["M", "P", "A", "S"], ro=True),
            "shutterspeed": radio("1/60", SHUTTERS, ro=True),
            "aperture": radio("4", ["2.8", "4", "5.6"], ro=True),
            "iso": radio("Auto", ["Auto", "160", "400", "800", "1600", "3200", "6400"], ro=True),
            "exposurecompensation": radio("0", ["-1", "0", "1"]),
            "whitebalance": radio("Automatic", ["Automatic", "Daylight", "Shade", "Incandescent", "Color Temperature"]),
            "colortemperature": rng(5500.0, 2500.0, 10000.0, 10.0),
            "focusmode": radio("Single-Servo AF", ["Manual", "Single-Servo AF", "Continuous-Servo AF"]),
            "capturemode": radio("Single frame", ["Single frame", "Continuous low speed", "Continuous high speed"]),
            "exposuremetermode": radio("Multi", ["Multi", "Spot", "Average", "Center Weighted"]),
            "imageformat": radio("JPEG Fine", ["RAW", "JPEG Fine", "JPEG Normal", "RAW + JPEG Fine",
                                               "RAW + JPEG Normal"]),
            "filmsimulation": radio("PROVIA/Standard", ["PROVIA/Standard", "Classic Chrome", "ETERNA/Cinema"]),
            "rawcompression": radio("Lossless compressed", ["Uncompressed", "Lossless compressed"]),
            "bulb": toggle(0),
        })
        self.dials_on_a = dials_on_a

    def set(self, name, value):
        super().set(name, value)
        if name == "prioritymode" and value == "USB" and self.dials_on_a:
            for w in FUJI_EXPOSURE:
                self.flat[w]["readonly"] = False


class FakePanasonic(VendorFake):
    SUMMARY = "Manufacturer: Panasonic\nModel: DC-S5M2\nVendor Extension ID: 0x1c (1.0)\n"

    def __init__(self) -> None:
        super().__init__({
            "manufacturer": text("Panasonic"), "cameramodel": text("DC-S5M2"),
            "expmode": radio("P", ["P", "A", "S", "M"]),
            "shutterspeed": radio("1/60", SHUTTERS),
            "f-number": radio("f/4", ["f/1.8", "f/2.8", "f/4", "f/5.6"]),
            "iso": radio("Auto", ["Auto", "100", "200", "400", "800", "1600", "3200", "6400"]),
            "exposurecompensation": radio("0", ["-1", "0", "1"]),
            "whitebalance": radio("Automatic", ["Automatic", "Daylight", "Cloudy", "Shade", "Incandescent",
                                                "Color Temperature"]),
            "colortemperature": rng(5500.0, 2500.0, 10000.0, 100.0),
            "afmode": radio("AF_S", ["AF_S", "AF_C", "AF_F", "MF"]),
            "imageformat": radio("1", ["1", "2", "3", "4"]),
            "capturetarget": radio("Internal RAM", ["Internal RAM", "Memory card"]),
            "viewfinder": toggle(0), "movie": toggle(0),
        })


class FakeOlympus(VendorFake):
    SUMMARY = "Manufacturer: OM Digital Solutions\nModel: OM-1\nVendor Extension ID: 0xfffd (1.0)\n"

    def __init__(self, manufacturer: str = "OM Digital Solutions") -> None:
        super().__init__({
            "manufacturer": text(manufacturer), "cameramodel": text("OM-1"),
            "d006": radio("2", ["1", "2", "3", "4"], ro=True),
            "shutterspeed": radio("1/60", SHUTTERS),
            "aperture": radio("4.0", ["1.8", "2.8", "4.0", "5.6"]),
            "iso": radio("Auto", ["Auto", "200", "400", "800", "1600", "3200", "6400"]),
            "exposurecompensation": radio("0.0", ["-1.0", "0.0", "1.0"]),
            "whitebalance": radio("Auto", ["Auto", "Sunny", "Shadow", "Cloudy", "Incandescent", "Color Temperature"]),
            "d01e": radio("1", ["1", "2", "3"]),                     # Olympus WB code, NOT a colour temperature
            "colortemperature": rng(5300.0, 2000.0, 14000.0, 100.0),
            "focusmode": radio("S-AF", ["S-AF", "C-AF", "MF", "S-AF+MF", "C-AF+TR"]),
            "d009": radio("1", ["1", "2", "3"]),
            "exposuremetermode": radio("Multi", ["Multi", "Center Weighted", "Spot"]),
            "imageformat": radio("Large Fine JPEG", ["RAW", "Large Fine JPEG", "Large Fine JPEG+RAW"]),
        })


class FakeGeneric(VendorFake):
    SUMMARY = "Manufacturer: Acme Imaging\nModel: Acme 1\nVendor Extension ID: 0x6 (1.0)\n"

    def __init__(self, movie: bool = False) -> None:
        flat = {
            "manufacturer": text("Acme Imaging"), "cameramodel": text("Acme 1"),
            "shutterspeed": radio("1/60", SHUTTERS), "f-number": radio("f/4", ["f/2.8", "f/4"]),
            "iso": radio("100", ["100", "200", "400", "800"]),
            "whitebalance": radio("Automatic", ["Automatic", "Daylight"]),
            "focusmode": radio("Automatic", ["Automatic", "Manual"]),
        }
        if movie:
            flat["movie"] = toggle(0)
        super().__init__(flat)


class SonyWithCard(FakeCamera):
    def __init__(self, shots: str = "812", movie_s: str = "5400") -> None:
        super().__init__()
        self.flat["manufacturer"] = text("Sony Corporation")
        self.flat["d249"] = text(shots)
        self.flat["d24a"] = text(movie_s)


FAKES = {"sony": FakeCamera, "canon": FakeCanon, "nikon": FakeNikon, "fuji": FakeFuji,
         "panasonic": FakePanasonic, "olympus": FakeOlympus, "generic": FakeGeneric}


# -- detection -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("manufacturer,expected", [
    ("Canon Inc.", "canon"), ("Nikon Corporation", "nikon"), ("FUJIFILM", "fuji"), ("Panasonic", "panasonic"),
    ("OLYMPUS", "olympus"), ("OLYMPUS IMAGING CORP.", "olympus"), ("OM Digital Solutions", "olympus"),
    ("OMSYSTEM", "olympus"), ("Sony Corporation", "sony"), ("Acme Imaging", "generic"),
])
def test_detect_by_manufacturer_widget(manufacturer, expected):
    assert vendors.detect({"manufacturer": text(manufacturer)}, "").name == expected


@pytest.mark.parametrize("ext,expected", [("0xb", "canon"), ("0xa", "nikon"), ("0xe", "fuji"), ("0x1c", "panasonic"),
                                          ("0x11", "sony"), ("0xfffd", "olympus"), ("0xfffe", "olympus")])
def test_detect_by_vendor_extension_id(ext, expected):
    name, evidence = vendors.identify({}, f"Model: X\nVendor Extension ID: {ext} (1.0)\n")
    assert name == expected and "vendor extension" in evidence


def test_detect_mtp_extension_falls_back_to_manufacturer_string():
    summary = "Manufacturer: Nikon Corporation\nModel: Z 8\nVendor Extension ID: 0x6 (1.0)\n"
    assert vendors.detect({}, summary).name == "nikon"


def test_manufacturer_widget_wins_over_extension_id():
    assert vendors.detect({"manufacturer": text("Canon Inc.")}, "Vendor Extension ID: 0xa").name == "canon"


def test_detect_existing_sony_fake_without_manufacturer():
    assert vendors.detect(widget_map(), "Model: ILCE-7M5").name == "sony"          # model pattern
    flat = widget_map()
    del flat["cameramodel"]
    assert vendors.detect(flat, "").name == "sony"                                 # d25a fingerprint


def test_detect_fingerprint_and_unknown():
    assert vendors.detect({"eosremoterelease": radio("None", ["None"])}, "").name == "canon"
    assert vendors.detect({"shutterspeed2": radio("1/60", ["1/60"])}, "").name == "nikon"
    assert vendors.detect({"shutterspeed": radio("1/60", ["1/60"])}, "Vendor Extension ID: 0x6").name == "generic"


@pytest.mark.parametrize("name", list(FAKES))
def test_for_camera_detects_every_fake(name):
    cam = FAKES[name]()
    assert vendors.for_camera(cam).name == name


def test_hex_widgets_are_vendor_specific():
    sony, nikon, olympus = (vendors.PROFILES[n] for n in ("sony", "nikon", "olympus"))
    assert sony.canonical_key("d242") == "movie_setting"
    assert nikon.canonical_key("d01e") == "color_temp"
    assert olympus.canonical_key("d01e") == "d01e"                 # Olympus WB code, not colour temperature
    assert olympus.canonical_key("d242") == "d242"                 # Sony ENUM codes never leak to other vendors
    assert olympus.enum_table("movie_setting", "d242") is None
    assert olympus.resolve("color_temp", FakeOlympus().flat) == "colortemperature"


# -- presets on every vendor ------------------------------------------------------------------------------

@pytest.mark.parametrize("vendor", list(FAKES))
@pytest.mark.parametrize("preset", list(presets.load_all()))
def test_every_builtin_preset_applies_cleanly_on_every_vendor(vendor, preset):
    cam = FAKES[vendor]()
    res = core.apply_settings(cam, presets.get(preset)["settings"], manual=presets.get(preset).get("manual"))
    d = res.as_dict()
    assert d["vendor"] == vendor
    assert not [s for s in d["problems"] if s["status"] == "error"], d["problems"]
    for u in d["unsupported"]:
        assert u["note"] == f"not available on {vendors.PROFILES[vendor].display} over USB"
        assert u["key"] not in {s["key"] for s in d["problems"]}


def test_photo_on_canon_translates_values():
    cam = FakeCanon()
    d = core.apply_settings(cam, presets.get("photo")["settings"]).as_dict()
    applied = {s["key"]: s["target"] for s in d["applied"]}
    assert applied == {"image_quality": "RAW + L", "shutter": "1/250", "aperture": "2.8", "focus_mode": "AI Servo",
                       "drive": "Continuous"}
    assert set(d["unchanged"]) == {"exposure_program", "iso", "white_balance", "metering"}  # Manual/Auto/Evaluative
    assert {u["key"] for u in d["unsupported"]} == {"steadyshot", "subject_recognition", "recognition_target",
                                                    "eye_select", "iso_auto_min", "iso_auto_max", "picture_profile",
                                                    "gamma_display_assist"}
    assert [(p["key"], p["status"]) for p in d["problems"]] == [("jpegquality", "missing")]  # Sony raw widget name
    assert d["blocked_by_mode_dial"] is False


def test_cinematic_on_canon_reports_sony_only_keys_as_unsupported():
    cam = FakeCanon()
    res = core.apply_settings(cam, presets.get("cinematic-24")["settings"])
    st = statuses(res)
    for key in ("movie_format", "movie_frame_rate", "movie_setting", "log_shooting", "log_gamut",
                "gamma_display_assist", "monitor_lut", "subject_recognition", "imagestabilization"):
        assert st[key] == "unsupported", key
    assert st["exposure_program"] == "unchanged"                  # "Movie (M)" → M → Manual (already on M)
    assert cam.flat["whitebalance"]["value"] == "Color Temperature"
    assert cam.flat["colortemperature"]["value"] == "5600"
    assert cam.flat["shutterspeed"]["value"] == "1/50"
    assert not any("Log Shooting" in m for m in res.manual)     # Sony menu path is not offered to Canon users
    assert res.as_dict()["problems"] == []


def test_canon_dial_on_av_is_a_camera_step_and_blocks_exposure():
    cam = FakeCanon(mode="AV")
    d = core.apply_settings(cam, {"exposure_program": "M", "shutter": "1/250", "aperture": 2.8}).as_dict()
    assert d["blocked_by_mode_dial"] is True
    assert d["manual_on_camera"][0].startswith("Exposure is locked")
    assert any(m.startswith("Mode dial → M") and "Canon" in m for m in d["manual_on_camera"])
    problems = by_key(d["problems"])
    assert "exposure_program" not in problems                     # camera step, not a failure
    assert problems["shutter"]["status"] == "readonly"
    assert by_key(d["applied"])["aperture"]["target"] == "2.8"


def test_photo_on_nikon_uses_auto_iso_toggle_and_nef():
    cam = FakeNikon()
    res = core.apply_settings(cam, presets.get("photo")["settings"])
    d = res.as_dict()
    assert ("autoiso", "On") in cam.writes
    assert not any(w == "iso" for w, _ in cam.writes)
    assert cam.flat["imagequality"]["value"] == "NEF+Fine"
    assert cam.flat["shutterspeed2"]["value"] == "1/250"
    assert cam.flat["f-number"]["value"] == "f/2.8"
    assert cam.flat["capturemode"]["value"] == "Continuous Low Speed"
    assert cam.flat["focusmode"]["value"] == "AF-C"
    assert "metering" in d["unchanged"]                           # "multi" == Nikon "Multi Spot" (matrix)
    assert statuses(res)["exposure_program"] == "camera-menu"     # expprogram follows the PASM dial
    assert any(m.startswith("Mode dial → M") for m in d["manual_on_camera"])


def test_cinematic_on_nikon_fixed_iso_and_kelvin():
    cam = FakeNikon()
    cam.flat["autoiso"]["value"] = "On"
    core.apply_settings(cam, presets.get("cinematic-24")["settings"])
    assert cam.writes.index(("autoiso", "Off")) < cam.writes.index(("iso", "800"))
    assert cam.flat["whitebalance"]["value"] == "Choose Color Temperature"
    assert cam.flat["d01e"]["value"] == 5600.0
    assert cam.flat["shutterspeed2"]["value"] == "1/50"


def test_photo_on_fuji_claims_usb_priority_first():
    cam = FakeFuji()
    res = core.apply_settings(cam, presets.get("photo")["settings"])
    d = res.as_dict()
    assert cam.writes[0] == ("prioritymode", "USB")
    assert cam.flat["shutterspeed"]["value"] == "1/250"
    assert cam.flat["aperture"]["value"] == "2.8"
    assert cam.flat["focusmode"]["value"] == "Continuous-Servo AF"
    assert cam.flat["imageformat"]["value"] == "RAW + JPEG Fine"
    assert cam.flat["capturemode"]["value"] == "Continuous low speed"
    assert statuses(res)["exposure_program"] == "camera-menu"
    assert d["blocked_by_mode_dial"] is False
    assert not [p for p in d["problems"] if p["key"] != "jpegquality"], d["problems"]


def test_fuji_dials_off_a_reports_lock_with_fix():
    cam = FakeFuji(dials_on_a=False)
    d = core.apply_settings(cam, {"shutter": "1/250"}).as_dict()
    p = by_key(d["problems"])["shutter"]
    assert p["status"] == "readonly" and "shutter-speed dial to A" in p["note"]
    assert d["blocked_by_mode_dial"] is True and "aperture ring" in d["manual_on_camera"][0]


def test_fuji_dry_run_never_writes_priority():
    cam = FakeFuji()
    core.apply_settings(cam, presets.get("photo")["settings"], dry_run=True)
    assert cam.writes == []


def test_panasonic_stills_mode_is_camera_step_movie_mode_is_set():
    cam = FakePanasonic()
    res = core.apply_settings(cam, presets.get("photo")["settings"])
    st = statuses(res)
    assert st["exposure_program"] == "camera-menu" and cam.flat["expmode"]["value"] == "P"
    assert cam.flat["afmode"]["value"] == "AF_C"
    assert st["image_quality"] == "no-match"                      # raw integer formats: reported, not guessed
    cam = FakePanasonic()
    core.apply_settings(cam, presets.get("cinematic-24")["settings"])
    assert cam.flat["expmode"]["value"] == "M"                    # "Movie (M)" → Creative Video M


def test_olympus_values_and_dial():
    cam = FakeOlympus()
    res = core.apply_settings(cam, presets.get("photo")["settings"])
    assert cam.flat["focusmode"]["value"] == "C-AF"
    assert cam.flat["imageformat"]["value"] == "Large Fine JPEG+RAW"
    assert statuses(res)["exposure_program"] == "camera-menu"
    assert cam.flat["d01e"]["value"] == "1"                       # never touched as a colour temperature


def test_generic_camera_uses_standard_names():
    cam = FakeGeneric()
    res = core.apply_settings(cam, {"shutter": "1/250", "aperture": 2.8, "subject_recognition": "On"})
    st = statuses(res)
    assert st["shutter"] == st["aperture"] == "ok"
    assert st["subject_recognition"] == "unsupported"
    assert res.as_dict()["unsupported"] == [{"key": "subject_recognition",
                                            "note": "not available on this PTP camera over USB"}]


def test_explicit_vendor_argument_overrides_detection(cam):
    res = core.apply_settings(cam, {"focus_mode": "AF-C"}, dry_run=True, vendor=vendors.PROFILES["sony"])
    assert res.vendor == "sony"


# -- current_settings / diff / dump on other vendors -------------------------------------------------------

def test_current_settings_canon_uses_vendor_map():
    cur = core.current_settings(FakeCanon())
    assert cur["exposure_program"] == {"value": "Manual", "writable": False, "widget": "autoexposuremode"}
    assert cur["exposure_program_dial"]["widget"] == "autoexposuremodedial"
    assert cur["focus_mode"]["value"] == "One Shot"
    assert cur["picture_style"]["widget"] == "picturestyle"
    assert cur["capture_target"]["value"] == "Internal RAM"
    assert "movie_format" not in cur


def test_current_settings_sony_picture_style_does_not_duplicate_creative_look(cam):
    cam.flat["d0fa"] = radio("1", ["1", "2", "3"])
    cur = core.current_settings(cam)
    assert cur["creative_look"] == {"value": "ST", "writable": True, "widget": "d0fa"}
    assert "picture_style" not in cur


def test_sony_picture_style_portable_key_uses_creative_look_labels(cam):
    cam.flat["d0fa"] = radio("1", ["1", "2", "3"])
    d = core.apply_settings(cam, {"picture_style": "NT"}).as_dict()
    step = by_key(d["applied"])["picture_style"]
    assert (step["current"], step["target"]) == ("ST", "NT") and cam.flat["d0fa"]["value"] == "3"


def test_diff_on_canon_matches_translated_values():
    cam = FakeCanon()
    cam.flat["focusmode"]["value"] = "AI Servo"
    assert core.diff(cam, {"focus_mode": "AF-C", "exposure_program": "Movie (M)", "aperture": 4}) == []
    assert core.diff(cam, {"focus_mode": "AF-S"})[0]["camera"] == "AI Servo"


def test_dump_records_vendor_and_restores_vendor_widgets(tmp_path):
    cam = FakeNikon()
    cam.flat["expprogram"]["readonly"] = False                    # dial-less / U-mode: writable
    path = core.dump(cam, tmp_path / "nikon.yaml")
    doc = yaml.safe_load(path.read_text())
    assert doc["vendor"] == "nikon"
    settings, _ = core.read_settings_file(path)
    assert settings["expprogram"] == "A"                          # Nikon's real mode widget is restorable
    assert "viewfinder" not in settings and "movie" not in settings
    canon_path = core.dump(FakeCanon(), tmp_path / "canon.yaml")
    canon_settings, _ = core.read_settings_file(canon_path)
    assert not {"viewfinder", "movierecordtarget", "eosremoterelease", "bulb"} & set(canon_settings)


def test_sony_dump_still_skips_expprogram(tmp_path, cam):
    cam.flat["expprogram"] = radio("M", ["P", "A", "S", "M"])
    settings, _ = core.read_settings_file(core.dump(cam, tmp_path / "sony.yaml"))
    assert "expprogram" not in settings


# -- capture / movie hooks ---------------------------------------------------------------------------------

def test_canon_movie_sequence():
    cam = FakeCanon()
    prof = vendors.PROFILES["canon"]
    prof.movie(cam, True)
    assert cam.writes == [("viewfinder", 1), ("movierecordtarget", "Card")]
    cam.writes.clear()
    prof.movie(cam, False)
    assert cam.writes == [("movierecordtarget", "None")]


def test_canon_movie_switches_eos_movie_mode_when_available():
    cam = FakeCanon(movie_mode_widget=True)
    vendors.PROFILES["canon"].movie(cam, True)
    assert cam.writes == [("eosmoviemode", 1), ("viewfinder", 1), ("movierecordtarget", "Card")]


def test_canon_without_movie_target_raises_clear_error():
    cam = FakeCanon()
    del cam.flat["movierecordtarget"]
    with pytest.raises(CameraError, match="Stills/Movie switch"):
        vendors.PROFILES["canon"].movie(cam, True)
    assert cam.writes == []


def test_canon_prepare_capture_sets_memory_card_once():
    cam = FakeCanon()
    prof = vendors.PROFILES["canon"]
    prof.prepare_capture(cam)
    prof.prepare_capture(cam)
    assert cam.writes == [("capturetarget", "Memory card")]


def test_nikon_movie_refusal_reads_prohibit_reason():
    cam = FakeNikon(prohibit="Card not inserted")
    with pytest.raises(CameraError) as e:
        vendors.PROFILES["nikon"].movie(cam, True)
    assert "Card not inserted" in str(e.value) and "photo/movie selector" in str(e.value)
    ok = FakeNikon()
    vendors.PROFILES["nikon"].movie(ok, True)
    assert ok.writes == [("movie", 1)]


@pytest.mark.parametrize("vendor,match", [("fuji", "Fujifilm movie recording"), ("olympus", "OM System/Olympus movie")])
def test_movie_unsupported_vendors_raise_one_clear_sentence(vendor, match):
    cam = FAKES[vendor]()
    with pytest.raises(CameraError, match=match) as e:
        vendors.PROFILES[vendor].movie(cam, True)
    assert "record button on the camera" in str(e.value)
    assert cam.writes == []


def test_panasonic_and_generic_movie():
    cam = FakePanasonic()
    vendors.PROFILES["panasonic"].movie(cam, True)
    vendors.PROFILES["panasonic"].movie(cam, False)
    assert cam.writes == [("movie", 1), ("movie", 0)]
    with pytest.raises(CameraError, match="no movie-record control"):
        vendors.GENERIC.movie(FakeGeneric(), True)
    gen = FakeGeneric(movie=True)
    vendors.GENERIC.movie(gen, True)
    assert gen.writes == [("movie", 1)]


def _ptp_with(fake, vendor: str, monkeypatch, log=None) -> PtpCamera:
    """A PtpCamera whose widget reads/writes go to `fake` (no USB, never opened)."""
    cam = PtpCamera()
    cam._vendor = vendors.PROFILES[vendor]
    monkeypatch.setattr(cam, "widget", fake.widget)

    def set_(name, value):
        if log is not None:
            log.append(("set", name, value))
        fake.set(name, value)
    monkeypatch.setattr(cam, "set", set_)
    return cam


def _fake_gp(log):
    def capture(kind):
        log.append(("capture",))
        return types.SimpleNamespace(folder="/", name="IMG_0001.JPG")

    class File:
        def save(self, path):
            open(path, "w").write("jpeg")
    return types.SimpleNamespace(capture=capture, wait_for_event=lambda t: (gp.GP_EVENT_TIMEOUT, None),
                                 file_get=lambda folder, name, kind: File())


def test_ptp_capture_sets_canon_capture_target_before_capture(tmp_path, monkeypatch):
    log: list = []
    fake = FakeCanon()
    cam = _ptp_with(fake, "canon", monkeypatch, log)
    cam.cam = _fake_gp(log)
    out = cam.capture(tmp_path, wait_extra=0)
    assert log == [("set", "capturetarget", "Memory card"), ("capture",)]
    assert [p.name for p in out] == ["IMG_0001.JPG"]


def test_ptp_capture_on_sony_never_touches_capture_target(tmp_path, monkeypatch):
    log: list = []
    fake = FakeCamera()
    fake.flat["capturetarget"] = radio("Internal RAM", ["Internal RAM", "Memory card"])
    cam = _ptp_with(fake, "sony", monkeypatch, log)
    cam.cam = _fake_gp(log)
    cam.capture(tmp_path, wait_extra=0)
    assert log == [("capture",)]


def test_ptp_movie_routes_through_vendor(monkeypatch):
    canon = FakeCanon()
    _ptp_with(canon, "canon", monkeypatch).movie(True)
    assert canon.writes == [("viewfinder", 1), ("movierecordtarget", "Card")]
    sony = FakeCamera()
    _ptp_with(sony, "sony", monkeypatch).movie(True)
    assert sony.writes == [("movie", 1)]
    with pytest.raises(CameraError, match="record button"):
        _ptp_with(FakeFuji(), "fuji", monkeypatch).movie(True)


def test_ptp_half_press_per_vendor(monkeypatch):
    sony = FakeCamera()
    sony.flat["autofocus"] = toggle(0)
    _ptp_with(sony, "sony", monkeypatch).half_press(True)
    assert sony.writes == [("autofocus", 1)]
    canon = FakeCanon()
    canon.flat["autofocusdrive"] = toggle(0)
    canon.flat["cancelautofocus"] = toggle(0)
    cam = _ptp_with(canon, "canon", monkeypatch)
    cam.half_press(True)
    cam.half_press(False)
    assert canon.writes == [("autofocusdrive", 1), ("cancelautofocus", 1)]


class _W:
    def __init__(self, value):
        self.value = value

    def get_value(self):
        return self.value


def test_ptp_detects_vendor_from_identity_widgets():
    values = {"manufacturer": "Nikon Corporation", "cameramodel": "Z 6_2"}

    def single(name):
        if name not in values:
            raise gp.GPhoto2Error(gp.GP_ERROR_BAD_PARAMETERS)
        return _W(values[name])
    cam = PtpCamera()
    cam.cam = types.SimpleNamespace(get_single_config=single, get_summary=lambda: "Model: Z 6_2")
    assert cam.vendor.name == "nikon"
    assert cam._vendor is vendors.PROFILES["nikon"]               # cached for the session


def test_ptp_vendor_undetermined_is_generic_but_not_cached():
    cam = PtpCamera()
    assert cam.vendor is vendors.GENERIC                          # not open: generic, no crash
    cam.cam = types.SimpleNamespace()                             # driver without identity support
    assert cam.vendor is vendors.GENERIC and cam._vendor is None


def test_ptp_config_reopen_workaround_is_sony_only(monkeypatch):
    class Root:
        def get_type(self):
            return gp.GP_WIDGET_WINDOW

        def count_children(self):
            return 0
    cam = PtpCamera()
    cam.cam = types.SimpleNamespace(get_config=lambda: Root())
    cam._vendor = vendors.PROFILES["canon"]
    cam._dirty = True
    monkeypatch.setattr(cam, "open", lambda: pytest.fail("Canon must not be reopened after a write"))
    assert cam.config() == {}


def test_ptp_live_view_error_includes_nikon_prohibit(monkeypatch):
    fake = FakeNikon(prohibit="Mode dial not in P/A/S/M")
    cam = _ptp_with(fake, "nikon", monkeypatch)

    def refuse():
        raise gp.GPhoto2Error(gp.GP_ERROR)
    cam.cam = types.SimpleNamespace(capture_preview=refuse)
    with pytest.raises(CameraError, match="Nikon says: Mode dial not in P/A/S/M"):
        cam.preview_jpeg()


# -- capabilities --------------------------------------------------------------------------------------

def test_capabilities_sony():
    caps = vendors.capabilities(SonyWithCard())
    assert (caps["vendor"], caps["vendor_name"], caps["tested_on_hardware"]) == ("sony", "Sony", True)
    assert caps["model"] == "ILCE-7M5" and "PC Remote" in caps["setup"]
    assert caps["settings"]["exposure_program"]["value"] == "Intelligent Auto"
    assert "Movie (M)" in caps["settings"]["exposure_program"]["choices"]
    assert caps["settings"]["movie_setting"]["value"] == "50M 4:2:0 8bit"
    assert caps["settings"]["log_shooting"]["available"] is False and "Log Shooting" in \
        caps["settings"]["log_shooting"]["camera_menu"]
    locked = {x["key"]: x["reason"] for x in caps["locked"]}
    assert "PC Remote" in locked["exposure_program"] and "mode dial to M" in locked["shutter"]
    assert caps["card"] == {"slot": 1, "remaining_shots": 812, "remaining_movie_seconds": 5400, "warning": None}
    assert caps["warnings"] == []
    assert caps["controls"]["movie"] == {"available": True, "widget": "movie"}
    assert "iso_auto" in caps["unsupported"]


@pytest.mark.parametrize("shots,movie_s,program,needle", [
    ("20", "3000", "1", "nearly full"), ("900", "0", "491603", "nearly full"), ("0", "0", "1", "full or missing")])
def test_capabilities_sony_card_warnings(shots, movie_s, program, needle):
    cam = SonyWithCard(shots, movie_s)
    cam.flat["500e"]["value"] = program
    caps = vendors.capabilities(cam)
    assert needle in caps["card"]["warning"] and caps["warnings"] == [caps["card"]["warning"]]


def test_capabilities_sony_movie_time_zero_in_stills_mode_is_not_a_warning():
    # real A7 V dump: 656 shots free, d24a = 0 while in Intelligent Auto
    caps = vendors.capabilities(SonyWithCard("656", "0"))
    assert caps["card"]["remaining_movie_seconds"] == 0 and caps["card"]["warning"] is None


def test_capabilities_sony_alias_listed_once():
    cam = SonyWithCard()
    cam.flat["d0fa"] = radio("1", ["1", "2"], ro=True)
    caps = vendors.capabilities(cam)
    assert caps["settings"]["creative_look"]["value"] == "ST" and "picture_style" not in caps["settings"]
    assert [x["key"] for x in caps["locked"]].count("creative_look") == 1


def test_capabilities_canon():
    caps = vendors.capabilities(FakeCanon(mode="AV"))
    assert caps["vendor"] == "canon" and caps["detected_by"] == "manufacturer 'Canon Inc.'"
    assert caps["tested_on_hardware"] is False and "Wi-Fi" in caps["setup"]
    assert "AI Servo" in caps["settings"]["focus_mode"]["choices"]
    assert caps["controls"]["movie"] == {"available": True, "widget": "movierecordtarget"}
    assert caps["controls"]["shutter_button"]["widget"] == "eosremoterelease"
    assert caps["controls"]["movie_mode_switch"]["available"] is False
    assert any("Internal RAM" in w for w in caps["warnings"])
    locked = {x["key"]: x["reason"] for x in caps["locked"]}
    assert "mode dial" in locked["exposure_program"] and "shutter" in locked
    assert {"movie_format", "log_shooting", "subject_recognition"} <= set(caps["unsupported"])
    assert caps["card"] is None


def test_capabilities_fuji_nikon_and_generic():
    fuji = vendors.capabilities(FakeFuji())
    assert fuji["controls"]["movie"]["available"] is False and "record button" in fuji["controls"]["movie"]["note"]
    assert any("prioritymode" in w for w in fuji["warnings"])
    assert fuji["settings"]["picture_style"]["value"] == "PROVIA/Standard"
    assert "set the shutter-speed dial to A" in {x["key"]: x["reason"] for x in fuji["locked"]}["shutter"]

    nikon = vendors.capabilities(FakeNikon(prohibit="Mode dial not in P/A/S/M"))
    assert nikon["prohibit"] == {"movie": "Mode dial not in P/A/S/M", "live_view": "Mode dial not in P/A/S/M"}
    assert any("live view currently refused" in w for w in nikon["warnings"])
    assert nikon["settings"]["iso_auto"]["widget"] == "autoiso"
    assert "prohibit" not in vendors.capabilities(FakeCanon()) and vendors.capabilities(FakeNikon())["warnings"] == []

    gen = vendors.capabilities(FakeGeneric())
    assert gen["vendor"] == "generic" and gen["vendor_name"] == "Generic PTP"
    assert gen["settings"]["shutter"]["widget"] == "shutterspeed"


def test_capabilities_never_writes():
    for fake in (SonyWithCard(), FakeCanon(), FakeFuji()):
        vendors.capabilities(fake)
        assert fake.writes == []


# Real A7 V: colortemperature is read-only while WB is Auto, d0da (movie SteadyShot) is read-only in photo modes.
def test_capabilities_color_temp_lock_names_white_balance():
    cam = SonyWithCard()
    cam.flat["colortemperature"]["readonly"] = True                   # WB Automatic
    locked = {x["key"]: x["reason"] for x in vendors.capabilities(cam)["locked"]}
    assert locked["color_temp"] == vendors.COLOR_TEMP_LOCK == "set white_balance to Color Temp first"
    cam.flat["whitebalance"]["value"] = "Choose Color Temperature"    # right WB, still locked → not the WB reason
    locked = {x["key"]: x["reason"] for x in vendors.capabilities(cam)["locked"]}
    assert locked["color_temp"] == vendors.DEFAULT_LOCK


def test_color_temp_lock_is_vendor_agnostic():
    canon = FakeCanon()
    canon.flat["colortemperature"]["readonly"] = True                 # WB Auto
    caps = vendors.capabilities(canon)
    assert caps["settings"]["color_temp"]["locked_reason"] == vendors.COLOR_TEMP_LOCK
    canon.flat["whitebalance"]["value"] = "Color Temperature"
    assert vendors.capabilities(canon)["settings"]["color_temp"]["locked_reason"] == vendors.DEFAULT_LOCK


@pytest.mark.parametrize("program,reason", [("1", vendors.MOVIE_ONLY_LOCK), ("65538", vendors.MOVIE_ONLY_LOCK),
                                            ("491603", vendors.DEFAULT_LOCK)])
def test_capabilities_steadyshot_movie_lock_in_photo_mode(program, reason):
    cam = SonyWithCard()
    cam.flat["500e"]["value"] = program
    cam.flat["d0da"] = radio("0", [], ro=True)
    assert vendors.capabilities(cam)["settings"]["steadyshot_movie"]["locked_reason"] == reason


def test_apply_readonly_color_temp_note_names_white_balance(cam):
    cam.flat["colortemperature"]["readonly"] = True
    d = core.apply_settings(cam, {"color_temp": 5600}, dry_run=True).as_dict()
    assert d["problems"][0]["status"] == "readonly" and vendors.COLOR_TEMP_LOCK in d["problems"][0]["note"]


def test_raw_compression_a7v_codes_are_labelled():
    # d288 on the A7 V offers 6, 7, 1 = Compressed (HQ), Lossless Compressed, Compressed
    cam = SonyWithCard()
    cam.flat["d288"] = radio("7", ["6", "7", "1"], ro=True)
    entry = vendors.capabilities(cam)["settings"]["raw_compression"]
    assert entry["value"] == "Lossless Compressed"
    assert entry["choices"] == ["Compressed (HQ)", "Lossless Compressed", "Compressed"]


@pytest.mark.parametrize("wanted,code", [("lossless", "7"), ("Lossless Compressed", "7"), ("compressed", "1"),
                                         ("compressed hq", "6"), ("Compressed (HQ)", "6")])
def test_raw_compression_labels_resolve_to_a7v_codes(wanted, code):
    from camctl.settings import enum_code
    assert enum_code("raw_compression", wanted, ["6", "7", "1"]).value == code


def test_setup_hints_cover_every_vendor():
    hints = vendors.setup_hints()
    assert set(hints) == {"Sony", "Canon", "Nikon", "Fujifilm", "Panasonic", "OM System/Olympus"}
    assert "USB Tether Shooting Auto".upper() in hints["Fujifilm"].upper()
    assert "PC(Tether)" in hints["Panasonic"] and "RAW/Control" in hints["OM System/Olympus"]


# -- Sony regression ------------------------------------------------------------------------------------------

def test_sony_photo_preset_unchanged_behaviour(cam):
    d = core.apply_settings(cam, presets.get("photo")["settings"]).as_dict()
    assert d["vendor"] == "sony"
    assert cam.writes[:2] == [("d25a", "1"), ("500e", "1")]
    assert d["unsupported"] == []
    assert by_key(d["applied"])["exposure_program"]["target"] == "M"
    assert cam.flat["focusmode"]["value"] == "AF-C"


def test_sony_cinematic_menu_only_keys_still_camera_menu(cam):
    res = core.apply_settings(cam, presets.get("cinematic-24")["settings"], dry_run=True)
    st = statuses(res)
    assert st["log_shooting"] == st["log_gamut"] == "camera-menu"
    assert any("Log Shooting: On (Flexible ISO)" in m for m in res.manual)


def test_capabilities_sony_reports_slot2_when_camera_has_it():
    cam = SonyWithCard()
    cam.flat["d257"], cam.flat["d258"] = text("0"), text("0")
    card = vendors.capabilities(cam)["card"]
    assert card["slot2"] == {"remaining_shots": 0, "remaining_movie_seconds": 0} and card["warning"] is None
