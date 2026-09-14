"""Tests for per-platform user directories and shared-library selection.

These cover the Windows/macOS support paths without needing those systems:
the helpers read ``sys.platform`` and the environment at call time, so each
branch can be exercised by monkeypatching.
"""

import sys
from pathlib import Path

from ezkaraoke import frame_bridge, paths, pitchshift, player


# --------------------------------------------------------------------- paths
def test_linux_xdg_defaults(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    home = Path.home()
    assert paths.config_dir() == home / ".config" / "ezkaraoke"
    assert paths.data_dir() == home / ".local" / "share" / "ezkaraoke"
    assert paths.config_file() == home / ".config" / "ezkaraoke" / "config.json"
    assert paths.database_file() == home / ".local" / "share" / "ezkaraoke" / "songs.db"


def test_linux_respects_xdg_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert paths.config_dir() == tmp_path / "cfg" / "ezkaraoke"
    assert paths.data_dir() == tmp_path / "data" / "ezkaraoke"


def test_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.config_dir() == tmp_path / "Roaming" / "ezkaraoke"
    assert paths.data_dir() == tmp_path / "Local" / "ezkaraoke"
    assert paths.config_file().name == "config.json"
    assert paths.database_file().name == "songs.db"


def test_windows_falls_back_without_env(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    home = Path.home()
    assert paths.config_dir() == home / "AppData" / "Roaming" / "ezkaraoke"
    assert paths.data_dir() == home / "AppData" / "Local" / "ezkaraoke"


def test_macos_uses_library(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    home = Path.home()
    assert paths.config_dir() == home / "Library" / "Application Support" / "ezkaraoke"
    assert paths.data_dir() == home / "Library" / "Application Support" / "ezkaraoke"


# -------------------------------------------------------- libvlc name lookup
def test_vlc_library_names_per_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert "libvlc.dll" in player._vlc_library_names()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert all(n.endswith(".dylib") for n in player._vlc_library_names())
    monkeypatch.setattr(sys, "platform", "linux")
    assert all(n.startswith("libvlc.so") for n in player._vlc_library_names())


# --------------------------------------------------- ffmpeg spawn behaviour
def test_spawn_kwargs_hides_console_only_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert "creationflags" in pitchshift._spawn_kwargs()
    monkeypatch.setattr(sys, "platform", "linux")
    assert pitchshift._spawn_kwargs() == {}
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pitchshift._spawn_kwargs() == {}


# ------------------------------------------------------- video output mode
def test_video_output_mode():
    assert frame_bridge.video_output_mode("windows") == "hwnd"
    assert frame_bridge.video_output_mode("xcb") == "hwnd"
    assert frame_bridge.video_output_mode("wayland") == "software"
