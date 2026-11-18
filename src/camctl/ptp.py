"""PTP backend (USB-C and Wi-Fi PTP/IP) via libgphoto2.

Multi-vendor: the camera's vendor is detected on open (`PtpCamera.vendor`, see vendors.py) and
capture / movie / half-press go through that vendor's hooks. Per-vendor camera setup: vendors.setup_hints().

Camera prerequisites (A7 V):
  Menu → Network → Transfer/Remote → PC Remote Function → PC Remote: On
  PC Remote Cnct Method: USB (or Wi-Fi Access Point / Wi-Fi Direct for PTP/IP)

Observed on ILCE-7M5 fw 1.01 with libgphoto2 2.5.34:
  * detected as a generic "USB PTP Class Camera" with Sony PTP extensions working
  * `expprogram` follows the physical mode dial: remote writes fail with "Bad parameters"
  * in Intelligent Auto, shutter / aperture / ISO / WB / metering are read-only
  * card storage is NOT visible while in PC Remote mode
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import gphoto2 as gp

from . import vendors
from .vendors import CameraError, VendorProfile

CONTAINER = (gp.GP_WIDGET_WINDOW, gp.GP_WIDGET_SECTION)
CHOICE = (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU)
TYPE_NAMES = {gp.GP_WIDGET_TEXT: "text", gp.GP_WIDGET_RANGE: "range", gp.GP_WIDGET_TOGGLE: "toggle",
              gp.GP_WIDGET_RADIO: "radio", gp.GP_WIDGET_MENU: "menu", gp.GP_WIDGET_BUTTON: "button",
              gp.GP_WIDGET_DATE: "date"}


__all__ = ["CameraError", "PtpCamera", "autodetect"]


def _explain(e: gp.GPhoto2Error) -> str:
    if e.code in (gp.GP_ERROR_IO_USB_CLAIM, gp.GP_ERROR_IO_LOCK):
        return (f"{e} — another app holds the camera (camctl already tried stopping macOS ptpcamerad). "
                "Quit Image Capture, Photos, Imaging Edge / Imaging Capture or Lightroom/Capture One tethering.")
    if e.code == gp.GP_ERROR_MODEL_NOT_FOUND:
        return (f"{e} — no camera found. Check the USB cable (data, not charge-only) and the camera's USB mode: "
                "Sony PC Remote: On (USB); Canon Wi-Fi off; Nikon MTP/PTP; Fujifilm USB Tether Shooting Auto; "
                "Panasonic PC(Tether); OM System RAW/Control.")
    if e.code == gp.GP_ERROR_BAD_PARAMETERS:
        return f"{e} — camera rejected the value (property locked by current mode/dial, or value invalid)."
    return str(e)


def release_macos_holder() -> bool:
    """Free a USB camera claimed by macOS ptpcamerad (Image Capture's daemon grabs PTP cameras on plug-in).

    ptpcamerad ignores SIGTERM while it holds a device (verified on macOS 26 with an A7 V), so it gets SIGKILL;
    launchd relaunches it on demand and it does not re-claim a camera that is already in use. True = killed."""
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(["pkill", "-KILL", "-x", "ptpcamerad"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# A reopen straight after close can briefly fail with these while the camera re-enumerates (A7 V after a
# RAW file-type write, 2026-09-28); [-53] claim errors are retried the same way. Only when this object
# already had a session, so a first open with no camera still fails fast.
REENUMERATING = frozenset({gp.GP_ERROR_MODEL_NOT_FOUND, gp.GP_ERROR_IO_USB_FIND})
REOPEN_TRIES = 5


class PtpCamera:
    """One open PTP session. Use as a context manager or call open()/close()."""

    def __init__(self, port: str | None = None) -> None:
        self.port = port          # None = autodetect USB; "usb:002,002"; "ptpip:192.168.122.1"
        self.cam: gp.Camera | None = None
        # libgphoto2 2.5.34 + Sony: a full get_config() after a property write can segfault (stale
        # vendor-prop cache). Reopening the session before the next full read avoids it.
        self._dirty = False
        # Remote exposure-mode control is session-scoped on Sony: priority (d25a) and mode (500e)
        # revert to the physical dial when the session closes, so re-assert them after a reopen.
        self._sticky: dict[str, Any] = {}
        self._vendor: VendorProfile | None = None     # detected on open()

    # -- session ------------------------------------------------------------
    def open(self) -> "PtpCamera":
        try:
            cam = self._init()
        except gp.GPhoto2Error as e:
            cam = self._recover(e)
        self.cam = cam
        if self._vendor is None:
            self._vendor = self._detect_vendor()
        return self

    def _recover(self, err: gp.GPhoto2Error) -> gp.Camera:
        """Self-heal the open failures seen on real hardware; anything else raises."""
        reopen = self._vendor is not None         # this object just closed its own session
        if err.code == gp.GP_ERROR_IO_USB_CLAIM:
            killed = release_macos_holder()       # ptpcamerad grabbed it, or our old claim is still draining
            tries = REOPEN_TRIES if reopen else int(killed)
        elif err.code in REENUMERATING and reopen:
            tries = REOPEN_TRIES                  # camera still re-enumerating after the close
        else:
            tries = 0
        if not tries:
            raise CameraError(_explain(err)) from err
        for attempt in range(tries):
            if attempt and err.code == gp.GP_ERROR_IO_USB_CLAIM:
                release_macos_holder()            # launchd respawns ptpcamerad and it grabs the camera again
            time.sleep(1.0)
            try:
                return self._init()
            except gp.GPhoto2Error as e:
                err = e
        raise CameraError(_explain(err)) from err

    def _init(self) -> gp.Camera:
        cam = gp.Camera()
        if self.port:
            ports = gp.PortInfoList()
            ports.load()
            idx = ports.lookup_path(self.port)
            if idx < 0:
                raise CameraError(f"port {self.port!r} not available")
            cam.set_port_info(ports[idx])
            if self.port.startswith("ptpip:"):
                abilities = gp.CameraAbilitiesList()
                abilities.load()
                cam.set_abilities(abilities[abilities.lookup_model("PTP/IP Camera")])
        cam.init()
        return cam

    def _detect_vendor(self) -> VendorProfile | None:
        """Cheap identity probe (single widgets + summary, no full config walk). None = undetermined."""
        identity: dict[str, dict[str, Any]] = {}
        try:
            for name in ("manufacturer", "cameramodel", "vendorextension"):
                try:
                    identity[name] = {"value": self._c.get_single_config(name).get_value()}
                except gp.GPhoto2Error:
                    pass
            summary = str(self._c.get_summary())
        except Exception:  # noqa: BLE001 — detection must never break opening the camera
            summary = ""
        if not identity and not summary:
            return None
        profile = vendors.detect(identity, summary)
        return None if profile is vendors.GENERIC and not identity else profile

    @property
    def vendor(self) -> VendorProfile:
        """Vendor profile of the connected camera (generic PTP until identified)."""
        if self._vendor is None and self.cam is not None:
            self._vendor = self._detect_vendor()
        return self._vendor or vendors.GENERIC

    def close(self) -> None:
        if self.cam is not None:
            try:
                self.cam.exit()
            except gp.GPhoto2Error:
                pass                              # best effort: never mask the caller's exception
            finally:
                self.cam = None

    def __enter__(self) -> "PtpCamera":
        return self.open() if self.cam is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def _c(self) -> gp.Camera:
        if self.cam is None:
            raise CameraError("camera session not open")
        return self.cam

    # -- config -------------------------------------------------------------
    def config(self) -> dict[str, dict[str, Any]]:
        """Flat {widget_name: {label, readonly, type, value, choices?, range?}}."""
        if self._dirty and (self._vendor is None or self._vendor.reopen_after_write):
            self.close()
            self.open()
            self._dirty = False
            for name in sorted(self._sticky, key=lambda n: n != "d25a"):   # priority before mode
                try:
                    self.set(name, self._sticky[name])
                except CameraError:
                    del self._sticky[name]        # e.g. dial moved; don't fail every later read on it
            self._dirty = False
        out: dict[str, dict[str, Any]] = {}
        write_only = self._write_only()

        def walk(w: gp.CameraWidget) -> None:
            t = w.get_type()
            if t in CONTAINER:
                for i in range(w.count_children()):
                    walk(w.get_child(i))
                return
            v = None if w.get_name() in write_only else self._value(w)
            d: dict[str, Any] = {"label": w.get_label(), "readonly": bool(w.get_readonly()),
                                 "type": TYPE_NAMES.get(t, str(t)), "value": v}
            if t in CHOICE:
                d["choices"] = [w.get_choice(i) for i in range(w.count_choices())]
            elif t == gp.GP_WIDGET_RANGE:
                d["range"] = list(w.get_range())
            out[w.get_name()] = d

        try:
            walk(self._c.get_config())
        except gp.GPhoto2Error as e:
            raise CameraError(_explain(e)) from e
        return out

    def widget(self, name: str) -> dict[str, Any]:
        """Fresh info for one widget (safe after writes, unlike a full get_config)."""
        try:
            w = self._c.get_single_config(name)
        except gp.GPhoto2Error as e:
            raise CameraError(_explain(e)) from e
        t = w.get_type()
        v = None if name in self._write_only() else self._value(w)
        d: dict[str, Any] = {"label": w.get_label(), "readonly": bool(w.get_readonly()),
                             "type": TYPE_NAMES.get(t, str(t)), "value": v}
        if t in CHOICE:
            d["choices"] = [w.get_choice(i) for i in range(w.count_choices())]
        elif t == gp.GP_WIDGET_RANGE:
            d["range"] = list(w.get_range())
        return d

    def _write_only(self) -> frozenset[str]:
        # Undetected vendor: still skip Sony's controls — reading them can segfault, skipping costs nothing.
        return self._vendor.write_only if self._vendor is not None else vendors.SONY_CONTROLS

    @staticmethod
    def _value(w: gp.CameraWidget) -> Any:
        try:
            return w.get_value()
        except gp.GPhoto2Error:
            return None                           # some Sony props can't be read

    def get(self, name: str) -> Any:
        if name in self._write_only():
            return None
        try:
            return self._c.get_single_config(name).get_value()
        except gp.GPhoto2Error as e:
            raise CameraError(_explain(e)) from e

    def set(self, name: str, value: Any) -> None:
        """Write one widget. `value` must already be a valid choice/number for the widget."""
        try:
            w = self._c.get_single_config(name)
            t = w.get_type()
            if t == gp.GP_WIDGET_RANGE:
                value = float(value)
            elif t in (gp.GP_WIDGET_TOGGLE, gp.GP_WIDGET_DATE):
                value = int(value)
            else:
                value = str(value)
            w.set_value(value)
            self._c.set_single_config(name, w)
            if name in ("d25a", "500e"):
                self._sticky[name] = value
        except gp.GPhoto2Error as e:
            raise CameraError(f"{name}={value!r}: {_explain(e)}") from e
        finally:
            self._dirty = True
        self.drain_events(0.3)

    def drain_events(self, seconds: float = 0.5) -> list[tuple[int, Any]]:
        """Let the camera settle after a write; returns events seen."""
        events = []
        end = time.time() + seconds
        while time.time() < end:
            typ, data = self._c.wait_for_event(100)
            if typ == gp.GP_EVENT_TIMEOUT:
                continue
            events.append((typ, data))
        return events

    # -- actions ------------------------------------------------------------
    def summary(self) -> str:
        return str(self._c.get_summary())

    def capture(self, dest: Path, wait_extra: float = 3.0) -> list[Path]:
        """Take a still and download every file it produces (RAW+JPEG → 2 files)."""
        dest.mkdir(parents=True, exist_ok=True)
        self.vendor.prepare_capture(self)         # e.g. Canon/Nikon: capturetarget → memory card
        try:
            first = self._c.capture(gp.GP_CAPTURE_IMAGE)
        except gp.GPhoto2Error as e:
            hint = (" — the camera did not release the shutter; in AF-S/AF-C it waits for focus lock "
                    "(lens cap on? too dark? nothing in the AF area?). Try again or use MF/DMF."
                    if e.code == gp.GP_ERROR else "")
            raise CameraError(f"capture: {_explain(e)}{hint}") from e
        paths = [(first.folder, first.name)]
        end = time.time() + wait_extra
        while time.time() < end:
            typ, data = self._c.wait_for_event(250)
            if typ == gp.GP_EVENT_FILE_ADDED:
                paths.append((data.folder, data.name))
                end = time.time() + 1.0
        saved = []
        for folder, name in dict.fromkeys(paths):
            target = dest / name
            n = 0
            while target.exists():                # Sony tethered names restart at capt0000 every session
                n += 1
                target = dest / f"{Path(name).stem}-{n}{Path(name).suffix}"
            self._c.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL).save(str(target))
            saved.append(target)
        return saved

    def preview_jpeg(self) -> bytes:
        try:
            f = self._c.capture_preview()
            return bytes(f.get_data_and_size())  # keep `f` alive: the memoryview points into its buffer
        except gp.GPhoto2Error as e:
            raise CameraError(f"live view: {_explain(e)}{self.vendor.refusal_hint(self, 'live_view')}") from e

    def movie(self, start: bool) -> None:
        # Sony: 'movie' toggle — 1 starts, 0 stops (needs Movie on the Photo/Movie switch). Canon: live view +
        # movierecordtarget; Nikon/Panasonic: 'movie' with a readable refusal; Fuji/OM: clear error.
        self.vendor.movie(self, start)

    def half_press(self, on: bool) -> None:
        self.vendor.half_press(self, on)


def autodetect() -> list[tuple[str, str]]:
    cams = gp.Camera.autodetect()
    return [(cams.get_name(i), cams.get_value(i)) for i in range(cams.count())]
