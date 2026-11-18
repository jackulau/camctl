"""Bluetooth LE camera remotes: Sony (verified on an A7 V), Canon, Fujifilm, Nikon, GoPro, Panasonic (experimental);
OM System is recognised but has no public protocol.

Remote control only (shutter, AF, record, zoom/focus where the brand offers it) — camera settings need USB/Wi-Fi.
Non-Sony protocols are credited to Furble (MIT, github.com/gkoh/furble, commit 0cac22a) and Open GoPro (Apache-2.0).

Integrator API (all async, JSON-safe results, BleError subclasses with one-sentence fixes):
    scan(timeout, brands=None) -> list[Found]
    pair(address=None, brand=None, on_prompt=..., verify=True, allow_bt_restart=False) -> dict
    execute(cmd, args, address=None, brand=None, use_daemon=True, on_prompt=...) -> dict
    unpair(address_or_name, clear_bond=False, allow_bt_restart=False) -> dict
    serve(address=None, brand=None, socket_path=None) / send_via_daemon(cmd, args) / daemon_status()
    clear_stale_bond(name_or_address, allow_bt_restart=False) -> dict
    supported_brands() -> dict

The pre-multi-brand API is unchanged: scan, Found, BleRemote (= SonyRemote), remember, remembered, CMD, STATUS,
parse_status, zoom_cmd, focus_cmd, STATE, REMOTE_SERVICE.
"""

from __future__ import annotations

import asyncio  # noqa: F401 — kept as an attribute: callers/tests patch ble.asyncio.sleep

from .api import create_remote, execute, pair, resolve_device, serve, unpair
from .base import (AF_ON, AUTOFOCUS, BULB, C1, CAPABILITIES, FOCUS, RECORD, SHOOT, ZOOM, Event, PairResult,
                   RecordResult, Remote, ShotResult)
from .bond import clear_stale_bond, find_paired_mac, is_insufficient_encryption, is_peer_removed, stale_bond_guidance
from .canon import CanonRemote
from .commands import COMMANDS, dispatch
from .daemon import SOCKET, BleDaemon, daemon_status, send_via_daemon
from .errors import (BleError, BluetoothUnavailable, BondRestartRequired, CameraNotFound, ConnectFailed, DaemonError,
                     NotSupported, PairingFailed, PairingNotSaved, PairingRequired)
from .fujifilm import FujifilmRemote
from .gopro import GoProRemote
from .nikon import NikonRemote
from .omsystem import OMSystemRemote
from .panasonic import PanasonicRemote
from .registry import DRIVERS, driver_for, normalize_brand, supported_brands
from .scan import AdvInfo, Found, detect, scan
from .sony import (CAMERA_DEVICE_TYPE, CMD, REMOTE_SERVICE, SONY_COMPANY_ID, STATUS, BleRemote, SonyRemote,
                   _pairing_flag, focus_cmd, parse_status, zoom_cmd)
from .store import STATE, Device, devices, forget, get_device, remember, remembered, set_default

__all__ = [
    # high level
    "scan", "pair", "execute", "unpair", "serve", "resolve_device", "create_remote", "supported_brands",
    "send_via_daemon", "daemon_status", "BleDaemon", "SOCKET", "clear_stale_bond", "dispatch", "COMMANDS",
    # results / model
    "Found", "AdvInfo", "detect", "Device", "ShotResult", "RecordResult", "PairResult", "Event",
    "SHOOT", "AUTOFOCUS", "FOCUS", "RECORD", "ZOOM", "BULB", "AF_ON", "C1", "CAPABILITIES",
    # drivers
    "Remote", "SonyRemote", "BleRemote", "CanonRemote", "FujifilmRemote", "NikonRemote", "PanasonicRemote",
    "GoProRemote", "OMSystemRemote", "DRIVERS", "driver_for", "normalize_brand",
    # store
    "STATE", "remember", "remembered", "devices", "get_device", "set_default", "forget",
    # bond helpers
    "is_peer_removed", "is_insufficient_encryption", "find_paired_mac", "stale_bond_guidance",
    # errors
    "BleError", "NotSupported", "CameraNotFound", "ConnectFailed", "BluetoothUnavailable", "PairingRequired",
    "PairingFailed", "PairingNotSaved", "BondRestartRequired", "DaemonError",
    # Sony protocol (pre-multi-brand API)
    "CMD", "STATUS", "REMOTE_SERVICE", "SONY_COMPANY_ID", "CAMERA_DEVICE_TYPE", "parse_status", "zoom_cmd",
    "focus_cmd", "_pairing_flag",
]
