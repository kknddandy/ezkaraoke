"""Tests for loudness config fields: defaults, round-trip and validation fallback."""

import json

from ezkaraoke.config import Config, load_config, save_config


# ------------------------------------------------------------------- config

def test_config_defaults_on_missing_file(tmp_path):
    cfg = load_config(tmp_path / "config.json")
    assert cfg.music_folder == ""
    assert cfg.db_path == ""
    assert cfg.language == "zh"
    assert cfg.web_port == 8848
    assert cfg.loudness_enabled is True
    assert cfg.loudness_target == -11.25
    assert cfg.loudness_workers == 8


def test_config_loudness_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    # Legacy config without the loudness fields keeps the defaults.
    path.write_text(json.dumps({"music_folder": "/m"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.loudness_enabled is True
    assert cfg.loudness_target == -11.25
    assert cfg.loudness_workers == 8
    # Custom values survive a save/load roundtrip.
    save_config(
        Config(
            music_folder="/m",
            web_port=9000,
            loudness_enabled=False,
            loudness_target=-16.0,
            loudness_workers=2,
        ),
        path,
    )
    cfg = load_config(path)
    assert cfg.music_folder == "/m"
    assert cfg.web_port == 9000
    assert cfg.loudness_enabled is False
    assert cfg.loudness_target == -16.0
    assert cfg.loudness_workers == 2
    # An int loudness_target is stored as float.
    path.write_text(
        json.dumps({"music_folder": "/m", "loudness_target": -18}), encoding="utf-8"
    )
    cfg = load_config(path)
    assert cfg.loudness_target == -18.0
    assert isinstance(cfg.loudness_target, float)


def test_config_loudness_invalid_values_fall_back(tmp_path):
    path = tmp_path / "config.json"
    # Wrong types for loudness_enabled (numbers where a bool is expected)
    # fall back while the other fields still load.
    for bad in (0, 1, 1.0, "yes", None, [True]):
        path.write_text(
            json.dumps(
                {
                    "music_folder": "/m",
                    "loudness_enabled": bad,
                    "loudness_target": -20.0,
                    "loudness_workers": 4,
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.loudness_enabled is True
        assert cfg.loudness_target == -20.0
        assert cfg.loudness_workers == 4
        assert cfg.music_folder == "/m"
    # Wrong types (bools where a number is expected) and out-of-range
    # targets fall back to -11.25.
    for bad in (True, False, -30.1, -4.9, 1.5, "-18", None):
        path.write_text(
            json.dumps(
                {
                    "music_folder": "/m",
                    "loudness_enabled": False,
                    "loudness_target": bad,
                    "loudness_workers": 0,
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.loudness_target == -11.25
        assert cfg.loudness_enabled is False
        assert cfg.loudness_workers == 0
    # Wrong types (bools where an int is expected) and out-of-range worker
    # counts fall back to 8.
    for bad in (True, False, -1, 17, 2.5, "8", None):
        path.write_text(
            json.dumps(
                {
                    "music_folder": "/m",
                    "loudness_enabled": False,
                    "loudness_target": -9.5,
                    "loudness_workers": bad,
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.loudness_workers == 8
        assert cfg.loudness_enabled is False
        assert cfg.loudness_target == -9.5
    # Boundary values are accepted.
    path.write_text(
        json.dumps(
            {
                "music_folder": "/m",
                "loudness_enabled": False,
                "loudness_target": -30,
                "loudness_workers": 16,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.loudness_target == -30.0
    assert cfg.loudness_workers == 16
    assert cfg.loudness_enabled is False
    # A second boundary: target -5 with workers 0 (auto).
    path.write_text(
        json.dumps({"loudness_target": -5, "loudness_workers": 0}),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.loudness_target == -5.0
    assert cfg.loudness_workers == 0
