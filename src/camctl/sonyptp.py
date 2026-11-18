"""Minimal raw PTP-over-USB client speaking Sony's SDIO extension.

Why this exists: libgphoto2 2.5.34 classifies Sony vendor codes with `code & 0x7000` and silently
drops the 0xE000 range, which is where the A7 V keeps Log Shooting (0xE0E3), Log color gamut
(0xE0E4), zebra, audio level, etc. This client talks to the camera directly (no libgphoto2) so
those properties can be read and written.

Protocol (mirrors libgphoto2 camlibs/ptp2 for Sony "mode 3" bodies):
  OpenSession → SDIO_Connect(1) → SDIO_Connect(2) → GetExtDeviceInfo(0x12C, 1) → SDIO_Connect(3)
  GetAllExtDevicePropInfo (0x9209): u32 count, u32 0, then Sony DPDs:
      u16 code, u16 datatype, u8 getset, u8 isenabled, default, current, u8 formflag, form,
      [optional 2nd enum list on 2024+ bodies: u16 n + values, detected by next u16 < 0x200]
  SetExtDevicePropValue (0x9205, code) + data phase = value packed as its datatype.

Only one USB client can own the camera: close any libgphoto2 session first.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SONY_VID = 0x054C
OC_OPEN_SESSION, OC_CLOSE_SESSION = 0x1002, 0x1003
OC_SDIO_CONNECT, OC_SDIO_GET_EXT_INFO = 0x9201, 0x9202
OC_SDIO_SET_PROP, OC_SDIO_CONTROL, OC_SDIO_GET_ALL = 0x9205, 0x9207, 0x9209
RC_OK, RC_SESSION_ALREADY_OPEN = 0x2001, 0x201E
T_COMMAND, T_DATA, T_RESPONSE = 1, 2, 3
PROTOCOL_3 = 0x12C

# PTP datatype → struct format
DT = {0x0001: "b", 0x0002: "B", 0x0003: "h", 0x0004: "H", 0x0005: "i", 0x0006: "I", 0x0007: "q", 0x0008: "Q"}
DT_STR = 0xFFFF


class PtpError(RuntimeError):
    pass


def _libusb_backend():
    import usb.backend.libusb1 as libusb1
    import gphoto2
    candidates = [*Path(gphoto2.__file__).parent.glob(".dylibs/libusb-1.0*.dylib"),
                  Path("/opt/homebrew/lib/libusb-1.0.dylib"), Path("/usr/local/lib/libusb-1.0.dylib")]
    for c in candidates:
        if c.exists():
            be = libusb1.get_backend(find_library=lambda _n, c=c: str(c))
            if be:
                return be
    return libusb1.get_backend()


@dataclass
class Prop:
    code: int
    datatype: int
    getset: int
    enabled: int
    current: Any
    default: Any = None
    form: str = "none"
    values: list = field(default_factory=list)       # enum values (or [min, max, step] for range)
    settable_values: list = field(default_factory=list)

    @property
    def writable(self) -> bool:
        # getset: 0 get-only, 1 get/set. isenabled: 0 disabled, 1 enabled, 2 display-only.
        return self.getset == 1 and self.enabled == 1


def _unpack_value(buf: bytes, off: int, dt: int) -> tuple[Any, int]:
    if dt == DT_STR:
        n = buf[off]
        s = buf[off + 1: off + 1 + 2 * n].decode("utf-16-le", "replace").rstrip("\x00")
        return s, off + 1 + 2 * n
    fmt = DT.get(dt)
    if fmt is None:
        raise PtpError(f"unsupported datatype 0x{dt:04x}")
    size = struct.calcsize(fmt)
    return struct.unpack_from("<" + fmt, buf, off)[0], off + size


def parse_props(blob: bytes) -> dict[int, Prop]:
    props: dict[int, Prop] = {}
    off = 8                                        # u32 count + u32 reserved
    while off + 6 <= len(blob):
        code, dt, getset, enabled = struct.unpack_from("<HHBB", blob, off)
        off += 6
        try:
            default, off = _unpack_value(blob, off, dt)
            current, off = _unpack_value(blob, off, dt)
        except (PtpError, struct.error, IndexError):
            break                                  # unknown datatype: can't find next record
        p = Prop(code, dt, getset, enabled, current, default)
        try:
            if off < len(blob):
                flag = blob[off]
                off += 1
                if flag == 1:
                    p.form = "range"
                    for _ in range(3):
                        v, off = _unpack_value(blob, off, dt)
                        p.values.append(v)
                elif flag == 2:
                    p.form = "enum"
                    (n,) = struct.unpack_from("<H", blob, off)
                    off += 2
                    for _ in range(n):
                        v, off = _unpack_value(blob, off, dt)
                        p.values.append(v)
                    # 2024+ bodies: second list = values settable right now (next u16 < 0x200)
                    if off + 2 <= len(blob):
                        (n2,) = struct.unpack_from("<H", blob, off)
                        if n2 < 0x200:
                            off += 2
                            for _ in range(n2):
                                v, off = _unpack_value(blob, off, dt)
                                p.settable_values.append(v)
        except (struct.error, IndexError):
            break                                  # truncated blob: keep the complete records
        if off > len(blob):
            break                                  # string ran past the end (slicing doesn't raise)
        props[code] = p
    return props


def pack_value(value: Any, dt: int) -> bytes:
    if dt == DT_STR:
        s = str(value) + "\x00"
        return bytes([len(s)]) + s.encode("utf-16-le")
    return struct.pack("<" + DT[dt], int(value))


class SonyPtp:
    def __init__(self) -> None:
        self.dev = None
        self.ep_in = self.ep_out = None
        self.tid = 0
        self._rx = b""                             # bytes read past the end of the previous container
        self.ext_codes: list[int] = []
        self.version = 0

    # -- USB transport ------------------------------------------------------
    def open(self) -> "SonyPtp":
        import usb.core
        import usb.util

        dev = usb.core.find(idVendor=SONY_VID, backend=_libusb_backend())
        if dev is None:
            raise PtpError("Sony camera not found on USB (PC Remote: On, USB)")
        cfg = dev.get_active_configuration()
        intf = next(i for i in cfg if i.bInterfaceClass == 6)
        try:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        except usb.core.USBError as e:
            from .ptp import release_macos_holder
            if not release_macos_holder():        # macOS ptpcamerad grabs PTP cameras on plug-in
                raise PtpError(f"could not claim the camera ({e}); quit Image Capture / Imaging Edge") from e
            time.sleep(1.0)
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        self.intf = intf.bInterfaceNumber
        self.dev = dev
        self._rx = b""
        try:
            eps = list(intf)
            self.ep_out = next(e for e in eps
                               if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                               and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
            self.ep_in = next(e for e in eps
                              if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                              and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
            rc, _, _ = self.transaction(OC_OPEN_SESSION, [1], reset_tid=True)
            if rc not in (RC_OK, RC_SESSION_ALREADY_OPEN):
                raise PtpError(f"OpenSession failed 0x{rc:04x}")
            self._sdio_handshake()
        except BaseException:
            self.close()                           # __exit__ never runs when __enter__ raises
            raise
        return self

    def close(self) -> None:
        if self.dev is None:
            return
        import usb.util
        try:
            self.transaction(OC_CLOSE_SESSION, [])
        except Exception:  # noqa: BLE001 — best effort
            pass
        try:
            usb.util.release_interface(self.dev, self.intf)
        except Exception:  # noqa: BLE001 — device may already be gone; still free resources
            pass
        finally:
            usb.util.dispose_resources(self.dev)
            self.dev = None

    def __enter__(self) -> "SonyPtp":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _read_container(self, timeout: int = 10000) -> tuple[int, int, bytes]:
        buf, self._rx = self._rx, b""
        while len(buf) < 12:                       # skip zero-length packets ending a previous transfer
            buf += bytes(self.dev.read(self.ep_in, 512 * 1024, timeout))
        length, ctype, code, _tid = struct.unpack_from("<IHHI", buf, 0)
        if length < 12:
            raise PtpError(f"bad container length {length}")
        while len(buf) < length:
            buf += bytes(self.dev.read(self.ep_in, max(512, length - len(buf)), timeout))
        self._rx = buf[length:]                    # no ZLP after a 512-multiple: next container came along
        return ctype, code, buf[12:length]

    def transaction(self, code: int, params: list[int], data_out: bytes | None = None,
                    reset_tid: bool = False) -> tuple[int, list[int], bytes]:
        if reset_tid:
            self.tid = 0
        tid = self.tid
        self.tid += 1
        cmd = struct.pack("<IHHI", 12 + 4 * len(params), T_COMMAND, code, tid) + b"".join(
            struct.pack("<I", p & 0xFFFFFFFF) for p in params)
        self.dev.write(self.ep_out, cmd, 5000)
        if data_out is not None:
            self.dev.write(self.ep_out, struct.pack("<IHHI", 12 + len(data_out), T_DATA, code, tid) + data_out, 10000)
        data = b""
        ctype, rcode, payload = self._read_container()
        if ctype == T_DATA:
            data = payload
            ctype, rcode, payload = self._read_container()
        if ctype != T_RESPONSE:
            raise PtpError(f"expected response container, got type {ctype}")
        rparams = [struct.unpack_from("<I", payload, i)[0] for i in range(0, len(payload) - len(payload) % 4, 4)]
        return rcode, rparams, data

    def _check(self, code: int, params: list[int], data_out: bytes | None = None) -> bytes:
        rc, _, data = self.transaction(code, params, data_out)
        if rc != RC_OK:
            raise PtpError(f"op 0x{code:04x} {[hex(p) for p in params]} → response 0x{rc:04x}")
        return data

    # -- Sony SDIO ----------------------------------------------------------
    def _sdio_handshake(self) -> None:
        self._check(OC_SDIO_CONNECT, [1, 0, 0])
        self._check(OC_SDIO_CONNECT, [2, 0, 0])
        info = b""
        for _ in range(20):
            info = self._check(OC_SDIO_GET_EXT_INFO, [PROTOCOL_3, 1])
            if info:
                break
        if info:
            self.version = struct.unpack_from("<H", info, 0)[0]
            off = 2
            codes: list[int] = []
            for _ in range(2):                       # properties list, then controls list
                if off + 4 > len(info):
                    break
                (n,) = struct.unpack_from("<I", info, off)
                off += 4
                codes += list(struct.unpack_from(f"<{n}H", info, off))
                off += 2 * n
            self.ext_codes = codes
        self._check(OC_SDIO_CONNECT, [3, 0, 0])

    def props(self) -> dict[int, Prop]:
        return parse_props(self._check(OC_SDIO_GET_ALL, []))

    def set_prop(self, code: int, value: Any, datatype: int | None = None) -> None:
        if datatype is None:
            p = self.props().get(code)
            if p is None:
                raise PtpError(f"camera does not report property 0x{code:04x}")
            datatype = p.datatype
        self._check(OC_SDIO_SET_PROP, [code], pack_value(value, datatype))


# -- human-readable reference ------------------------------------------------

# Standard PTP codes with Sony vendor values (libgphoto2 camlibs/ptp2 tables, verified on the A7 V).
PTP_STD_VALUES = {
    "5005": {"2": "Auto", "4": "Daylight", "6": "Incandescent", "7": "Flash", "32769": "Fluor: Warm White",
             "32770": "Fluor: Cool White", "32771": "Fluor: Day White", "32772": "Fluor: Daylight",
             "32784": "Cloudy", "32785": "Shade", "32786": "Color Temp", "32800": "Custom 1",
             "32801": "Custom 2", "32802": "Custom 3", "32816": "Underwater Auto"},
    "500a": {"1": "MF", "2": "AF-S", "32772": "AF-C", "32773": "AF-A", "32774": "DMF"},
    "500b": {"32769": "Multi", "32770": "Center", "32771": "Entire Screen Avg", "32772": "Spot Standard",
             "32773": "Spot Large", "32774": "Highlight"},
}


def sony_props_path() -> Path:
    """Optional user-supplied property-name table ({"<hex code>": {"name", "values"}}); not shipped with camctl.
    Without it, explain() shows hex codes and camctl's own labels."""
    from .paths import CONFIG_DIR
    return Path(os.environ.get("CAMCTL_SONY_PROPS") or CONFIG_DIR / "sony_props.json").expanduser()


def _reference() -> dict[str, dict]:
    import json
    from .settings import CANONICAL, ENUMS
    path = sony_props_path()
    try:
        ref = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as e:
        raise PtpError(f"Cannot read the Sony property table {path}: {e}") from e
    for code, values in PTP_STD_VALUES.items():
        ref.setdefault(code, {"name": code})["values"] = values
    for c in CANONICAL:                               # camctl's verified labels win over the table's
        for cand in c.candidates:
            if len(cand) == 4 and cand in ref:
                ref[cand]["key"] = c.key
                if c.key in ENUMS:
                    ref[cand]["values"] = ENUMS[c.key]
    return ref


def _fmt(code: int, v: Any, values: dict[str, str]) -> str:
    if isinstance(v, str):
        return repr(v)
    if str(v) in values:
        return values[str(v)]
    if code == 0xD20D:                                # shutter: numerator << 16 | denominator
        n, d = v >> 16, v & 0xFFFF
        if v == 0 or not d:
            return "Bulb" if v == 0 else str(v)
        return f"1/{d}" if n == 1 else f'{n / d:g}"'
    if code == 0x5007:
        return f"f/{v / 100:g}"
    if code == 0xD21E:
        iso = v & 0xFFFFFF
        return "Auto" if iso == 0xFFFFFF else f"ISO {iso}"
    if code == 0xD20F:
        return f"{v}K"
    return str(v)


def explain(props: dict[int, Prop]) -> list[dict]:
    """Decode every property the camera reports: name (from the optional table), current value, writability,
    choices."""
    ref = _reference()
    rows = []
    for code in sorted(props):
        p, r = props[code], ref.get(f"{code:04x}", {})
        values = r.get("values", {})
        choices = p.settable_values or p.values
        rows.append({
            "code": f"{code:04x}", "name": r.get("name", "?"), "key": r.get("key"),
            "value": _fmt(code, p.current, values), "raw": p.current,
            "writable": p.writable, "enabled": {0: "no", 1: "yes", 2: "display-only"}.get(p.enabled, p.enabled),
            **({"range": p.values} if p.form == "range" else
               {"choices": [_fmt(code, c, values) for c in choices]} if choices else {}),
        })
    return rows
