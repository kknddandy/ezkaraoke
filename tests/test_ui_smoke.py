import os
import sys

# Must set offscreen platform BEFORE importing PySide6
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtWidgets import QApplication

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

    config = Config(music_folder="")
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
    assert select_win._song_table.rowCount() == 10, "Song table should list all 10 songs"

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
