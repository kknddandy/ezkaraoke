"""Tests for i18n, config language round-trip, window retranslate and fullscreen."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest
from PySide6.QtCore import QEvent, Qt, QPointF
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
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
    # Playback STATE label (player window) vs the loudness stop button key.
    assert i18n.tr("停止") == "Stopped"
    assert i18n.tr("停止测量") == "Stop measuring"
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
        assert win._song_model.headerData(0, Qt.Horizontal) == "Artist"
        assert win._song_model.headerData(2, Qt.Horizontal) == "Version"
        assert win._song_model.headerData(3, Qt.Horizontal) == "Size"
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


def test_player_window_progress_bar(qapp, monkeypatch):
    controller = PlayerController()
    win = PlayerWindow(controller)
    win.show()
    qapp.processEvents()
    try:
        # The bar must be a horizontal slider (regression: a parent-only
        # QSlider() defaults to vertical and rendered as a thin strip)
        assert win._seek.orientation() == Qt.Horizontal

        # Unknown length: slider disabled, placeholder label
        win._update_progress()
        assert not win._seek.isEnabled()
        assert win._time_label.text() == "-:-- / -:--"

        # A 60 s track at the 15 s mark
        monkeypatch.setattr(controller, "media_length_ms", lambda: 60000)
        monkeypatch.setattr(controller, "playback_position_ms", lambda: 15000)
        win._update_progress()
        assert win._seek.isEnabled()
        assert win._seek.minimum() == 0
        assert win._seek.maximum() == 60000
        assert win._seek.value() == 15000
        assert win._time_label.text() == "0:15 / 1:00"

        # Seeking from the slider reaches the controller
        seeks: list[int] = []
        monkeypatch.setattr(controller, "seek_to_ms", lambda ms: seeks.append(ms))
        win._seek.seekRequested.emit(42000)
        assert seeks == [42000]
        assert win._time_label.text() == "0:42 / 1:00"

        # Dragging updates the label live without seeking yet;
        # releasing seeks to the dragged position
        win._on_slider_moved(20000)
        assert win._time_label.text() == "0:20 / 1:00"
        assert seeks == [42000]
        win._on_slider_released(20000)
        assert seeks == [42000, 20000]

        # Unknown length again: back to the disabled placeholder
        monkeypatch.setattr(controller, "media_length_ms", lambda: -1)
        win._update_progress()
        assert not win._seek.isEnabled()
        assert win._time_label.text() == "-:-- / -:--"
        win._seek.seekRequested.emit(5000)  # must not crash
    finally:
        win.close()
        qapp.processEvents()


# --------------------------------------------------------------- mic mixer

def test_player_window_mixer_toggle(qapp, monkeypatch):
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices", lambda: []
    )
    controller = PlayerController()
    win = PlayerWindow(controller)
    win.show()
    qapp.processEvents()
    try:
        assert win._btn_mixer.text() == "混音台"
        assert win._btn_mixer.objectName() == "ToolButton"
        assert win._btn_mixer.isCheckable()
        assert win._mixer_dialog is None

        # Toggling the button builds the mixer dialog
        win._btn_mixer.click()
        qapp.processEvents()
        assert win._btn_mixer.isChecked()
        assert win._mixer_dialog is not None
        assert win._mixer_dialog.isVisible()
        assert win._mixer_dialog.windowTitle() == "混音台"

        # Toggling again closes it
        win._btn_mixer.click()
        qapp.processEvents()
        assert not win._btn_mixer.isChecked()
        assert win._mixer_dialog is None

        # Closing the dialog itself unchecks the toolbar button too
        win._btn_mixer.click()
        qapp.processEvents()
        assert win._mixer_dialog is not None
        win._mixer_dialog.close()
        qapp.processEvents()
        assert win._mixer_dialog is None
        assert not win._btn_mixer.isChecked()
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_mixer_enable_persists(qapp, monkeypatch):
    saved: list = []
    monkeypatch.setattr(
        "ezkaraoke.player_window.save_config", lambda cfg: saved.append(cfg)
    )
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices", lambda: []
    )
    calls: list = []
    config = Config(mic_enabled=False)
    controller = PlayerController()
    monkeypatch.setattr(
        controller, "set_mic_enabled", lambda on: calls.append(on)
    )
    win = PlayerWindow(controller, config=config)
    win.show()
    qapp.processEvents()
    try:
        win._btn_mixer.click()
        qapp.processEvents()
        # The enable state is initialised from the config; no mixer yet.
        assert win._mic_enable_btn.isChecked() is False
        assert win._mic_status_label.text() == "麦克风不可用"

        win._mic_enable_btn.click()
        qapp.processEvents()
        assert calls == [True]
        assert config.mic_enabled is True
        assert saved and saved[-1] is config
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_mixer_volume_slider(qapp, monkeypatch):
    saved: list = []
    monkeypatch.setattr(
        "ezkaraoke.player_window.save_config", lambda cfg: saved.append(cfg)
    )
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices", lambda: []
    )
    calls: list = []

    def fake_configure(**kwargs):
        calls.append(kwargs)

    config = Config()
    controller = PlayerController()
    monkeypatch.setattr(controller, "configure_mic", fake_configure)
    win = PlayerWindow(controller, config=config)
    win.show()
    qapp.processEvents()
    try:
        win._btn_mixer.click()
        qapp.processEvents()
        slider = win._mic_gain_slider
        assert slider.minimum() == -24
        assert slider.maximum() == 24
        assert slider.value() == 0
        assert win._mic_gain_value.text() == "+0 dB"

        slider.setValue(10)
        qapp.processEvents()
        assert calls and calls[-1]["gain_db"] == 10.0
        assert calls[-1]["enabled"] is True
        assert win._mic_gain_value.text() == "+10 dB"
        # The in-memory config updates immediately; the write is debounced
        # by the single-shot timer, so nothing is on disk yet.
        assert config.mic_gain_db == 10.0
        assert saved == []
        win._save_mic_config()
        assert saved and saved[-1] is config
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_mixer_device_combo(qapp, monkeypatch):
    monkeypatch.setattr(
        "ezkaraoke.player_window.save_config", lambda cfg: None
    )
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices",
        lambda: [(4, "USB Mic"), (7, "Webcam")],
    )
    calls: list = []

    def fake_configure(**kwargs):
        calls.append(kwargs)

    config = Config()
    controller = PlayerController()
    monkeypatch.setattr(controller, "configure_mic", fake_configure)
    win = PlayerWindow(controller, config=config)
    win.show()
    qapp.processEvents()
    try:
        win._btn_mixer.click()
        qapp.processEvents()
        combo = win._mic_device_combo
        # 系统默认 + both devices; item DATA is the device name, never
        # the index.
        assert combo.count() == 3
        assert combo.itemText(0) == "系统默认"
        assert combo.itemData(0) == ""
        assert combo.itemText(1) == "USB Mic"
        assert combo.itemData(1) == "USB Mic"
        assert combo.itemText(2) == "Webcam"
        assert combo.itemData(2) == "Webcam"

        # Selecting an item passes the device NAME to configure_mic.
        combo.setCurrentIndex(1)
        qapp.processEvents()
        assert calls and calls[-1]["device"] == "USB Mic"
        assert config.mic_device == "USB Mic"
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_mixer_status_semantics(qapp, monkeypatch):
    monkeypatch.setattr(
        "ezkaraoke.player_window.save_config", lambda cfg: None
    )
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices", lambda: []
    )
    config = Config()
    controller = PlayerController()
    win = PlayerWindow(controller, config=config)
    win.show()
    qapp.processEvents()
    try:
        win._btn_mixer.click()
        qapp.processEvents()
        # Stop the 500 ms status poller so it cannot race the assertions.
        win._mic_status_timer.stop()

        class FakeMixer:
            def __init__(self, running: bool, last_error: str | None):
                self._running = running
                self.last_error = last_error

            def is_running(self) -> bool:
                return self._running

        # No mixer at all.
        controller._mic_mixer = None
        win._update_mic_status()
        assert win._mic_status_label.text() == "麦克风不可用"

        # Enabled and playing, but the stream never started: show the
        # failure reason instead of the misleading "未启用".
        controller._mic_mixer = FakeMixer(False, "boom")
        controller._state = "playing"
        win._mic_enable_btn.setChecked(True)
        win._update_mic_status()
        assert win._mic_status_label.text() == "boom"

        # Stopped: "not running" is the normal condition again.
        controller._state = "stopped"
        win._update_mic_status()
        assert win._mic_status_label.text() == "麦克风：未启用"

        # Playing but the user turned the mic off: plain "未启用".
        controller._state = "playing"
        win._mic_enable_btn.setChecked(False)
        win._update_mic_status()
        assert win._mic_status_label.text() == "麦克风：未启用"

        # Stream running: "已启用" regardless of a latched error.
        controller._mic_mixer = FakeMixer(True, "boom")
        win._mic_enable_btn.setChecked(True)
        win._update_mic_status()
        assert win._mic_status_label.text() == "麦克风：已启用"
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_mixer_stored_device_absent(qapp, monkeypatch):
    monkeypatch.setattr(
        "ezkaraoke.player_window.save_config", lambda cfg: None
    )
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices", lambda: [(4, "USB Mic")]
    )
    config = Config(mic_device="Ghost Mic")
    controller = PlayerController()
    win = PlayerWindow(controller, config=config)
    win.show()
    qapp.processEvents()
    try:
        win._btn_mixer.click()
        qapp.processEvents()
        combo = win._mic_device_combo
        # The stored device is no longer enumerated (unplugged or renamed):
        # it must remain visible and selectable, not silently reset to
        # 系统默认.
        idx = combo.findData("Ghost Mic")
        assert idx >= 0
        assert combo.itemText(idx) == "Ghost Mic"
        assert combo.currentIndex() == idx
        assert config.mic_device == "Ghost Mic"
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_mixer_save_debounce(qapp, monkeypatch):
    saved: list = []
    monkeypatch.setattr(
        "ezkaraoke.player_window.save_config", lambda cfg: saved.append(cfg)
    )
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices", lambda: []
    )
    config = Config()
    controller = PlayerController()
    win = PlayerWindow(controller, config=config)
    win.show()
    qapp.processEvents()
    try:
        win._btn_mixer.click()
        qapp.processEvents()
        # A drag fires valueChanged many times: the in-memory config is
        # updated immediately, but the write is coalesced by the
        # single-shot debounce timer, so nothing has been written yet.
        for value in (1, 5, 10, 18):
            win._mic_gain_slider.setValue(value)
            qapp.processEvents()
        assert config.mic_gain_db == 18.0
        assert saved == []

        # The timer (or the toggle / device handlers) performs the write.
        win._save_mic_config()
        assert saved == [config]

        # The immediate save cancels the pending debounced write: letting
        # the 400 ms timer elapse must not produce a second save.
        QTest.qWait(650)
        assert saved == [config]
    finally:
        win.close()
        qapp.processEvents()


def test_player_window_mixer_without_config(qapp, monkeypatch):
    saved: list = []
    monkeypatch.setattr(
        "ezkaraoke.player_window.save_config", lambda cfg: saved.append(cfg)
    )
    monkeypatch.setattr(
        "ezkaraoke.player_window.list_input_devices", lambda: []
    )
    calls: list = []

    def fake_configure(**kwargs):
        calls.append(kwargs)

    controller = PlayerController()
    monkeypatch.setattr(controller, "configure_mic", fake_configure)
    win = PlayerWindow(controller)  # no config: panel still builds
    win.show()
    qapp.processEvents()
    try:
        win._btn_mixer.click()
        qapp.processEvents()
        assert win._mixer_dialog is not None
        assert win._mic_enable_btn.isChecked() is True  # default

        win._mic_gain_slider.setValue(5)
        qapp.processEvents()
        assert calls and calls[-1]["gain_db"] == 5.0
        assert saved == []  # persistence skipped without a config

        win._btn_mixer.click()
        qapp.processEvents()
        assert win._mixer_dialog is None
    finally:
        win.close()
        qapp.processEvents()
