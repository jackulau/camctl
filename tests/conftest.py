"""Shared fixtures: an in-memory camera that behaves like PtpCamera without USB."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from camctl.ptp import CameraError

SHUTTERS = ["30", "15", "8", "4", "2", "1", "1/2", "1/4", "1/8", "1/15", "1/25", "1/30", "1/40", "1/50", "1/60",
            "1/80", "1/100", "1/125", "1/160", "1/200", "1/250", "1/320", "1/400", "1/500", "1/1000", "1/2000"]


def widget_map() -> dict[str, dict[str, Any]]:
    """Small flat map shaped like PtpCamera.config() on an ILCE-7M5 in Intelligent Auto."""
    return {
        "cameramodel": {"label": "Camera Model", "readonly": True, "type": "text", "value": "ILCE-7M5"},
        "batterylevel": {"label": "Battery Level", "readonly": True, "type": "text", "value": "80%"},
        "d25a": {"label": "Priority Mode", "readonly": False, "type": "menu", "value": "0", "choices": ["0", "1"]},
        "500e": {"label": "Exposure Program Mode", "readonly": True, "type": "menu", "value": "294912",
                 "choices": ["294912", "65538", "131075", "196612", "1", "491600", "491601", "491602", "491603"]},
        "shutterspeed": {"label": "Shutter Speed", "readonly": True, "type": "radio", "value": "1/60",
                         "choices": SHUTTERS},
        "f-number": {"label": "F-Number", "readonly": True, "type": "radio", "value": "f/4",
                     "choices": ["f/1.8", "f/2", "f/2.8", "f/4", "f/5.6", "f/8"]},
        "iso": {"label": "ISO Speed", "readonly": True, "type": "radio", "value": "Auto ISO",
                "choices": ["Auto ISO", "100", "200", "400", "800", "1600", "3200", "6400"]},
        "whitebalance": {"label": "WhiteBalance", "readonly": False, "type": "radio", "value": "Automatic",
                         "choices": ["Automatic", "Daylight", "Shade", "Cloudy", "Tungsten",
                                     "Choose Color Temperature"]},
        "colortemperature": {"label": "Color Temperature", "readonly": False, "type": "range", "value": 5500.0,
                             "range": [2500.0, 9900.0, 100.0]},
        "focusmode": {"label": "Focus Mode", "readonly": False, "type": "radio", "value": "AF-A",
                      "choices": ["Automatic", "AF-A", "AF-C", "DMF", "Manual"]},
        "d23f": {"label": "Picture Profile", "readonly": False, "type": "menu", "value": "0",
                 "choices": ["0", "1", "2", "3", "4", "5", "6", "10", "11"]},
        "d241": {"label": "File Format Movie", "readonly": False, "type": "menu", "value": "9",
                 "choices": ["11", "8", "9", "14", "15"]},
        "d242": {"label": "Recording Setting Movie", "readonly": False, "type": "menu", "value": "63",
                 "choices": ["54", "56", "61", "63", "66"]},
        "d157": {"label": "PTP Property 0xd157", "readonly": False, "type": "menu", "value": "1",
                 "choices": ["2", "1"]},
        "movie": {"label": "Movie", "readonly": False, "type": "toggle", "value": 0},
    }


class FakeCamera:
    """Implements the Backend protocol (config/widget/set/drain_events) plus the action methods.

    Lock rules mirror the real body: 500e is read-only until d25a=1 (PC Remote priority), and
    shutter/aperture/ISO stay read-only until the camera is in M (they unlock on the next event drain,
    like the ~0.5 s delay observed on hardware).
    """

    def __init__(self) -> None:
        self.flat = widget_map()
        self.writes: list[tuple[str, Any]] = []
        self.opened = 0
        self.closed = 0
        self._pending_unlock = False

    # context manager like PtpCamera
    def __enter__(self) -> "FakeCamera":
        self.opened += 1
        return self

    def __exit__(self, *exc) -> None:
        self.closed += 1

    def config(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.flat)

    def widget(self, name: str) -> dict[str, Any]:
        if name not in self.flat:
            raise CameraError(f"{name}: no such widget")
        return copy.deepcopy(self.flat[name])

    def set(self, name: str, value: Any) -> None:
        w = self.flat.get(name)
        if w is None:
            raise CameraError(f"{name}: no such widget")
        if w["readonly"]:
            raise CameraError(f"{name}={value!r}: camera rejected the value (Bad parameters)")
        if "choices" in w and str(value) not in w["choices"]:
            raise CameraError(f"{name}={value!r}: not a valid choice")
        w["value"] = value if w["type"] in ("range", "toggle") else str(value)
        self.writes.append((name, w["value"]))
        if name == "d25a":
            self.flat["500e"]["readonly"] = str(value) != "1"
        if name == "500e":
            self._pending_unlock = str(value) in ("1", "491603")

    def drain_events(self, seconds: float = 0.5) -> list:
        if self._pending_unlock:
            for n in ("shutterspeed", "f-number", "iso"):
                self.flat[n]["readonly"] = False
            self._pending_unlock = False
        return []

    # actions
    def summary(self) -> str:
        return "Model: ILCE-7M5"

    def preview_jpeg(self) -> bytes:
        return b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"

    def movie(self, start: bool) -> None:
        self.set("movie", 1 if start else 0)


@pytest.fixture
def cam() -> FakeCamera:
    return FakeCamera()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep backups, user presets, BLE pairings and offload logs out of ~/.config, never talk to a running BLE daemon,
    never enumerate real USB cameras (a test that wants a detected camera patches ptp.autodetect itself) and never
    reach Google Drive."""
    from camctl import core, presets, ptp
    from camctl.ble import daemon, store
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(presets, "USER_DIR", tmp_path / "presets")
    monkeypatch.setattr(store, "STATE", tmp_path / "ble.json")
    monkeypatch.setattr(daemon, "SOCKET", tmp_path / "no-daemon.sock")
    monkeypatch.setattr(ptp, "autodetect", lambda: [])
    monkeypatch.setattr(ptp, "release_macos_holder", lambda: False)   # never kill the real ptpcamerad
    # Drive: no credentials (a stray Drive() raises instead of asking gcloud), logs in tmp_path.
    from camctl import offload, organize
    monkeypatch.setenv("CAMCTL_DRIVE_AUTH", "oauth")
    monkeypatch.setenv("CAMCTL_DRIVE_TOKEN", str(tmp_path / "no-drive-token.json"))
    monkeypatch.setattr(offload, "LOG_DIR", tmp_path / "offload-logs")
    monkeypatch.setattr(organize, "LOG_DIR", tmp_path / "offload-logs")
    monkeypatch.setattr(offload, "HASH_CACHE", tmp_path / "md5-cache.json")
    monkeypatch.setattr(offload, "volume_id", lambda card: f"name:{card.name}")
    # Never read the real ~/.config/camctl/organize.yaml or Sony property table: tests see the defaults.
    monkeypatch.setenv("CAMCTL_ORGANIZE_CONFIG", str(tmp_path / "no-organize.yaml"))
    monkeypatch.setattr(organize, "_layout_cache", None)
    monkeypatch.setenv("CAMCTL_SONY_PROPS", str(tmp_path / "no-sony-props.json"))
    # Hard guards: a test that forgets to fake the camera / Bluetooth fails instead of driving real hardware
    # (camctl.ble.api binds scan() internally, so patching camctl.ble.scan alone does not stop a real scan).
    import bleak
    import gphoto2
    from camctl.ble import bond
    monkeypatch.setattr(gphoto2, "Camera", _no_hardware("USB camera (gphoto2.Camera)"))
    monkeypatch.setattr(bleak, "BleakScanner", _no_hardware("Bluetooth scan (bleak.BleakScanner)"))
    monkeypatch.setattr(bleak, "BleakClient", _no_hardware("Bluetooth connection (bleak.BleakClient)"))
    monkeypatch.setattr(bond, "_run", _no_hardware("blueutil"))
    # ...and one that forgets to fake Drive fails instead of calling Google (every requests call goes through here).
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", _no_hardware("network HTTP (Google Drive)"))
    _HARDWARE_ATTEMPTS.clear()
    yield
    # MCP tools turn every exception into {"error": ...}; this catches an attempt that was swallowed that way.
    assert not _HARDWARE_ATTEMPTS, f"test tried to use real hardware: {_HARDWARE_ATTEMPTS}"


_HARDWARE_ATTEMPTS: list[str] = []


def _no_hardware(what: str):
    def refuse(*args, **kwargs):
        _HARDWARE_ATTEMPTS.append(what)
        raise AssertionError(f"test tried to use real hardware: {what} — fake it in the test")
    return refuse
