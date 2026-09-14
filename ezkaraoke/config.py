"""Application configuration (JSON file in the user config dir)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ezkaraoke import paths

DEFAULT_CONFIG_PATH = paths.config_file()


@dataclass
class Config:
    music_folder: str = ""
    db_path: str = ""
    language: str = "zh"
    web_port: int = 8848


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
    db_path = data.get("db_path", "")
    if not isinstance(db_path, str):
        db_path = ""
    language = data.get("language", "zh")
    if language not in ("zh", "en"):
        language = "zh"
    web_port = data.get("web_port", 8848)
    if (
        not isinstance(web_port, int)
        or isinstance(web_port, bool)
        or not 1 <= web_port <= 65535
    ):
        web_port = 8848
    return Config(
        music_folder=music_folder,
        db_path=db_path,
        language=language,
        web_port=web_port,
    )


def save_config(cfg: Config, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Save *cfg* as UTF-8 JSON, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )
