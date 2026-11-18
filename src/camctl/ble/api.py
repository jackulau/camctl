"""High-level async entry points for the CLI and MCP server.

    found  = await scan(timeout)                                   # every brand
    result = await pair(address=None, brand=None, on_prompt=print_to_user, allow_bt_restart=False)
    result = await execute("shoot", {"af": True})                  # daemon if running, else direct connect
    result = await execute("record", {"action": "start"}, address=..., brand=...)
    info   = await unpair("ILCE-7M5", clear_bond=True, allow_bt_restart=True)
    await serve()                                                  # `camctl ble serve`: hold the link open

All return JSON-safe dicts and raise BleError subclasses whose message is one sentence with the fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import Any

from . import bond, store
from .base import Prompt, Remote
from .commands import check_args, dispatch
from .daemon import BleDaemon, send_via_daemon
from .errors import CameraNotFound, PairingNotSaved
from .registry import DRIVERS, driver_for, normalize_brand
from .scan import Found, scan
from .store import Device

_ADVERTISE_HINT = ("turn on the camera's Bluetooth remote (Sony: MENU → Network → Bluetooth → Bluetooth Rmt Ctrl; "
                   "Canon: Bluetooth function = Remote; Nikon: Connection to remote (ML-L7); Fujifilm/LUMIX/GoPro: "
                   "Bluetooth on) and keep it within a few metres")


def create_remote(device: Device | Found | str, *, brand: str | None = None, on_prompt: Prompt | None = None,
                  **options: Any) -> Remote:
    """Driver instance (not connected) for a remembered Device, a scan result, or a bare address (Sony default)."""
    if isinstance(device, str):
        device = Device(device, None, brand or "sony")
    cls = driver_for(brand or device.brand)
    return cls(device.address, name=device.name, extra=dict(device.extra), on_prompt=on_prompt, **options)


def _device_from_found(f: Found) -> Device:
    return Device(f.address, f.name, f.brand, dict(f.extra))


async def resolve_device(address: str | None = None, brand: str | None = None, *, timeout: float = 6.0) -> Device:
    """Which camera to talk to: the given address (remembered or scanned), else the default, else the best scan hit."""
    b = normalize_brand(brand) if brand else None
    if address:
        dev = store.get_device(address)
        if dev is not None:
            if b and dev.brand != b:
                dev.brand = b
            return dev
        if b:
            return Device(address, None, b)
        hit = next((f for f in await scan(timeout) if f.address.lower() == address.lower()), None)
        return _device_from_found(hit) if hit else Device(address, None, "sony")
    dev = store.get_device()
    if dev is not None and (b is None or dev.brand == b):
        return dev
    if b:
        dev = next((d for d in store.devices() if d.brand == b), None)
        if dev is not None:
            return dev
    cams = await scan(timeout, brands=[b] if b else None, include_unsupported=False)
    if not cams:
        what = f"No {DRIVERS[b].label}" if b else "No supported camera"
        raise CameraNotFound(f"{what} is advertising over Bluetooth; {_ADVERTISE_HINT}.")
    f = cams[0]
    store.remember(f.address, f.name, f.brand, extra=f.extra)
    return _device_from_found(f)


async def _connect(remote: Remote, device: Device, timeout: float) -> None:
    """Connect; for brands whose address rotates (Nikon, LUMIX) re-scan once to find the camera again."""
    try:
        await remote.connect()
    except CameraNotFound:
        if not remote.rotating_address:
            raise
        hit = next((f for f in await scan(timeout, brands=[remote.brand]) if remote.matches_device(f, device)), None)
        if hit is None:
            raise
        remote.address = hit.address
        store.remember(hit.address, device.name or hit.name, remote.brand, extra=device.extra, default=False)
        await remote.connect()


async def pair(address: str | None = None, brand: str | None = None, *, timeout: float = 6.0,
               on_prompt: Prompt | None = None, remote_name: str = "camctl", verify: bool = True,
               allow_bt_restart: bool = False, confirm_wait: float | None = None) -> dict[str, Any]:
    """Pair with a camera in pairing mode, remember it (as default), and prove the pairing survives a reconnect.

    A stale macOS bond (CBError 14) is cleared automatically only with allow_bt_restart=True (restarts Bluetooth);
    otherwise BondRestartRequired carries the exact command / System Settings step.
    """
    b = normalize_brand(brand) if brand else None
    cams = await scan(timeout, brands=[b] if b else None, include_unsupported=True)
    if address:
        found = next((f for f in cams if f.address.lower() == address.lower()), None)
        if found is None:
            known = store.get_device(address)
            found = Found(address, (known.name if known else None) or address, None, None, "",
                          b or (known.brand if known else "sony"), dict(known.extra) if known else {})
    else:
        ranked = sorted((f for f in cams if f.supported), key=lambda f: f.pairing_mode is not True)
        if not ranked:
            unsupported = next((f for f in cams if not f.supported), None)
            if unsupported is not None:
                driver_for(unsupported.brand)(unsupported.address).require("shoot")   # raises NotSupported
            raise CameraNotFound(f"No camera in pairing mode found; {_ADVERTISE_HINT}, then start pairing on the "
                                 f"camera.")
        found = ranked[0]
    remote = create_remote(_device_from_found(found), on_prompt=on_prompt, remote_name=remote_name,
                           confirm_wait=confirm_wait)
    healed: dict[str, Any] | None = None
    try:
        try:
            result = await remote.pair()
        except PairingNotSaved:
            await remote.disconnect()
            healed = await bond.clear_stale_bond(found.name or found.address, allow_bt_restart,
                                                 on_log=remote.prompt)
            if not healed.get("cleared"):
                raise
            result = await remote.pair()
        extra = dict(result.extra)
        if healed and healed.get("mac"):
            extra["mac"] = healed["mac"]
        store.remember(remote.address, found.name, found.brand, extra=extra)
        if verify:
            result.verified = await remote.verify_pairing()
    finally:
        await remote.disconnect()
    out = result.to_dict()
    if healed is not None:
        out["stale_bond_cleared"] = healed
    return out


async def execute(cmd: str, args: dict[str, Any] | None = None, *, address: str | None = None,
                  brand: str | None = None, use_daemon: bool = True, on_prompt: Prompt | None = None,
                  timeout: float = 6.0, socket_path: str | Path | None = None) -> dict[str, Any]:
    """Run one command (see commands.COMMANDS). Uses the daemon's held link when running, else connects directly.

    The result carries "via": "daemon" | "direct".
    """
    check_args(cmd, args or {})
    if use_daemon:
        res = await send_via_daemon(cmd, args, address=address, socket_path=socket_path)
        if res is not None:
            return {**res, "via": "daemon"}
    device = await resolve_device(address, brand, timeout=timeout)
    remote = create_remote(device, on_prompt=on_prompt)
    try:
        await _connect(remote, device, timeout)
        res = await dispatch(remote, cmd, args)
    finally:
        await remote.disconnect()
    if remote.extra != device.extra:           # e.g. Nikon serial learned during the handshake
        store.remember(remote.address, device.name, device.brand, extra=remote.extra, default=False)
    return {**res, "via": "direct"}


async def unpair(address_or_name: str, *, clear_bond: bool = False, allow_bt_restart: bool = False) -> dict[str, Any]:
    """Forget a remembered camera; with clear_bond also remove the Mac's bond (see bond.clear_stale_bond)."""
    dev = store.get_device(address_or_name)
    out: dict[str, Any] = {"forgotten": store.forget(address_or_name)}
    if clear_bond:
        out["bond"] = await bond.clear_stale_bond((dev.name if dev and dev.name else address_or_name),
                                                  allow_bt_restart)
    return out


async def serve(address: str | None = None, brand: str | None = None, *, socket_path: str | Path | None = None,
                on_prompt: Prompt | None = None, keepalive_interval: float = 5.0, timeout: float = 6.0) -> None:
    """Run the persistent-link daemon until SIGINT/SIGTERM or a 'shutdown' request."""
    device = await resolve_device(address, brand, timeout=timeout)
    daemon = BleDaemon(create_remote(device, on_prompt=on_prompt), socket_path, keepalive_interval=keepalive_interval)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, daemon.request_stop)
    await daemon.serve_forever()
