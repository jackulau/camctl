"""Bond health: classify Bluetooth errors, and self-heal a stale macOS bond.

Verified on a Sony A7 V + macOS (2026-09-26):
- Writes to an encrypted characteristic fail with BleakGATTProtocolError(15, 'Insufficient Encryption') until
  macOS finishes bonding (~10 s); retrying every ~3 s succeeds.
- If the user does not press OK on the camera's pairing prompt, the camera drops the bond at disconnect and every
  later connect fails with 'CBErrorDomain Code=14 "Peer removed pairing information"'.
- The Mac's leftover bond can be removed without System Settings, but only via a Bluetooth power-cycle:
  `blueutil --power 0; sleep 3; blueutil --power 1; sleep 5; blueutil --unpair <mac>` (plain --unpair alone did
  not work). The power-cycle briefly disconnects every other Bluetooth device (AirPods, mouse), so it only runs
  when the caller passes allow_bt_restart=True.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from .errors import (BleError, BluetoothUnavailable, BondRestartRequired, CameraNotFound, ConnectFailed,
                     NotSupported, PairingNotSaved)

log = logging.getLogger("camctl.ble")

BLUEUTIL_CANDIDATES = ("/opt/homebrew/bin/blueutil", "/usr/local/bin/blueutil")
_ENCRYPTION_CODES = {0x05, 0x08, 0x0C, 0x0F}   # insufficient authentication/authorization/key size/encryption
_ENCRYPTION_TEXT = ("insufficient encryption", "insufficient authentication", "encryption is insufficient",
                    "authentication is insufficient")

Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


# -- classification ------------------------------------------------------------------

def is_peer_removed(err: BaseException | str) -> bool:
    """macOS CBError 14: the camera deleted its half of the bond, the Mac still has its half."""
    msg = str(err)
    return "Peer removed pairing information" in msg or ("CBErrorDomain" in msg and "Code=14" in msg)


def is_insufficient_encryption(err: BaseException) -> bool:
    """True while the link is not encrypted yet (bonding still in progress or never started)."""
    args = getattr(err, "args", ())
    if type(err).__name__ == "BleakGATTProtocolError" and args and isinstance(args[0], int):
        return args[0] in _ENCRYPTION_CODES
    msg = str(err).lower()
    return any(t in msg for t in _ENCRYPTION_TEXT)


def stale_bond_guidance(name: str, mac: str | None = None, blueutil: str | None = None) -> str:
    exe = blueutil or "blueutil"
    target = mac or "<address from `blueutil --paired`>"
    return (f'The Mac still holds a stale pairing for "{name}"; clear it with '
            f"`{exe} --power 0; sleep 3; {exe} --power 1; sleep 5; {exe} --unpair {target}` "
            f"(briefly disconnects other Bluetooth devices), or pass allow_bt_restart=True, or use "
            f'System Settings → Bluetooth → "{name}" → ⓘ → Forget This Device.')


def translate_error(err: BaseException, remote: Any = None) -> BaseException:
    """Map a bleak/OS error to a BleError with a one-sentence fix. BleErrors and cancellation pass through."""
    if isinstance(err, (BleError, asyncio.CancelledError, KeyboardInterrupt)):
        return err
    addr = getattr(remote, "address", None) or "the camera"
    name = getattr(remote, "name", None) or addr
    msg = str(err)
    kind = type(err).__name__
    low = msg.lower()
    if is_peer_removed(err):
        return PairingNotSaved(
            f'The camera "{name}" no longer has this Mac\'s pairing (CBError 14); clear the Mac\'s stale bond '
            f'(System Settings → Bluetooth → "{name}" → Forget This Device, or re-run pairing with '
            f"allow_bt_restart=True to do it via blueutil), then put the camera in pairing mode, pair again and "
            f"press OK on the camera.")
    if kind == "BleakDeviceNotFoundError" or ("not found" in low and "characteristic" not in low):
        return CameraNotFound(f"Camera {addr} not found; turn it on, enable its Bluetooth remote function, "
                              f"and keep it within a few metres.")
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
        return ConnectFailed(f"Timed out talking to {addr}; wake the camera (half-press the shutter), check that its "
                             f"Bluetooth remote is enabled, and retry.")
    if kind == "BleakBluetoothNotAvailableError" or "powered off" in low or "turned off" in low:
        return BluetoothUnavailable("Bluetooth is off or unavailable; turn it on and allow your terminal/app Bluetooth "
                                    "access in System Settings → Privacy & Security → Bluetooth.")
    if "not authorized" in low or "unauthorized" in low:
        return BluetoothUnavailable("This app is not allowed to use Bluetooth; enable it in System Settings → "
                                    "Privacy & Security → Bluetooth and restart the app.")
    return BleError(f"Bluetooth error with {addr}: {msg or kind}; retry, and power-cycle the camera if it persists.")


# -- macOS stale-bond self-heal ---------------------------------------------------------

_ADDR = re.compile(r"address:\s*([0-9a-f]{2}(?:[-:][0-9a-f]{2}){5})", re.I)
_NAME = re.compile(r'name:\s*"([^"]*)"')


def _norm_mac(s: str) -> str:
    return s.lower().replace(":", "-")


def parse_paired(text: str) -> list[dict[str, Any]]:
    """Parse `blueutil --paired` lines into [{address, name, connected}]."""
    out = []
    for line in text.splitlines():
        a = _ADDR.search(line)
        if not a:
            continue
        n = _NAME.search(line)
        out.append({"address": _norm_mac(a.group(1)), "name": n.group(1) if n else "",
                    "connected": ", connected" in line})
    return out


def find_paired_mac(name_or_address: str, paired_text: str) -> str | None:
    """Classic MAC of a paired device, matched by exact name (case-insensitive) or by address."""
    key = name_or_address.strip()
    for d in parse_paired(paired_text):
        if d["address"] == _norm_mac(key) or (d["name"] and d["name"].lower() == key.lower()):
            return d["address"]
    return None


def find_blueutil() -> str | None:
    return shutil.which("blueutil") or next((p for p in BLUEUTIL_CANDIDATES if Path(p).exists()), None)


async def _run(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 20)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        raise BleError(f"`{' '.join(argv)}` hung for 20 s; check Bluetooth in System Settings.") from None
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def clear_stale_bond(name_or_address: str, allow_bt_restart: bool = False, *, runner: Runner | None = None,
                           on_log: Callable[[str], None] | None = None, platform: str | None = None,
                           blueutil: str | None = None) -> dict[str, Any]:
    """Remove the Mac's leftover bond for a camera that forgot this Mac.

    Returns {"cleared": bool, "mac", "name", ...}. `cleared` False with a reason when the Mac has no such bond.
    Raises BondRestartRequired (with the exact command) unless allow_bt_restart is True, because the required
    Bluetooth power-cycle briefly disconnects the user's other Bluetooth devices.
    """
    say = on_log or log.info
    if (platform or sys.platform) != "darwin":
        raise NotSupported("Stale-bond repair is macOS-only; on Linux run `bluetoothctl remove <address>`, on Windows "
                           "remove the camera in Settings → Bluetooth.")
    exe = blueutil or find_blueutil()
    if exe is None:
        raise BondRestartRequired(f'Remove the Mac\'s stale pairing in System Settings → Bluetooth → '
                                  f'"{name_or_address}" → ⓘ → Forget This Device (or `brew install blueutil` so '
                                  f"camctl can do it).")
    run = runner or _run
    rc, out, err = await run([exe, "--paired"])
    if rc != 0:
        raise BleError(f"`{exe} --paired` failed ({err.strip() or rc}); allow your terminal Bluetooth access in "
                       f"System Settings → Privacy & Security → Bluetooth.")
    mac = find_paired_mac(name_or_address, out)
    if mac is None:
        return {"cleared": False, "mac": None, "name": name_or_address,
                "reason": f'No device named "{name_or_address}" is paired with this Mac; nothing to clear.'}
    if not allow_bt_restart:
        raise BondRestartRequired(stale_bond_guidance(name_or_address, mac, exe))
    say(f'Restarting Bluetooth to clear the stale pairing for "{name_or_address}" ({mac}); other Bluetooth devices '
        f"reconnect in a few seconds.")
    try:
        await run([exe, "--power", "0"])
        await asyncio.sleep(3)
    finally:
        try:   # never leave the user's Bluetooth off
            rc_on, _, err_on = await run([exe, "--power", "1"])
            if rc_on != 0:
                say(f"Could not turn Bluetooth back on ({err_on.strip() or rc_on}); turn it on in Control Centre.")
        except Exception as e:  # noqa: BLE001 — reported, then the original error (if any) propagates
            say(f"Could not turn Bluetooth back on ({e}); turn it on in Control Centre.")
    await asyncio.sleep(5)
    rc, _, err = await run([exe, "--unpair", mac])
    _, after, _ = await run([exe, "--paired"])
    if find_paired_mac(mac, after):
        raise BleError(f'blueutil could not remove the pairing for "{name_or_address}" ({err.strip() or rc}); use '
                       f'System Settings → Bluetooth → "{name_or_address}" → ⓘ → Forget This Device.')
    return {"cleared": True, "mac": mac, "name": name_or_address}
