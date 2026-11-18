"""Config location and the one-time migration from the pre-rename name (a7ctl)."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "camctl"
_LEGACY_DIR = Path.home() / ".config" / "a7ctl"


def _migrate() -> None:
    # Old A7CTL_* variables keep working unless the CAMCTL_* one is set.
    for key, value in list(os.environ.items()):
        if key.startswith("A7CTL_"):
            os.environ.setdefault("CAMCTL_" + key[6:], value)
    # Move ~/.config/a7ctl (backups, presets, logs, Drive token, BLE pairing) once; leave a symlink behind.
    try:
        if _LEGACY_DIR.is_dir() and not _LEGACY_DIR.is_symlink() and not CONFIG_DIR.exists():
            _LEGACY_DIR.rename(CONFIG_DIR)
            _LEGACY_DIR.symlink_to(CONFIG_DIR)
    except OSError:
        pass  # read-only home or a race with another process: both paths still resolve


_migrate()
