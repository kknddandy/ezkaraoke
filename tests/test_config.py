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


# -------------------------------------------------------------------- mic

def test_config_mic_defaults_on_missing_file(tmp_path):
    cfg = load_config(tmp_path / "config.json")
    assert cfg.mic_enabled is True
    assert cfg.mic_gain_db == 0.0
    assert cfg.mic_echo == 0.35
    assert cfg.mic_bass_db == 0.0
    assert cfg.mic_treble_db == 0.0
    assert cfg.mic_device == ""


def test_config_mic_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    # Legacy config without the mic fields keeps the defaults.
    path.write_text(json.dumps({"music_folder": "/m"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.mic_enabled is True
    assert cfg.mic_gain_db == 0.0
    assert cfg.mic_echo == 0.35
    assert cfg.mic_bass_db == 0.0
    assert cfg.mic_treble_db == 0.0
    assert cfg.mic_device == ""
    # Custom values survive a save/load roundtrip.
    save_config(
        Config(
            music_folder="/m",
            mic_enabled=False,
            mic_gain_db=-6.5,
            mic_echo=0.8,
            mic_bass_db=3.0,
            mic_treble_db=-2.0,
            mic_device="2",
        ),
        path,
    )
    cfg = load_config(path)
    assert cfg.music_folder == "/m"
    assert cfg.mic_enabled is False
    assert cfg.mic_gain_db == -6.5
    assert cfg.mic_echo == 0.8
    assert cfg.mic_bass_db == 3.0
    assert cfg.mic_treble_db == -2.0
    assert cfg.mic_device == "2"
    # An int mic_gain_db is stored as float.
    path.write_text(json.dumps({"mic_gain_db": -6}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.mic_gain_db == -6.0
    assert isinstance(cfg.mic_gain_db, float)


def test_config_mic_invalid_values_fall_back(tmp_path):
    path = tmp_path / "config.json"
    # Wrong types (bools where a number is expected, and vice versa),
    # out-of-range and non-numeric mic_gain_db values fall back to 0.0.
    for bad in (True, False, "-6", None, [1.0], 25, -25, 24.5):
        path.write_text(
            json.dumps({"mic_enabled": False, "mic_gain_db": bad}),
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.mic_gain_db == 0.0
        assert cfg.mic_enabled is False
    # Boundaries are accepted.
    for good, expected in ((-24, -24.0), (24, 24.0)):
        path.write_text(json.dumps({"mic_gain_db": good}), encoding="utf-8")
        cfg = load_config(path)
        assert cfg.mic_gain_db == expected
    # mic_echo: bools/strings/None and out-of-range fall back to 0.35.
    for bad in (True, False, "0.5", None, -0.1, 1.1):
        path.write_text(json.dumps({"mic_echo": bad}), encoding="utf-8")
        assert load_config(path).mic_echo == 0.35
    for good, expected in ((0, 0.0), (1, 1.0)):
        path.write_text(json.dumps({"mic_echo": good}), encoding="utf-8")
        assert load_config(path).mic_echo == expected
    # mic_bass_db / mic_treble_db: fall back to 0.0, boundaries accepted.
    for bad in (True, False, "3", None, -12.5, 12.5, {"db": 1}):
        path.write_text(
            json.dumps({"mic_bass_db": bad, "mic_treble_db": bad}),
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.mic_bass_db == 0.0
        assert cfg.mic_treble_db == 0.0
    path.write_text(
        json.dumps({"mic_bass_db": -12, "mic_treble_db": 12}),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.mic_bass_db == -12.0
    assert cfg.mic_treble_db == 12.0
    # mic_enabled only accepts bool (numbers fall back to True).
    for bad in (0, 1, 1.0, "yes", None, ["on"]):
        path.write_text(json.dumps({"mic_enabled": bad}), encoding="utf-8")
        assert load_config(path).mic_enabled is True
    path.write_text(json.dumps({"mic_enabled": False}), encoding="utf-8")
    assert load_config(path).mic_enabled is False
    # mic_device only accepts str (anything else falls back to "").
    for bad in (2, ["2"], None, True, {"index": 1}):
        path.write_text(json.dumps({"mic_device": bad}), encoding="utf-8")
        assert load_config(path).mic_device == ""
    path.write_text(json.dumps({"mic_device": "HDA Intel"}), encoding="utf-8")
    assert load_config(path).mic_device == "HDA Intel"
