"""BLE exceptions. Every message is one sentence that says what went wrong and how to fix it.

All derive from BleError(RuntimeError), so callers that caught RuntimeError from the old single-module
`camctl.ble` keep working.
"""

from __future__ import annotations


class BleError(RuntimeError):
    """Base class for Bluetooth remote errors."""


class NotSupported(BleError):
    """The camera (or its brand's BLE protocol) cannot do this."""


class CameraNotFound(BleError):
    """Nothing matching is advertising / reachable."""


class ConnectFailed(BleError):
    """The link could not be opened or the camera lacks an expected service."""


class BluetoothUnavailable(BleError):
    """Bluetooth is off, missing, or the app lacks permission."""


class PairingRequired(BleError):
    """The camera refuses commands because this computer is not (or no longer) bonded/registered."""


class PairingFailed(BleError):
    """A brand pairing handshake did not complete."""


class PairingNotSaved(BleError):
    """The camera discarded the bond (macOS CBError 14 'Peer removed pairing information')."""


class BondRestartRequired(BleError):
    """Clearing a stale macOS bond needs a Bluetooth power-cycle the caller has not allowed."""


class DaemonError(BleError):
    """The persistent-link daemon misbehaved."""


ERRORS: dict[str, type[Exception]] = {c.__name__: c for c in (
    BleError, NotSupported, CameraNotFound, ConnectFailed, BluetoothUnavailable, PairingRequired, PairingFailed,
    PairingNotSaved, BondRestartRequired, DaemonError, ValueError)}
