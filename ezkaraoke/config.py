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
    loudness_enabled: bool = True
    loudness_target: float = -11.25      # allowed -30 .. -5
    loudness_workers: int = 8            # allowed 0 .. 16; 0 = auto (cpu/2)
    mic_enabled: bool = True
    mic_gain_db: float = 0.0             # allowed -24 .. 24
    mic_echo: float = 0.35               # allowed 0.0 .. 1.0
    mic_bass_db: float = 0.0             # allowed -12 .. 12
    mic_treble_db: float = 0.0           # allowed -12 .. 12
    mic_device: str = ""                 # PortAudio input index/name; "" = system default


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
    loudness_enabled = data.get("loudness_enabled", True)
    if not isinstance(loudness_enabled, bool):
        loudness_enabled = True
    loudness_target = data.get("loudness_target", -11.25)
    if (
        isinstance(loudness_target, bool)
        or not isinstance(loudness_target, (int, float))
        or not -30 <= loudness_target <= -5
    ):
        loudness_target = -11.25
    else:
        loudness_target = float(loudness_target)
    loudness_workers = data.get("loudness_workers", 8)
    if (
        not isinstance(loudness_workers, int)
        or isinstance(loudness_workers, bool)
        or not 0 <= loudness_workers <= 16
    ):
        loudness_workers = 8
    mic_enabled = data.get("mic_enabled", True)
    if not isinstance(mic_enabled, bool):
        mic_enabled = True
    mic_gain_db = data.get("mic_gain_db", 0.0)
    if (
        isinstance(mic_gain_db, bool)
        or not isinstance(mic_gain_db, (int, float))
        or not -24 <= mic_gain_db <= 24
    ):
        mic_gain_db = 0.0
    else:
        mic_gain_db = float(mic_gain_db)
    mic_echo = data.get("mic_echo", 0.35)
    if (
        isinstance(mic_echo, bool)
        or not isinstance(mic_echo, (int, float))
        or not 0.0 <= mic_echo <= 1.0
    ):
        mic_echo = 0.35
    else:
        mic_echo = float(mic_echo)
    mic_bass_db = data.get("mic_bass_db", 0.0)
    if (
        isinstance(mic_bass_db, bool)
        or not isinstance(mic_bass_db, (int, float))
        or not -12 <= mic_bass_db <= 12
    ):
        mic_bass_db = 0.0
    else:
        mic_bass_db = float(mic_bass_db)
    mic_treble_db = data.get("mic_treble_db", 0.0)
    if (
        isinstance(mic_treble_db, bool)
        or not isinstance(mic_treble_db, (int, float))
        or not -12 <= mic_treble_db <= 12
    ):
        mic_treble_db = 0.0
    else:
        mic_treble_db = float(mic_treble_db)
    mic_device = data.get("mic_device", "")
    if not isinstance(mic_device, str):
        mic_device = ""
    return Config(
        music_folder=music_folder,
        db_path=db_path,
        language=language,
        web_port=web_port,
        loudness_enabled=loudness_enabled,
        loudness_target=loudness_target,
        loudness_workers=loudness_workers,
        mic_enabled=mic_enabled,
        mic_gain_db=mic_gain_db,
        mic_echo=mic_echo,
        mic_bass_db=mic_bass_db,
        mic_treble_db=mic_treble_db,
        mic_device=mic_device,
    )


def save_config(cfg: Config, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Save *cfg* as UTF-8 JSON, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )
