import os
import sys
from pathlib import Path

# Must set offscreen platform BEFORE importing PySide6
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

# Core modules may not exist yet (other agent in flight).
# The test will fail at import time with ImportError if so — that is expected.
from ezkaraoke.config import Config
from ezkaraoke.library import Library, Song
from ezkaraoke.player import PlayerController
from ezkaraoke.player_window import PlayerWindow
from ezkaraoke.select_window import SelectWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_ui_smoke(qapp):
    # Build a temp library with 10 songs across 3 artists
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

    library = Library()
    library.load(songs)

    config = Config(music_folder="/tmp/ezkaraoke_test_music")
    controller = PlayerController()

    player_win = PlayerWindow(controller)
    select_win = SelectWindow(controller, library, config)

    player_win.show()
    select_win.show()

    # Process events to let windows initialize
    qapp.processEvents()

    # Assertions
    assert player_win.isVisible(), "Player window should be visible"
    assert select_win.isVisible(), "Select window should be visible"

    # Center table should have 10 rows
    assert select_win._song_table.rowCount() == 10, "Song table should list all 10 songs"

    # Artist tree: top item "全部歌曲" + 3 artists = 4 top-level items
    top_count = select_win._tree.topLevelItemCount()
    assert top_count == 1, "Tree should have 1 top-level item (全部歌曲)"
    all_item = select_win._tree.topLevelItem(0)
    assert all_item.childCount() == 3, "All-songs node should have 3 artist children"

    # Append a song via controller and assert queue table updates
    controller.append(songs[0])
    qapp.processEvents()
    assert select_win._queue_table.rowCount() == 1, "Queue table should show 1 row after append"

    # Close windows
    player_win.close()
    select_win.close()
    qapp.processEvents()
