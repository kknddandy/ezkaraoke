"""Per-platform user directories for config, data and caches.

Linux follows the XDG base-directory spec; Windows uses the roaming
``%APPDATA%`` for config and ``%LOCALAPPDATA%`` for the library/cache;
macOS uses ``~/Library``. The functions read ``sys.platform`` and the
environment on each call, so tests can exercise every branch without
reloading modules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ezkaraoke"


def _env_path(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else fallback


def config_dir() -> Path:
    """Directory that holds the user config file."""
    if sys.platform == "win32":
        return _env_path("APPDATA", Path.home() / "AppData" / "Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _env_path("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME


def data_dir() -> Path:
    """Directory that holds the song database and other persistent data."""
    if sys.platform == "win32":
        return _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _env_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_NAME


def config_file() -> Path:
    """Full path of the JSON config file."""
    return config_dir() / "config.json"


def database_file() -> Path:
    """Full path of the SQLite song library."""
    return data_dir() / "songs.db"
