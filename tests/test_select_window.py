"""Tests for SelectWindow against the SQLite-backed database (offscreen)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ezkaraoke import avatar as avatar_mod
from ezkaraoke.config import Config
from ezkaraoke.database import SongDatabase
from ezkaraoke.library import Song
from ezkaraoke.player import PlayerController
from ezkaraoke.select_window import SelectWindow

SONGS = [
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


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def win(qapp, tmp_path):
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(SONGS)
    # Pre-mark avatar attempts so __init__ never starts a live network worker.
    for name in db.artists():
        db.mark_avatar_tried(name)
    controller = PlayerController()
    window = SelectWindow(controller, db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    yield window
    window.close()
    qapp.processEvents()
    db.close()


def test_default_state(win):
    assert win._mode == "artist"
    assert win._btn_mode_artist.isChecked()
    assert win._artist_list.isVisible()
    assert not win._letter_list.isVisible()
    assert win._artist_list.count() == 4  # 全部 + 3 artists
    assert win._artist_list.item(0).text().startswith("全部 (10)")
    assert win._song_table.rowCount() == 10
    for i in range(win._artist_list.count()):
        icon = win._artist_list.item(i).icon()
        assert icon is not None and not icon.isNull()  # placeholder pixmaps


def test_select_artist_filters_table(win):
    item = win._artist_list.item(1)  # artists are sorted; 周杰伦 first after 全部
    assert item.data(Qt.UserRole) == "周杰伦"
    win._artist_list.setCurrentItem(item)
    assert win._current_artist == "周杰伦"
    assert win._song_table.rowCount() == 4
    win._artist_list.setCurrentItem(win._artist_list.item(0))  # back to 全部
    assert win._song_table.rowCount() == 10


def test_letter_mode(win):
    win._btn_mode_letter.click()
    assert win._mode == "letter"
    assert win._letter_list.isVisible()
    assert not win._artist_list.isVisible()
    assert win._letter_list.count() == 28  # 全部 + 26 letters + '#'
    # zero-count letters are disabled (QListWidgetItem: flag-based)
    def enabled(item):
        return bool(item.flags() & Qt.ItemIsEnabled)

    disabled = [win._letter_list.item(i) for i in range(win._letter_list.count())
                if not enabled(win._letter_list.item(i))]
    assert any(i.data(Qt.UserRole) == "B" for i in disabled)
    # enabled entries are exactly those with songs
    enabled_letters = [win._letter_list.item(i).data(Qt.UserRole)
                       for i in range(win._letter_list.count())
                       if enabled(win._letter_list.item(i))]
    assert enabled_letters == [None, "D", "G", "J", "K", "P", "Q", "X", "Y"]


def test_select_letter_filters_table(win):
    win._btn_mode_letter.click()
    for i in range(win._letter_list.count()):
        if win._letter_list.item(i).data(Qt.UserRole) == "Q":
            win._letter_list.setCurrentItem(win._letter_list.item(i))
    assert win._current_letter == "Q"
    assert win._song_table.rowCount() == 2  # 晴天 + 七里香


def test_search_overrides_mode(win):
    win._btn_mode_letter.click()
    win._search_edit.setText("林")
    assert win._song_table.rowCount() == 3  # 林俊杰's songs regardless of mode
    win._search_edit.clear()
    # back to letter mode with no letter selected -> all songs
    assert win._song_table.rowCount() == 10


def test_rebuild_updates_lists_and_table(win, qapp, monkeypatch):
    # Avatar fetch is stubbed out (no network); the worker still runs through
    # the real signal flow and finishes quickly.
    monkeypatch.setattr(avatar_mod, "fetch_artist_avatar", lambda name: None)
    new_songs = [Song("张学友", "吻别", "/music/张学友-吻别.mp4")]
    win._on_scan_finished(new_songs)
    assert win._artist_list.count() == 2  # 全部 + 张学友
    assert win._artist_list.item(1).text() == "张学友 (1)"
    assert win._song_table.rowCount() == 1
    assert win._db.song_count() == 1
    # wait for the avatar worker to finish (no network behind the stub)
    worker = win._avatar_worker
    if worker is not None:
        assert worker.wait(5000)
        for _ in range(100):
            qapp.processEvents()
    assert "1 首" in win._status_left.text()


def test_avatar_fetched_persists_and_marks_tried(win):
    payload = b"\xff\xd8\xff\xe0" + b"J" * 200
    win._on_avatar_fetched("周杰伦", payload)
    assert win._db.get_avatar("周杰伦") == payload
    icon = None
    for i in range(win._artist_list.count()):
        if win._artist_list.item(i).data(Qt.UserRole) == "周杰伦":
            icon = win._artist_list.item(i).icon()
    assert icon is not None and not icon.isNull()

    win._on_avatar_fetched("邓紫棋", None)
    assert "邓紫棋" not in win._db.artists_without_avatar()
    assert win._db.get_avatar("邓紫棋") is None


def test_table_has_two_columns_no_filename(win):
    assert win._song_table.columnCount() == 2
    assert win._song_table.horizontalHeaderItem(0).text() == "歌手"
    assert win._song_table.horizontalHeaderItem(1).text() == "歌名"
    # sorting is manual (pinyin-aware); Qt's built-in sort is not used
    assert not win._song_table.isSortingEnabled()
    assert not win._song_table.horizontalHeader().isSortIndicatorShown()


def test_header_click_sorts_artist_by_pinyin(win):
    win._on_header_clicked(0)
    artists = [win._song_table.item(i, 0).text() for i in range(win._song_table.rowCount())]
    # pinyin order: deng < lin < zhou (codepoint order would be zhou < lin < deng)
    assert artists == ["邓紫棋"] * 3 + ["林俊杰"] * 3 + ["周杰伦"] * 4
    assert win._song_table.horizontalHeader().isSortIndicatorShown()
    # clicking the same header again reverses the order
    win._on_header_clicked(0)
    artists = [win._song_table.item(i, 0).text() for i in range(win._song_table.rowCount())]
    assert artists[0] == "周杰伦"
    assert artists[-1] == "邓紫棋"


def test_header_click_sorts_title_by_pinyin(win):
    win._on_header_clicked(1)
    titles = [win._song_table.item(i, 1).text() for i in range(win._song_table.rowCount())]
    assert titles == [
        "倒数", "稻香", "光年之外", "江南", "可惜没如果",
        "泡沫", "七里香", "晴天", "修炼爱情", "夜曲",
    ]


def test_selection_follows_sorted_rows(win):
    win._on_header_clicked(1)
    win._song_table.selectRow(0)  # 倒数 (邓紫棋) after title sort
    songs = win._selected_songs()
    assert songs == [Song("邓紫棋", "倒数", "/music/邓紫棋-倒数.mp4")]


def test_mode_switch_stays_fast_on_large_library(qapp, tmp_path):
    # Regression: ResizeToContents header made each setItem re-measure the
    # column -> O(n^2). Mode switching with thousands of songs froze the UI.
    db = SongDatabase(tmp_path / "big.db")
    artists = [f"歌手{chr(0x4E00 + i)}" for i in range(120)]
    songs = [
        Song(artists[i % len(artists)], f"歌曲{i:04d}",
             f"/m/{artists[i % len(artists)]}-歌曲{i:04d}.mp4")
        for i in range(1500)
    ]
    db.rebuild(songs)
    for name in artists:
        db.mark_avatar_tried(name)
    controller = PlayerController()
    window = SelectWindow(controller, db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    t0 = time.monotonic()
    window._btn_mode_letter.click()
    qapp.processEvents()
    dt = time.monotonic() - t0
    window.close()
    qapp.processEvents()
    db.close()
    assert dt < 0.5, f"mode switch took {dt:.2f}s (must stay under 0.5s)"


def test_close_is_fast_with_running_avatar_worker(qapp, tmp_path, monkeypatch):
    # Regression: closeEvent used to wait out the avatar worker (up to
    # minutes with a slow network), making the window appear frozen.
    def fake_fetch(name):
        time.sleep(1.0)
        return b"\xff\xd8\xff" + name.encode() * 40

    monkeypatch.setattr(avatar_mod, "fetch_artist_avatar", fake_fetch)
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(SONGS)  # not pre-marked -> __init__ starts the avatar worker
    controller = PlayerController()
    window = SelectWindow(controller, db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    assert window._avatar_worker is not None
    t0 = time.monotonic()
    window.close()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, f"close blocked {elapsed:.2f}s on the avatar worker"
    window._avatar_worker.wait(15000)  # let the daemon finish for clean teardown
    db.close()


def test_avatar_progress_bar_states(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        avatar_mod, "fetch_artist_avatar",
        lambda name: b"\xff\xd8\xff" + name.encode() * 40,
    )
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(SONGS)  # not pre-marked -> worker starts in __init__
    controller = PlayerController()
    window = SelectWindow(controller, db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    seen = []
    deadline = time.time() + 15
    while window._avatar_worker is not None and time.time() < deadline:
        if window._progress.isVisible():
            seen.append(window._progress.format())
        qapp.processEvents()
        time.sleep(0.01)
    assert seen, "progress bar never showed while avatars were fetching"
    assert any(f.startswith("正在获取歌手头像") for f in seen)
    assert not window._progress.isVisible()  # hidden once done
    assert window._db.get_avatar("周杰伦") is not None
    window.close()
    qapp.processEvents()
    db.close()
