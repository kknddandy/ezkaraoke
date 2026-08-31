"""Application configuration (JSON file in the user config dir)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ezkaraoke" / "config.json"


@dataclass
class Config:
    music_folder: str = ""


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load config from *path*. Missing or invalid file returns a default Config."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Config()
    if not isinstance(data, dict):
        return Config()
    music_folder = data.get("music_folder", "")
    if not isinstance(music_folder, str):
        music_folder = ""
    return Config(music_folder=music_folder)


def save_config(cfg: Config, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Save *cfg* as UTF-8 JSON, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )
