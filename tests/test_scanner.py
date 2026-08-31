"""Tests for ezkaraoke.scanner (no libvlc required)."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from ezkaraoke.scanner import VIDEO_EXTS, ScanWorker, parse_filename, scan_folder


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ------------------------------------------------------------- parse_filename
def test_parse_one_dash():
    assert parse_filename("周杰伦-晴天.mp4") == ("周杰伦", "晴天")


def test_parse_no_dash():
    assert parse_filename("晴天.mp4") == ("未知歌手", "晴天")


def test_parse_multi_dash():
    assert parse_filename("a-b-c-d.mp4") == ("a", "b")


def test_parse_discards_remainder():
    assert parse_filename("周杰伦-晴天-官方MV.mp4") == ("周杰伦", "晴天")


def test_parse_empty_artist():
    assert parse_filename("-x.mp4") == ("未知歌手", "x")


def test_parse_unicode():
    assert parse_filename("陈奕迅-浮夸-现场版.mkv") == ("陈奕迅", "浮夸")


def test_parse_strips_extension():
    assert parse_filename("A-B.webm") == ("A", "B")
    assert parse_filename("A-B.ts") == ("A", "B")
    # multi-dot stems keep only the final extension
    assert parse_filename("x-y-1.2.3.mp4") == ("x", "y")


def test_parse_no_extension():
    assert parse_filename("A-B") == ("A", "B")


# --------------------------------------------------------------- VIDEO_EXTS
def test_video_exts_set():
    assert VIDEO_EXTS == {
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v",
        ".mpg", ".mpeg", ".webm", ".rmvb", ".rm", ".3gp",
    }


# ---------------------------------------------------------------- scan_folder
def test_scan_folder_nested_and_deterministic(tmp_path):
    sub = tmp_path / "sub"
    deep = sub / "deep"
    deep.mkdir(parents=True)

    (tmp_path / "B歌手-B曲.mp4").write_bytes(b"")
    (tmp_path / "A歌手-A曲.MP4").write_bytes(b"")      # uppercase ext
    (tmp_path / "readme.txt").write_bytes(b"")           # not a video
    (sub / "C歌手-C曲.mkv").write_bytes(b"")
    (sub / "notes.MP4.txt").write_bytes(b"")             # not a video
    (deep / "nodash.avi").write_bytes(b"")               # no "-" in name

    songs = scan_folder(str(tmp_path))

    # deterministic walk order: root dir, then sub, then sub/deep
    assert [(s.artist, s.title) for s in songs] == [
        ("A歌手", "A曲"),
        ("B歌手", "B曲"),
        ("C歌手", "C曲"),
        ("未知歌手", "nodash"),
    ]
    assert [os.path.basename(s.path) for s in songs] == [
        "A歌手-A曲.MP4",
        "B歌手-B曲.mp4",
        "C歌手-C曲.mkv",
        "nodash.avi",
    ]
    for s in songs:
        assert os.path.isabs(s.path)
        assert os.path.isfile(s.path)

    # second scan yields identical result
    again = scan_folder(str(tmp_path))
    assert [(s.artist, s.title, s.path) for s in again] == [
        (s.artist, s.title, s.path) for s in songs
    ]


def test_scan_folder_empty(tmp_path):
    assert scan_folder(str(tmp_path)) == []


def test_scan_folder_missing_root(tmp_path):
    # os.walk on a missing dir yields nothing -> empty list, no exception
    assert scan_folder(str(tmp_path / "does-not-exist")) == []


# ------------------------------------------------------------------ ScanWorker
def test_scan_worker_finished_signal(qapp, tmp_path):
    (tmp_path / "周杰伦-晴天.mp4").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")

    results: list[list] = []
    errors: list[str] = []
    worker = ScanWorker(str(tmp_path))
    worker.finished.connect(lambda songs: results.append(songs))
    worker.error.connect(lambda message: errors.append(message))
    worker.start()
    deadline = time.time() + 10
    while worker.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert worker.wait(5000)
    for _ in range(100):  # flush queued cross-thread signals
        qapp.processEvents()

    assert errors == []
    assert len(results) == 1
    assert [(s.artist, s.title) for s in results[0]] == [("周杰伦", "晴天")]


def test_scan_worker_daemon_thread(qapp, tmp_path):
    worker = ScanWorker(str(tmp_path))
    assert worker.daemon is True  # never blocks interpreter shutdown
    worker.start()
    assert worker.wait(5000)
