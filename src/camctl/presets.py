"""Preset loading: built-ins shipped with the package + user YAML in ~/.config/camctl/presets/."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .paths import CONFIG_DIR

USER_DIR = CONFIG_DIR / "presets"


def _builtin() -> dict[str, dict[str, Any]]:
    text = resources.files("camctl").joinpath("data/builtin.yaml").read_text()
    return yaml.safe_load(text)


def load_all() -> dict[str, dict[str, Any]]:
    presets = {name: {**p, "source": "builtin"} for name, p in _builtin().items()}
    if USER_DIR.is_dir():
        for f in sorted(USER_DIR.glob("*.y*ml")):
            doc = yaml.safe_load(f.read_text()) or {}
            if "settings" in doc:                       # single preset per file, named by filename
                presets[f.stem] = {**doc, "source": str(f)}
            else:                                       # map of presets
                for name, p in doc.items():
                    presets[name] = {**p, "source": str(f)}
    return presets


def get(name: str) -> dict[str, Any]:
    presets = load_all()
    if name not in presets:
        raise KeyError(f"unknown preset {name!r}; have: {', '.join(presets)}")
    return presets[name]


def save(name: str, settings: dict[str, Any], description: str = "", manual: list[str] | None = None) -> Path:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_DIR / f"{name}.yaml"
    doc = {"description": description, "settings": settings, "manual": manual or []}
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return path
