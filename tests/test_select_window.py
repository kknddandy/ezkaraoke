"""Tests for SelectWindow against the SQLite-backed database (offscreen)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

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
    # artists are pinyin-sorted: 邓紫棋 (d) < 林俊杰 (l) < 周杰伦 (z)
    assert [win._artist_list.item(i).data(Qt.UserRole) for i in range(1, 4)] == [
        "邓紫棋", "林俊杰", "周杰伦",
    ]
    item = win._artist_list.item(3)
    assert item.data(Qt.UserRole) == "周杰伦"
    win._artist_list.setCurrentItem(item)
    assert win._current_artist == "周杰伦"
    assert win._song_table.rowCount() == 4
    win._artist_list.setCurrentItem(win._artist_list.item(0))  # back to 全部
    assert win._song_table.rowCount() == 10


def test_placeholder_label_shows_pinyin_letter(qapp):
    from ezkaraoke.select_window import make_placeholder_pixmap, placeholder_label

    assert placeholder_label("丁丁") == "中D-丁"
    assert placeholder_label("阿东") == "中A-阿"
    # 整串消歧：长春虫子 -> ch，不能按单字"长"取 zh
    assert placeholder_label("长春虫子") == "中C-长"
    assert placeholder_label("Coldplay") == "C"
    icon = QIcon(make_placeholder_pixmap("丁丁", size=112))
    assert not icon.isNull()


def test_avatar_pixmap_label_always_drawn(qapp):
    import io

    from PIL import Image

    from ezkaraoke.select_window import avatar_pixmap

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(buf, "PNG")
    red = buf.getvalue()

    with_photo = avatar_pixmap("丁丁", red, size=64)
    assert not with_photo.isNull()
    px = with_photo.toImage().pixelColor(8, 8)
    assert px.red() > 200  # photo shows through behind the label

    without = avatar_pixmap("丁丁", None, size=64)
    assert not without.isNull()
    px2 = without.toImage().pixelColor(8, 8)
    assert abs(px2.red() - 0x2A) < 12 and abs(px2.blue() - 0x3E) < 12  # placeholder bg


def test_artist_list_pinyin_order_a_before_c(qapp, tmp_path):
    """回归：阿东 (a) 必须排在 长春虫子 (ch) 前面（不能按 Unicode 码位序）。"""
    songs = [
        Song("长春虫子", "歌一", "/music/长春虫子-歌一.mp4"),
        Song("阿东", "歌二", "/music/阿东-歌二.mp4"),
        Song("周杰伦", "歌三", "/music/周杰伦-歌三.mp4"),
    ]
    db = SongDatabase(tmp_path / "pinyin.db")
    db.rebuild(songs)
    for name in db.artists():
        db.mark_avatar_tried(name)
    window = SelectWindow(PlayerController(), db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    try:
        order = [window._artist_list.item(i).data(Qt.UserRole)
                 for i in range(1, window._artist_list.count())]
        assert order == ["阿东", "长春虫子", "周杰伦"]
    finally:
        window.close()
        db.close()


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


def _menu_delete_setup(win, monkeypatch, answer):
    """Make the context menu pick 永久删除 and the dialog answer *answer*.

    PySide6 C++ classes ignore class-level attribute assignment, so the
    module-level QMenu/QMessageBox references are patched instead.
    """
    import types

    from ezkaraoke import select_window as sw

    class FakeMenu(QMenu):
        def exec(self, *a, **k):  # noqa: A003
            return self.actions()[0]

    fake_box = types.SimpleNamespace(
        question=lambda *a, **k: answer,
        StandardButton=QMessageBox.StandardButton,
    )
    monkeypatch.setattr(sw, "QMenu", FakeMenu)
    monkeypatch.setattr(sw, "QMessageBox", fake_box)


def _row_center(win, title: str):
    from PySide6.QtCore import QPoint

    table = win._song_table
    for r in range(table.rowCount()):
        if table.item(r, 1) is not None and table.item(r, 1).text() == title:
            return QPoint(table.visualItemRect(table.item(r, 1)).center())
    raise AssertionError(f"no row with title {title}")


def test_context_menu_permanent_delete(qapp, tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    f1 = media / "周杰伦-晴天.mp4"
    f1.write_bytes(b"video-data")
    f2 = media / "邓紫棋-泡沫.mp4"
    f2.write_bytes(b"video-data")
    songs = [Song("周杰伦", "晴天", str(f1)), Song("邓紫棋", "泡沫", str(f2))]
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(songs)
    for name in db.artists():
        db.mark_avatar_tried(name)
    controller = PlayerController()
    controller.append(songs[0])  # 晴天 is also queued
    window = SelectWindow(controller, db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    try:
        from ezkaraoke import select_window as sw

        removed_cache_calls = []
        monkeypatch.setattr(
            sw, "remove_cached_for", removed_cache_calls.append
        )
        _menu_delete_setup(window, monkeypatch, QMessageBox.StandardButton.Yes)
        window._on_song_context_menu(_row_center(window, "晴天"))
        qapp.processEvents()
        assert not f1.exists()          # file removed from disk
        assert f2.exists()
        assert window._db.song_count() == 1
        assert window._db.artists() == ["邓紫棋"]
        assert len(controller.queue) == 0  # queued copy removed
        assert window._song_table.rowCount() == 1
        assert "1 首" in window._status_left.text()
        assert removed_cache_calls == [str(f1)]
    finally:
        window.close()
        qapp.processEvents()
        db.close()


def test_context_menu_delete_declined_keeps_everything(qapp, tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    f1 = media / "song.mp4"
    f1.write_bytes(b"x")
    song = Song("某人", "某歌", str(f1))
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild([song])
    for name in db.artists():
        db.mark_avatar_tried(name)
    window = SelectWindow(PlayerController(), db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    try:
        _menu_delete_setup(window, monkeypatch, QMessageBox.StandardButton.No)
        window._on_song_context_menu(_row_center(window, "某歌"))
        qapp.processEvents()
        assert f1.exists()
        assert window._db.song_count() == 1
    finally:
        window.close()
        qapp.processEvents()
        db.close()


def test_context_menu_delete_current_song_stops_playback(qapp, tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    f1 = media / "now.mp4"
    f1.write_bytes(b"x")
    f2 = media / "next.mp4"
    f2.write_bytes(b"y")
    s1 = Song("歌手", "正在播", str(f1))
    s2 = Song("歌手", "下一首", str(f2))
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild([s1, s2])
    for name in db.artists():
        db.mark_avatar_tried(name)
    controller = PlayerController()
    controller._queue = [s1, s2]  # pretend 正在播 is current (no VLC needed)
    controller._current_index = 0
    assert controller.current_song is not None
    window = SelectWindow(controller, db, Config(music_folder=""))
    window.show()
    qapp.processEvents()
    try:
        _menu_delete_setup(window, monkeypatch, QMessageBox.StandardButton.Yes)
        window._on_song_context_menu(_row_center(window, "正在播"))
        qapp.processEvents()
        assert not f1.exists()
        assert controller.current_index == -1        # playback stopped
        assert controller.queue == [s2]             # next song survives
        assert window._db.song_count() == 1
    finally:
        window.close()
        qapp.processEvents()
        db.close()
