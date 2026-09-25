import os
import sys

# Must set offscreen platform BEFORE importing PySide6
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QPushButton

from ezkaraoke import select_window as select_window_module
from ezkaraoke.config import Config
from ezkaraoke.database import SongDatabase
from ezkaraoke.library import Song
from ezkaraoke.player import PlayerController
from ezkaraoke.player_window import PlayerWindow
from ezkaraoke.select_window import SelectWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_ui_smoke(qapp, tmp_path):
    # Build a temp song database with 10 songs across 3 artists
    songs = [
        Song("周杰伦", "晴天", "/music/周杰伦-晴天.mp4"),
        Song("周杰伦", "七里香", "/music/周杰伦-七里香.mp4"),
        Song("周杰伦", "稻香", "/music/周杰伦-稻香.mp4"),
        Song("周杰伦", "夜曲", "/music/周杰伦-夜曲.mp4"),
        Song("邓紫棋", "光年之外", "/music/邓紫棋-光年之外.mp4"),
        Song("邓紫棋", "倒数", "/music/邓紫棋-倒数.mp4"),
        Song("邓紫棋", "泡沫", "/music/邓紫棋-泡沫.mp4"),
        Song("林俊杰", "江南", "/music/林俊杰-江南.mp4"),
        Song("林俊杰", "可惜没如果", "/music/林俊杰-可惜没如果.mp4"),
        Song("林俊杰", "修炼爱情", "/music/林俊杰-修炼爱情.mp4"),
    ]

    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(songs)
    # Pre-mark avatar attempts so no live network worker starts.
    for name in db.artists():
        db.mark_avatar_tried(name)

    config = Config(music_folder="", web_port=0)
    controller = PlayerController()

    player_win = PlayerWindow(controller)
    select_win = SelectWindow(controller, db, config)

    player_win.show()
    select_win.show()

    # Process events to let windows initialize
    qapp.processEvents()

    # Assertions
    assert player_win.isVisible(), "Player window should be visible"
    assert select_win.isVisible(), "Select window should be visible"

    # Center table should have 10 rows
    assert select_win._song_model.rowCount() == 10, "Song table should list all 10 songs"

    # Artist list: "全部" + 3 artists = 4 items
    assert select_win._artist_list.count() == 4, "Artist list should have 4 items"
    assert select_win._artist_list.item(0).text().startswith("全部 (10)")

    # Append a song via controller and assert queue table updates
    controller.append(songs[0])
    qapp.processEvents()
    assert select_win._queue_table.rowCount() == 1, "Queue table should show 1 row after append"

    # Close windows
    player_win.close()
    select_win.close()
    qapp.processEvents()
    db.close()


def test_loudness_panel(qapp, tmp_path, monkeypatch):
    songs = [Song("周杰伦", "晴天", "/music/周杰伦-晴天.mp4")]

    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(songs)
    # Pre-mark avatar attempts so no live network worker starts.
    for name in db.artists():
        db.mark_avatar_tried(name)

    config = Config(music_folder="", web_port=0)
    controller = PlayerController()
    win = SelectWindow(controller, db, config)
    win.show()
    qapp.processEvents()

    # The toolbar carries the 响度对齐 button (dark-theme ToolButton).
    assert win._btn_loudness is not None
    assert win._btn_loudness.objectName() == "ToolButton"

    created = []

    class FakeLoudnessWorker(QObject):
        progress = Signal(int, int)
        finished = Signal()
        error = Signal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.start_calls = 0
            self.stop_calls = 0
            self.wait_calls = 0
            created.append(self)

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

        def wait(self, ms: int = 5000):
            self.wait_calls += 1
            return True

        def isRunning(self):
            return False

    monkeypatch.setattr(select_window_module, "LoudnessWorker", FakeLoudnessWorker)

    try:
        win._open_loudness_panel()
        qapp.processEvents()
        dlg = win._loudness_dialog
        assert dlg is not None

        # Invoke the start action.
        start_btns = [
            b for b in dlg.findChildren(QPushButton) if b.text() == "开始测量"
        ]
        assert len(start_btns) == 1, "Panel should offer exactly one start button"
        # The stop button has its own key (the bare 停止 key is the player's
        # playback state label, not this button).
        stop_btns = [
            b for b in dlg.findChildren(QPushButton) if b.text() == "停止测量"
        ]
        assert len(stop_btns) == 1, "Panel should offer exactly one stop button"
        start_btns[0].click()
        qapp.processEvents()

        assert len(created) == 1, "Start should create exactly one loudness worker"
        assert created[0].start_calls == 1, "Start should start the worker"
        assert win._loudness_worker is created[0]

        # Closing the window must stop AND wait for the worker.
        win.close()
        qapp.processEvents()
        assert created[0].stop_calls == 1, "closeEvent should stop the worker"
        assert created[0].wait_calls == 1, "closeEvent should wait for the worker"
        assert not created[0].isRunning(), "no lingering loudness worker"
    finally:
        qapp.processEvents()
        db.close()


def test_loudness_finished_does_not_clobber_new_worker(qapp, tmp_path, monkeypatch):
    songs = [Song("周杰伦", "晴天", "/music/周杰伦-晴天.mp4")]

    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(songs)
    # Pre-mark avatar attempts so no live network worker starts.
    for name in db.artists():
        db.mark_avatar_tried(name)

    config = Config(music_folder="", web_port=0)
    controller = PlayerController()
    win = SelectWindow(controller, db, config)
    win.show()
    qapp.processEvents()

    created = []

    class FakeLoudnessWorker(QObject):
        progress = Signal(int, int)
        finished = Signal()
        error = Signal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.start_calls = 0
            self.stop_calls = 0
            self.wait_calls = 0
            created.append(self)

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

        def wait(self, ms: int = 5000):
            self.wait_calls += 1
            return True

        def isRunning(self):
            return False

    monkeypatch.setattr(select_window_module, "LoudnessWorker", FakeLoudnessWorker)

    try:
        win._open_loudness_panel()
        qapp.processEvents()
        dlg = win._loudness_dialog
        assert dlg is not None

        # Start worker A through the panel.
        start_btns = [
            b for b in dlg.findChildren(QPushButton) if b.text() == "开始测量"
        ]
        assert len(start_btns) == 1
        start_btns[0].click()
        qapp.processEvents()
        worker_a = created[0]
        assert win._loudness_worker is worker_a
        assert win._progress.isVisible(), "progress bar shown while measuring"

        # A newer worker B becomes the tracked one while A's queued
        # finished signal is still pending.
        worker_b = FakeLoudnessWorker(str(db.path))
        # Wire B the way _start_worker does (identity-bound slot).
        worker_b.finished.connect(
            lambda w=worker_b: win._on_loudness_finished(w)
        )
        win._loudness_worker = worker_b

        # A's stale finished must NOT clear B's reference or idle the
        # progress bar.
        worker_a.finished.emit()
        qapp.processEvents()
        assert win._loudness_worker is worker_b
        assert win._progress.isVisible(), (
            "stale worker's finished must not idle the progress bar"
        )

        # ...while the tracked worker's own finished still resets everything.
        worker_b.finished.emit()
        qapp.processEvents()
        assert win._loudness_worker is None
        assert not win._progress.isVisible()
    finally:
        win.close()
        qapp.processEvents()
        db.close()
