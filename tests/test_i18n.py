"""Tests for i18n, config language round-trip, window retranslate and fullscreen."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest
from PySide6.QtCore import QEvent, Qt, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from ezkaraoke import i18n
from ezkaraoke.config import Config, load_config, save_config
from ezkaraoke.database import SongDatabase
from ezkaraoke.library import Song
from ezkaraoke.player import PlayerController
from ezkaraoke.player_window import PlayerWindow
from ezkaraoke.select_window import SelectWindow

SONGS = [
    Song("周杰伦", "晴天", "/music/周杰伦-晴天.mp4"),
    Song("周杰伦", "七里香", "/music/周杰伦-七里香.mp4"),
    Song("邓紫棋", "光年之外", "/music/邓紫棋-光年之外.mp4"),
]


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def reset_language():
    i18n.set_language(i18n.DEFAULT_LANGUAGE, notify=False)
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE, notify=False)


# --------------------------------------------------------------------- tr()

def test_tr_identity_in_zh():
    assert i18n.current_language() == "zh"
    assert i18n.tr("点歌") == "点歌"
    assert i18n.tr("共 {count} 首", count=3) == "共 3 首"


def test_tr_english_table():
    i18n.set_language("en", notify=False)
    assert i18n.tr("点歌") == "Queue"
    assert i18n.tr("共 {count} 首", count=3) == "3 songs"
    assert i18n.tr("升{semis}", semis=2) == "+2"
    # Unknown key (song data) passes through untouched.
    assert i18n.tr("晴天") == "晴天"
    # Key with an unfilled placeholder passes through untouched.
    assert i18n.tr("需要 {missing}") == "需要 {missing}"


def test_set_language_fallback_and_notification():
    seen = []

    def cb():
        seen.append(i18n.current_language())

    i18n.on_language_changed(cb)
    try:
        i18n.set_language("xx")  # invalid -> zh; unchanged -> no notification
        assert i18n.current_language() == "zh"
        assert seen == []
        i18n.set_language("en")
        assert i18n.current_language() == "en"
        assert seen == ["en"]
        i18n.set_language("en")  # unchanged -> no repeat notification
        assert seen == ["en"]
    finally:
        i18n.off_language_changed(cb)


# ------------------------------------------------------------------- config

def test_config_language_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(music_folder="/m", language="en"), path)
    assert load_config(path).language == "en"
    # Legacy config without the language field keeps the default.
    path.write_text(json.dumps({"music_folder": "/m"}), encoding="utf-8")
    assert load_config(path).language == "zh"
    # Invalid values fall back to zh.
    path.write_text(json.dumps({"language": "fr"}), encoding="utf-8")
    assert load_config(path).language == "zh"


def test_config_web_port_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    # Legacy config without web_port keeps the default.
    path.write_text(json.dumps({"music_folder": "/m"}), encoding="utf-8")
    assert load_config(path).web_port == 8848
    save_config(Config(music_folder="/m", web_port=9000), path)
    assert load_config(path).web_port == 9000
    # Invalid values fall back to the default.
    for bad in (0, -1, 65536, "8848", None, True):
        path.write_text(
            json.dumps({"music_folder": "/m", "web_port": bad}), encoding="utf-8"
        )
        assert load_config(path).web_port == 8848


# ------------------------------------------------------------- select window

def test_select_window_retranslate(qapp, tmp_path, monkeypatch):
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(SONGS)
    for name in db.artists():
        db.mark_avatar_tried(name)
    saved = []
    monkeypatch.setattr(
        "ezkaraoke.select_window.save_config", lambda cfg: saved.append(cfg)
    )
    config = Config(music_folder="", web_port=0)
    controller = PlayerController()
    win = SelectWindow(controller, db, config)
    win.show()
    qapp.processEvents()
    try:
        # Default zh state.
        assert win._btn_append.text() == "点歌"
        assert win._artist_list.item(0).text() == "全部 (3)"
        assert win._btn_lang.text() == "EN"

        win._toggle_language()
        qapp.processEvents()
        assert i18n.current_language() == "en"
        assert config.language == "en"
        assert saved and saved[-1].language == "en"
        assert win.windowTitle() == "ezkaraoke · Song Selector"
        assert win._btn_append.text() == "Queue"
        assert win._song_table.horizontalHeaderItem(0).text() == "Artist"
        assert win._song_table.horizontalHeaderItem(2).text() == "Size"
        assert win._artist_list.item(0).text() == "All (3)"
        assert win._btn_lang.text() == "中文"

        win._toggle_language()
        qapp.processEvents()
        assert i18n.current_language() == "zh"
        assert win._btn_append.text() == "点歌"
        assert win._artist_list.item(0).text() == "全部 (3)"
    finally:
        win.close()
        qapp.processEvents()
        db.close()


# ------------------------------------------------------------- player window

def test_player_window_fullscreen(qapp):
    controller = PlayerController()
    win = PlayerWindow(controller)
    win.show()
    qapp.processEvents()
    try:
        assert not win.isFullScreen()
        assert win._main_layout.contentsMargins().left() == 12
        assert win._btn_fullscreen.text() == "全屏"

        win.toggle_fullscreen()
        qapp.processEvents()
        assert win.isFullScreen()
        assert win._main_layout.contentsMargins().left() == 0
        assert win._control_bar.isVisible()
        assert win._btn_fullscreen.text() == "退出全屏"

        win._exit_fullscreen()
        qapp.processEvents()
        assert not win.isFullScreen()
        assert win._main_layout.contentsMargins().left() == 12
        assert win._control_bar.isVisible()

        # F11 toggles, Esc exits.
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F11, Qt.NoModifier))
        assert win.isFullScreen()
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        assert not win.isFullScreen()

        # Double-clicking the video area toggles fullscreen too.
        dbl = QMouseEvent(
            QEvent.MouseButtonDblClick,
            QPointF(10, 10),
            QPointF(10, 10),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        win._video_area.mouseDoubleClickEvent(dbl)
        assert win.isFullScreen()
        win._exit_fullscreen()
        qapp.processEvents()
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_retranslate(qapp):
    controller = PlayerController()
    win = PlayerWindow(controller)
    win.show()
    qapp.processEvents()
    try:
        win._on_pitch(2)
        assert win._pitch_label.text() == "升2"

        i18n.set_language("en")  # notifies the retranslate listener
        qapp.processEvents()
        assert win.windowTitle() == "ezkaraoke · Player"
        assert win._btn_next.text() == "Next"
        assert win._btn_fullscreen.text() == "Fullscreen"
        assert win._pitch_label.text() == "+2"

        i18n.set_language("zh")
        qapp.processEvents()
        assert win.windowTitle() == "ezkaraoke · 播放器"
        assert win._btn_next.text() == "下一首"
        assert win._pitch_label.text() == "升2"
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_replay_button(qapp):
    controller = PlayerController()
    win = PlayerWindow(controller)
    win.show()
    qapp.processEvents()
    try:
        assert win._btn_replay.text() == "重播"
        # replay restarts the current song
        controller.append(Song("某人", "某歌", "/music/某人-某歌.mp4"))
        controller.play_at(0)
        win._btn_replay.click()
        qapp.processEvents()
        assert controller.is_playing
        assert controller.current_index == 0
        # replay with nothing playing is a no-op
        controller.stop()
        win._btn_replay.click()
        assert controller.current_index == -1
        # retranslates with the language
        i18n.set_language("en")
        qapp.processEvents()
        assert win._btn_replay.text() == "Replay"
    finally:
        win.close()
        qapp.processEvents()
