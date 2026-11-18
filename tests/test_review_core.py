"""Regression tests for bugs found in review of core / settings / ptp / sonyptp (fakes only, no USB)."""

from __future__ import annotations

import struct
import types

import gphoto2 as gp
import pytest
import yaml

from camctl import core, ptp, sonyptp
from camctl.ptp import CameraError, PtpCamera
from camctl.settings import enum_code, match_choice, sort_for_apply
from camctl.sonyptp import PtpError, SonyPtp, pack_value, parse_props

from .conftest import SHUTTERS, FakeCamera


def steps(res):
    return {s.key: s for s in res.steps}


class WbCamera(FakeCamera):
    """Colour temperature is read-only until white balance is on 'Choose Color Temperature' (like the A7 V)."""

    def __init__(self) -> None:
        super().__init__()
        self.flat["colortemperature"]["readonly"] = True

    def set(self, name, value):
        super().set(name, value)
        if name == "whitebalance":
            self.flat["colortemperature"]["readonly"] = str(value) != "Choose Color Temperature"


# -- dump → load ordering / raw widget names ------------------------------------------

def test_sort_for_apply_orders_raw_widget_names_like_their_canonical_key():
    # dump files list widgets in libgphoto2 order: named widgets first, Sony dXXX/500e last
    order = sort_for_apply(["colortemperature", "whitebalance", "shutterspeed", "d242", "d241", "500e", "d25a"])
    assert order.index("d25a") < order.index("500e") < order.index("shutterspeed")
    assert order.index("d241") < order.index("d242")
    assert order.index("whitebalance") < order.index("colortemperature")


def test_load_dump_sets_kelvin_after_white_balance():
    cam = WbCamera()
    res = core.apply_settings(cam, {"colortemperature": 3200.0, "whitebalance": "Choose Color Temperature"})
    st = steps(res)
    assert st["whitebalance"].status == "ok"
    assert st["colortemperature"].status == "ok", st["colortemperature"].note
    assert cam.flat["colortemperature"]["value"] == 3200.0


def test_load_dump_raw_mode_widgets_unlock_and_settle():
    cam = FakeCamera()
    res = core.apply_settings(cam, {"shutterspeed": "1/50", "d25a": "1", "500e": "1"})   # dump file order
    d = res.as_dict()
    assert d["problems"] == [], d["problems"]
    assert cam.flat["shutterspeed"]["value"] == "1/50"


def test_raw_500e_in_preset_uses_priority_unlock():
    cam = FakeCamera()
    res = core.apply_settings(cam, {"500e": "1"})
    assert steps(res)["500e"].status == "ok"
    assert cam.writes == [("d25a", "1"), ("500e", "1")]


# -- dump → load exclusions -------------------------------------------------------------

def _dump_file(tmp_path, props):
    p = tmp_path / "dump.yaml"
    p.write_text(yaml.safe_dump({"camctl_dump": 1, "properties": props}))
    return p


@pytest.mark.parametrize("widget,info", [
    ("d2c1", {"label": "ShutterHalfRelease", "type": "menu", "value": "0", "choices": []}),
    ("d2c2", {"label": "ShutterRelease", "type": "menu", "value": "0", "choices": []}),
    ("d2c7", {"label": "RequestOneShooting", "type": "menu", "value": "0", "choices": []}),
    ("d2c8", {"label": "MovieRecButtonHold", "type": "menu", "value": "0", "choices": []}),
    ("d2d1", {"label": "Manual Focus Adjust", "type": "range", "value": 0.0, "range": [0.0, 0.0, 0.0]}),
    ("d2dd", {"label": "Zoom Operation", "type": "range", "value": 0.0, "range": [0.0, 0.0, 0.0]}),
    ("d2e9", {"label": "Save Zoom and Focus Position", "type": "menu", "value": "0", "choices": []}),
    ("d2ea", {"label": "Load Zoom and Focus Position", "type": "menu", "value": "0", "choices": []}),
    ("d223", {"label": "Date/Time Set", "type": "text", "value": ""}),
    ("focusmagnifier", {"label": "Focus Magnifier", "type": "radio", "value": "Off,320,240",
                        "choices": ["Off", "1", "5.5", "11"]}),
    ("expprogram", {"label": "Exposure Program", "type": "radio", "value": "M", "choices": ["P", "A", "S", "M"]}),
])
def test_dump_load_skips_action_and_volatile_widgets(tmp_path, widget, info):
    p = _dump_file(tmp_path, {widget: {"readonly": False, **info},
                              "whitebalance": {"readonly": False, "type": "radio", "value": "Daylight"}})
    settings, _ = core.read_settings_file(p)
    assert widget not in settings
    assert settings == {"whitebalance": "Daylight"}


def test_dump_load_skips_unreadable_values(tmp_path):
    p = _dump_file(tmp_path, {"d04c": {"readonly": False, "type": "text", "value": None},
                              "whitebalance": {"readonly": False, "type": "radio", "value": "Daylight"}})
    assert core.read_settings_file(p) == ({"whitebalance": "Daylight"}, [])


# -- per-key robustness -------------------------------------------------------------------

def test_range_with_non_numeric_value_is_a_problem_not_a_crash(cam):
    res = core.apply_settings(cam, {"color_temp": "auto", "focus_mode": "AF-C"})
    st = steps(res)
    assert st["color_temp"].status == "no-match"
    assert st["focus_mode"].status == "ok"
    assert core.diff(cam, {"color_temp": "warm"})[0]["key"] == "color_temp"


def test_widget_read_error_is_reported_per_key():
    class Flaky(FakeCamera):
        def widget(self, name):
            if name == "focusmode":
                raise CameraError("focusmode: I/O problem")
            return super().widget(name)

    cam = Flaky()
    res = core.apply_settings(cam, {"focus_mode": "AF-C", "white_balance": "daylight"})
    st = steps(res)
    assert st["focus_mode"].status == "error" and "I/O" in st["focus_mode"].note
    assert st["white_balance"].status == "ok"


# -- value matching -------------------------------------------------------------------------

def test_enum_partial_bitrate_does_not_match_inside_a_bigger_number():
    # 43 = 250M 4:2:2 10bit, 50 = 150M 4:2:0 10bit, 61 = 50M 4:2:2 10bit, 63 = 50M 4:2:0 8bit
    m = enum_code("movie_setting", "50M", ["43", "50", "61", "63"])
    assert m.value in ("61", "63"), m


def test_enum_partial_frame_rate_still_works():
    assert enum_code("movie_frame_rate", "24", ["3", "5", "7", "1"]).value == "7"


def test_raw_enum_codes_are_never_snapped_to_a_neighbour():
    m = match_choice("d17b", "3", ["2", "5", "255"])      # FocusMagnificationTime codes
    assert m.value is None and m.alternatives == ["2", "5", "255"]


def test_exposure_values_still_snap_with_note():
    m = match_choice("shutter", "1/48", SHUTTERS)
    assert (m.value, m.exact) == ("1/50", False) and "1/50" in m.note
    m = match_choice("shutterspeed", "1/48", SHUTTERS)      # raw widget name of a canonical key
    assert m.value == "1/50"
    m = match_choice("iso_auto_max", "7000", ["100", "3200", "6400", "12800"])
    assert m.value == "6400"                                # log scale: 6400 is nearer than 3200/12800


@pytest.mark.parametrize("wanted,expected", [(True, "On"), (False, "Off")])
def test_yaml_booleans_match_on_off(wanted, expected):
    # user preset `steadyshot: on` loads as True under YAML 1.1
    assert match_choice("steadyshot", wanted, ["On", "Off"]).value == expected


def test_shutter_and_aperture_spellings():
    for w in ("1/50", "1/50s", "0.02", "1/50 sec"):
        assert match_choice("shutter", w, SHUTTERS).value == "1/50", w
    assert match_choice("shutter", '1"', SHUTTERS).value == "1"
    for w in ("f/2.8", "F2.8", 2.8, "2.8"):
        assert match_choice("aperture", w, ["f/2", "f/2.8", "f/4"]).value == "f/2.8", w


def test_iso_auto_spellings_and_non_finite():
    choices = ["Auto ISO", "100", "200", "400"]
    for w in ("auto", "Auto ISO", "ISO auto", "AUTO"):
        assert match_choice("iso", w, choices).value == "Auto ISO", w
    assert match_choice("iso", "nan", choices).value is None
    assert match_choice("shutter", "inf", SHUTTERS).value is None


# -- ptp.PtpCamera (fake libgphoto2 camera object) ------------------------------------------

class FakeRoot:
    def get_type(self):
        return gp.GP_WIDGET_WINDOW

    def count_children(self):
        return 0


class FakeGp:
    def __init__(self, exit_error: bool = False) -> None:
        self.exit_error = exit_error
        self.exited = 0

    def get_config(self):
        return FakeRoot()

    def exit(self):
        self.exited += 1
        if self.exit_error:
            raise gp.GPhoto2Error(gp.GP_ERROR_IO)

    def wait_for_event(self, timeout):
        return gp.GP_EVENT_TIMEOUT, None


def test_config_reopen_survives_failed_sticky_reassert(monkeypatch):
    cam = PtpCamera()
    monkeypatch.setattr(cam, "open", lambda: setattr(cam, "cam", FakeGp()) or cam)
    cam.cam = FakeGp()
    written = []

    def fake_set(name, value):
        written.append(name)
        cam._dirty = True
        if name == "500e" and "d25a" not in written:
            raise CameraError("500e='1': Bad parameters")

    monkeypatch.setattr(cam, "set", fake_set)
    cam._sticky = {"500e": "1", "d25a": "1"}      # 500e first: d25a came from another path
    cam._dirty = True
    assert cam.config() == {}
    assert written == ["d25a", "500e"]           # priority re-asserted before the mode
    assert cam._dirty is False

    cam._sticky = {"500e": "491603"}
    written.clear()
    cam._dirty = True
    monkeypatch.setattr(cam, "set", lambda n, v: (_ for _ in ()).throw(CameraError("dial moved")))
    assert cam.config() == {}                    # a refused re-assert must not break every later read


def test_close_error_does_not_mask_original_exception():
    cam = PtpCamera()
    cam.cam = FakeGp(exit_error=True)
    with pytest.raises(ValueError):
        with cam:
            raise ValueError("user code failed")
    assert cam.cam is None


def test_widget_unreadable_value_is_none_like_config():
    class W:
        def get_type(self):
            return gp.GP_WIDGET_TEXT

        def get_label(self):
            return "PTP Property 0xd04c"

        def get_readonly(self):
            return 0

        def get_value(self):
            raise gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED)

    cam = PtpCamera()
    cam.cam = types.SimpleNamespace(get_single_config=lambda name: W())
    assert cam.widget("d04c")["value"] is None


def test_capture_does_not_overwrite_previous_download(tmp_path, monkeypatch):
    saved_data = {}

    class File:
        def __init__(self, name):
            self.name = name

        def save(self, path):
            saved_data[path] = self.name
            open(path, "w").write("new " + self.name)

    clock = iter(range(1000))
    monkeypatch.setattr(ptp.time, "time", lambda: next(clock))
    fake = types.SimpleNamespace(
        capture=lambda kind: types.SimpleNamespace(folder="/", name="capt0000.jpg"),
        wait_for_event=lambda t: (gp.GP_EVENT_TIMEOUT, None),
        file_get=lambda folder, name, kind: File(name))
    (tmp_path / "capt0000.jpg").write_text("previous shot")
    cam = PtpCamera()
    cam.cam = fake
    out = cam.capture(tmp_path, wait_extra=2)
    assert (tmp_path / "capt0000.jpg").read_text() == "previous shot"
    assert len(out) == 1 and out[0] != tmp_path / "capt0000.jpg" and out[0].read_text() == "new capt0000.jpg"
    assert out[0].suffix == ".jpg"


# -- sonyptp ---------------------------------------------------------------------------------

U16 = 0x0004


def _dpd_enum(code, values, settable):
    rec = struct.pack("<HHBB", code, U16, 1, 1) + pack_value(values[0], U16) * 2 + b"\x02"
    rec += struct.pack("<H", len(values)) + b"".join(pack_value(v, U16) for v in values)
    return rec + struct.pack("<H", len(settable)) + b"".join(pack_value(v, U16) for v in settable)


def test_parse_props_truncated_blob_keeps_complete_records():
    good = _dpd_enum(0xD242, [54, 61, 63], [54, 63])
    bad = _dpd_enum(0xD241, [8, 9, 11, 14], [8, 9])
    for cut in range(1, len(bad)):
        props = parse_props(struct.pack("<II", 2, 0) + good + bad[:cut])
        assert props[0xD242].values == [54, 61, 63]
        assert 0xD241 not in props or props[0xD241].values in ([], [8, 9, 11, 14])   # [] = cut before form byte


def test_pack_value_signed():
    assert pack_value(-2, 0x0001) == b"\xfe"
    assert pack_value("-300", 0x0003) == struct.pack("<h", -300)
    assert pack_value(-5, 0x0005) == struct.pack("<i", -5)


def _container(ctype, code, tid, payload=b""):
    return struct.pack("<IHHI", 12 + len(payload), ctype, code, tid) + payload


class FakeDev:
    def __init__(self, reads):
        self.reads = list(reads)
        self.written = []

    def write(self, ep, data, timeout):
        self.written.append(bytes(data))

    def read(self, ep, size, timeout):
        if not self.reads:
            raise TimeoutError("usb read timeout")
        chunk = self.reads.pop(0)
        out, rest = chunk[:size], chunk[size:]
        if rest:
            self.reads.insert(0, rest)
        return out


def _sony(reads):
    s = SonyPtp()
    s.dev = FakeDev(reads)
    return s


def test_transaction_response_without_data_phase():
    s = _sony([_container(3, 0x2001, 0, struct.pack("<I", 7))])
    assert s.transaction(0x1002, [1]) == (0x2001, [7], b"")


def test_transaction_data_and_response_in_one_transfer():
    # data container length is a multiple of 512 and the device sent no ZLP: both arrive in one read
    data = _container(2, 0x9209, 0, b"\x00" * (512 - 12))
    s = _sony([data + _container(3, 0x2001, 0)])
    rc, _, payload = s.transaction(0x9209, [])
    assert rc == 0x2001 and len(payload) == 500


def test_read_container_skips_zero_length_packet():
    s = _sony([b"", _container(3, 0x2001, 0)])
    assert s.transaction(0x1002, [1])[0] == 0x2001


class FakeUsbDevice(FakeDev):
    def __init__(self, reads):
        super().__init__(reads)
        ep_out = types.SimpleNamespace(bEndpointAddress=0x01, bmAttributes=0x02)
        ep_in = types.SimpleNamespace(bEndpointAddress=0x81, bmAttributes=0x02)
        self._eps = [ep_out, ep_in]

    def get_active_configuration(self):
        eps = self._eps

        class Intf:
            bInterfaceClass = 6
            bInterfaceNumber = 0

            def __iter__(self):
                return iter(eps)
        return [Intf()]

    def is_kernel_driver_active(self, n):
        return False


@pytest.fixture
def usb_calls(monkeypatch):
    import usb.core
    import usb.util
    calls = []
    monkeypatch.setattr(sonyptp, "_libusb_backend", lambda: None)
    monkeypatch.setattr(usb.util, "claim_interface", lambda dev, n: calls.append("claim"))
    monkeypatch.setattr(usb.util, "release_interface", lambda dev, n: calls.append("release"))
    monkeypatch.setattr(usb.util, "dispose_resources", lambda dev: calls.append("dispose"))
    return calls, usb.core


def test_open_failing_after_claim_releases_interface(usb_calls, monkeypatch):
    calls, usb_core = usb_calls
    # OpenSession OK, SDIO Connect(1) refused
    dev = FakeUsbDevice([_container(3, 0x2001, 0), _container(3, 0x2002, 1)])
    monkeypatch.setattr(usb_core, "find", lambda **kw: dev)
    s = SonyPtp()
    with pytest.raises(PtpError):
        with s:
            pass
    assert calls[0] == "claim" and "release" in calls and calls[-1] == "dispose"
    assert s.dev is None


def test_close_disposes_even_if_release_fails(usb_calls, monkeypatch):
    import usb.util
    calls, _ = usb_calls

    def boom(dev, n):
        raise OSError("device gone")
    monkeypatch.setattr(usb.util, "release_interface", boom)
    s = _sony([])
    s.intf = 0
    s.close()
    assert calls == ["dispose"] and s.dev is None


class _TextWidget:
    def __init__(self, name: str, value: str = "x") -> None:
        self.name, self.value, self.reads = name, value, 0

    def get_type(self):
        return gp.GP_WIDGET_TEXT

    def get_name(self):
        return self.name

    def get_label(self):
        return self.name

    def get_readonly(self):
        return 0

    def get_value(self):
        self.reads += 1
        return self.value


class _Window(FakeRoot):
    def __init__(self, children) -> None:
        self.children = children

    def count_children(self):
        return len(self.children)

    def get_child(self, i):
        return self.children[i]


@pytest.mark.parametrize("vendor", ["sony", None])
def test_config_never_reads_sony_control_widgets(vendor):
    """libgphoto2 2.5.34: get_value() on Sony SDIO controls (d2c1…) reads freed memory → segfault."""
    from camctl import vendors
    control, setting = _TextWidget("d2c2"), _TextWidget("d241", "XAVC S 4K")
    cam = PtpCamera()
    cam.cam = FakeGp()
    cam.cam.get_config = lambda: _Window([control, setting])
    cam._vendor = vendors.PROFILES[vendor] if vendor else None
    flat = cam.config()
    assert control.reads == 0 and flat["d2c2"]["value"] is None
    assert flat["d241"]["value"] == "XAVC S 4K"


def test_open_self_heals_when_macos_ptpcamerad_holds_the_camera(monkeypatch):
    """[-53] claim error → kill ptpcamerad once and retry (verified on hardware 2026-09-28)."""
    from camctl import ptp
    attempts, killed = [], []

    def fake_init(self):
        attempts.append(1)
        if len(attempts) == 1:
            raise gp.GPhoto2Error(gp.GP_ERROR_IO_USB_CLAIM)
        return FakeGp()

    monkeypatch.setattr(ptp.PtpCamera, "_init", fake_init)
    monkeypatch.setattr(ptp, "release_macos_holder", lambda: killed.append(1) or True)
    monkeypatch.setattr(ptp.time, "sleep", lambda s: None)
    monkeypatch.setattr(ptp.PtpCamera, "_detect_vendor", lambda self: None)
    cam = ptp.PtpCamera().open()
    assert cam.cam is not None and len(attempts) == 2 and killed == [1]


def test_open_claim_error_without_holder_is_clear(monkeypatch):
    from camctl import ptp
    monkeypatch.setattr(ptp.PtpCamera, "_init",
                        lambda self: (_ for _ in ()).throw(gp.GPhoto2Error(gp.GP_ERROR_IO_USB_CLAIM)))
    monkeypatch.setattr(ptp, "release_macos_holder", lambda: False)
    with pytest.raises(CameraError, match="Quit Image Capture"):
        ptp.PtpCamera().open()


@pytest.mark.parametrize("had_session,expect_tries", [(True, 3), (False, 1)])
def test_open_retries_reenumerating_camera_only_on_reopen(monkeypatch, had_session, expect_tries):
    """[-105] right after close (camera re-enumerating) is retried; a first open with no camera fails fast."""
    from camctl import ptp, vendors
    attempts = []

    def fake_init(self):
        attempts.append(1)
        if len(attempts) < 3:
            raise gp.GPhoto2Error(gp.GP_ERROR_MODEL_NOT_FOUND)
        return FakeGp()

    monkeypatch.setattr(ptp.PtpCamera, "_init", fake_init)
    monkeypatch.setattr(ptp.time, "sleep", lambda s: None)
    monkeypatch.setattr(ptp.PtpCamera, "_detect_vendor", lambda self: vendors.PROFILES["sony"])
    cam = ptp.PtpCamera()
    cam._vendor = vendors.PROFILES["sony"] if had_session else None
    if had_session:
        assert cam.open().cam is not None
    else:
        with pytest.raises(CameraError, match="no camera found"):
            cam.open()
    assert len(attempts) == expect_tries


def test_reopen_retries_claim_error_while_old_session_drains(monkeypatch):
    from camctl import ptp, vendors
    attempts = []

    def fake_init(self):
        attempts.append(1)
        if len(attempts) < 3:
            raise gp.GPhoto2Error(gp.GP_ERROR_IO_USB_CLAIM)
        return FakeGp()

    monkeypatch.setattr(ptp.PtpCamera, "_init", fake_init)
    monkeypatch.setattr(ptp.time, "sleep", lambda s: None)
    cam = ptp.PtpCamera()
    cam._vendor = vendors.PROFILES["sony"]
    assert cam.open().cam is not None and len(attempts) == 3
