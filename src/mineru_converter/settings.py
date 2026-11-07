from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path


_SETTING_KEYS = {"output_root", "obsidian_vault", "obsidian_target"}


def default_settings_path() -> Path:
    """Return a per-user settings path without hardcoded machine paths."""

    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "MinerU PDF to Markdown" / "settings.json"
    if sys.platform != "darwin":
        return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "mineru-pdf-to-markdown" / "settings.json"
    return Path.home() / "Library" / "Application Support" / "MinerU PDF to Markdown" / "settings.json"


def load_gui_settings(path: Path | None = None) -> dict[str, str]:
    settings_path = (path or default_settings_path()).expanduser()
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if key in _SETTING_KEYS and isinstance(value, str) and value.strip()
    }


def save_gui_settings(settings: Mapping[str, str], path: Path | None = None) -> Path:
    """Atomically save non-secret GUI preferences with private permissions."""

    settings_path = (path or default_settings_path()).expanduser()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        key: str(value)
        for key, value in settings.items()
        if key in _SETTING_KEYS and isinstance(value, str) and value.strip()
    }
    temporary = settings_path.with_name(f".{settings_path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, settings_path)
    settings_path.chmod(0o600)
    return settings_path
