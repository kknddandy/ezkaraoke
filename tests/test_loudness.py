"""Tests for ezkaraoke.loudness (EBU R128 measurement + worker).

Fixture synthesis: the sine WAVs are generated with exact amplitude (a
sine's integrated loudness is about 3.01 LU below its peak, so amplitude
``sqrt(2) * 10 ** (target / 20)`` lands on the target LUFS). Generating
the samples in Python keeps the level exact regardless of the ffmpeg
build's lavfi ``sine`` default amplitude. The dual-track mp4 still goes
through ffmpeg (muxing two aac streams). Tests that need the real
binaries are skipped via shutil.which guards.
"""

import hashlib
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from ezkaraoke.database import SCHEMA, SongDatabase
from ezkaraoke.loudness import (
    LoudnessResult,
    LoudnessWorker,
    audio_track_count,
    build_cmd,
    compute_gain,
    measure_file,
    parse_ebur128,
    pending_files,
)
from ezkaraoke.scanner import scan_folder

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(
    not HAS_FFMPEG, reason="ffmpeg not available"
)
requires_media_tools = pytest.mark.skipif(
    not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe not available"
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ----------------------------------------------------------------- fixtures
def _sine_wav(path, target_lufs: float, duration: float = 5.0) -> None:
    """Write a mono 16-bit PCM sine that measures *target_lufs* (EBU R128).

    A full-scale sine's integrated loudness sits about 3.01 LU below its
    peak, so amplitude sqrt(2) * 10 ** (target / 20) hits the target.
    """
    amp = math.sqrt(2.0) * 10 ** (target_lufs / 20.0)
    rate = 44100
    freq = 997.0
    n = int(rate * duration)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            sample = amp * math.sin(2.0 * math.pi * freq * i / rate)
            sample = max(-1.0, min(1.0, sample))
            frames += struct.pack("<h", int(sample * 32767))
        w.writeframes(bytes(frames))


def _sine_mp4(path, target_lufs: float, duration: float = 5.0) -> None:
    """Sine at *target_lufs* wrapped in an audio-only mp4.

    .mp4 is a VIDEO_EXT so scan_folder registers it, like the real library.
    """
    src = path.with_suffix(".src.wav")
    _sine_wav(src, target_lufs, duration)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-c:a", "aac", "-b:a", "128k", str(path),
        ],
        check=True,
        capture_output=True,
    )
    src.unlink()


def _dual_track_mp4(path) -> None:
    """Mux two 997 Hz sine aac tracks ~14 dB apart into one mp4."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=997:duration=5",
            "-f", "lavfi", "-i", "sine=frequency=997:duration=5",
            "-filter_complex", "[0:a]volume=-10dB[a0];[1:a]volume=-24dB[a1]",
            "-map", "[a0]", "-map", "[a1]",
            "-c:a", "aac", "-b:a", "128k", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _db_with_folder(folder: str, tmp_path) -> tuple[SongDatabase, str]:
    db_path = str(tmp_path / "songs.db")
    db = SongDatabase(db_path)
    db.rebuild(scan_folder(folder))
    return db, db_path


def _wait_for_worker(qapp, worker, timeout=120.0):
    deadline = time.time() + timeout
    while worker.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert worker.wait(5000)
    for _ in range(200):  # flush queued cross-thread signals
        qapp.processEvents()


# ------------------------------------------------------------- build_cmd
def test_build_cmd_uses_vn_and_track_position():
    assert build_cmd("/m/a.mp4", 1) == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        "/m/a.mp4",
        "-vn",
        "-map",
        "0:a:1",
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]


# ----------------------------------------------------------- parse_ebur128
def test_parse_ebur128_summary_block():
    stderr = (
        "ffmpeg version 8.0.1\n"
        "Input #0, wav, from '/m/a.wav':\n"
        "  Duration: 00:04:23.12, start: 0.000000, bitrate: 1500 kb/s\n"
        "Stream #0:0: Audio: pcm_s16le, 44100 Hz, mono\n"
        "Integrated loudness:\n"
        "    I:          -9.7 LUFS\n"
        "    Threshold: -20.2 LUFS\n"
        "\n"
        "  Loudness range:\n"
        "    LRA:         9.8 LU\n"
        "\n"
        "  True peak:\n"
        "    Peak:        2.7 dBFS\n"
    )
    result = parse_ebur128(stderr)
    assert result is not None
    assert result.lufs == -9.7
    assert result.peak_db == 2.7
    assert result.duration == pytest.approx(263.12)


def test_parse_ebur128_takes_last_integrated_block():
    stderr = (
        "Integrated loudness:\n"
        "    I:          -15.0 LUFS\n"
        "True peak:\n"
        "    Peak:        -1.0 dBFS\n"
        "Integrated loudness:\n"
        "    I:          -9.7 LUFS\n"
        "True peak:\n"
        "    Peak:        2.7 dBFS\n"
    )
    result = parse_ebur128(stderr)
    assert result is not None
    assert result.lufs == -9.7
    assert result.peak_db == 2.7


def test_parse_ebur128_peak_and_duration_optional():
    result = parse_ebur128("Integrated loudness:\n    I:          -11.0 LUFS\n")
    assert result is not None
    assert result.lufs == -11.0
    assert result.peak_db is None
    assert result.duration is None


def test_parse_ebur128_without_integrated_returns_none():
    assert (
        parse_ebur128("ffmpeg: /m/a.wav: No such file or directory\n") is None
    )


# ------------------------------------------------------------- compute_gain
def test_compute_gain_normal_boost():
    assert compute_gain(-15.0, None, -11.25) == pytest.approx(3.75)
    # a comfortable peak does not limit the gain
    assert compute_gain(-15.0, -8.0, -11.25) == pytest.approx(3.75)


def test_compute_gain_clipped_peak_forces_attenuation():
    # already at target loudness but peaking at +2.7 dBFS: can only go down
    gain = compute_gain(-11.25, 2.7, -11.25)
    assert gain == pytest.approx(-3.7)
    assert gain < 0


def test_compute_gain_clamped_to_max_boost():
    assert compute_gain(-31.2, None, -11.25) == pytest.approx(12.0)


def test_compute_gain_negative_for_loud_source():
    assert compute_gain(-7.6, None, -11.25) == pytest.approx(-3.65)


def test_compute_gain_clamped_to_max_attenuate():
    assert compute_gain(5.0, None, -11.25) == pytest.approx(-16.25)
    assert compute_gain(10.0, None, -11.25) == pytest.approx(-20.0)


# --------------------------------------------------------------- measure_file
@requires_ffmpeg
def test_measure_file_sine_accuracy(tmp_path):
    """Spec 9.1: synthesized sines must measure within 0.5 LU of intent."""
    quiet = tmp_path / "quiet.wav"
    loud = tmp_path / "loud.wav"
    _sine_wav(quiet, -20.0)
    _sine_wav(loud, -6.0)

    r_quiet = measure_file(str(quiet), 0)
    r_loud = measure_file(str(loud), 0)

    assert r_quiet is not None and r_loud is not None
    assert abs(r_quiet.lufs - (-20.0)) <= 0.5
    assert abs(r_loud.lufs - (-6.0)) <= 0.5
    # peaks sit 3.01 LU above the sine's loudness
    assert r_quiet.peak_db is not None
    assert abs(r_quiet.peak_db - (-17.0)) <= 0.5
    assert r_loud.peak_db is not None
    assert abs(r_loud.peak_db - (-3.0)) <= 0.5
    assert r_quiet.duration is not None
    assert abs(r_quiet.duration - 5.0) <= 0.1


@requires_ffmpeg
def test_measure_file_is_read_only(tmp_path):
    """Spec 9.8: md5 (and size/mtime) must not change after measuring."""
    song = tmp_path / "E-e.wav"
    _sine_wav(song, -20.0, duration=2.0)
    before_md5 = hashlib.md5(song.read_bytes()).hexdigest()
    st_before = os.stat(str(song))

    result = measure_file(str(song), 0)

    assert result is not None
    assert hashlib.md5(song.read_bytes()).hexdigest() == before_md5
    st_after = os.stat(str(song))
    assert st_before.st_size == st_after.st_size
    assert st_before.st_mtime == st_after.st_mtime


def test_measure_file_missing_file_returns_none(tmp_path):
    assert measure_file(str(tmp_path / "nope.mp4"), 0) is None


def test_measure_file_stop_event_kills_in_flight(tmp_path, monkeypatch):
    """A set stop_event must kill the in-flight ffmpeg and return None."""
    state = {"killed": False, "waited": False}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)

        def kill(self):
            state["killed"] = True

        def wait(self):
            state["waited"] = True

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    stop = threading.Event()
    stop.set()

    assert (
        measure_file(str(tmp_path / "a.mp4"), 0, timeout=120.0, stop_event=stop)
        is None
    )
    assert state["killed"]
    assert state["waited"]


def test_audio_track_count_missing_file_returns_one(tmp_path):
    assert audio_track_count(str(tmp_path / "nope.mp4")) == 1


# --------------------------------------------------------------- pending_files
def test_pending_files_does_not_retry_deliberate_failures(tmp_path):
    """ok=0 row with a matching (size, mtime) snapshot is final (spec 5.2)."""
    folder = tmp_path / "media"
    folder.mkdir()
    a, b = folder / "a.mp4", folder / "b.mp4"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    st_a, st_b = os.stat(str(a)), os.stat(str(b))

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        conn.executescript(SCHEMA)
        for path, st in ((a, st_a), (b, st_b)):
            conn.execute(
                "INSERT INTO songs (path, artist, title, letter, size) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(path), "X", "x", "X", st.st_size),
            )
        conn.execute(
            "INSERT INTO loudness (path, track_pos, lufs, peak_db, duration, "
            "size, mtime, ok, measured_at, tool) "
            "VALUES (?, 0, NULL, NULL, NULL, ?, ?, 0, 1.0, 'ebur128')",
            (str(a), st_a.st_size, st_a.st_mtime),
        )
        conn.commit()
        # 'a' keeps its matching snapshot -> only 'b' is pending
        assert pending_files(conn) == [(str(b), st_b.st_size, st_b.st_mtime, set())]
        # force re-measures everything, ignoring snapshots (matching is
        # always empty when forcing)
        forced = [
            (p, matching)
            for p, _s, _m, matching in pending_files(conn, force=True)
        ]
        assert forced == [(str(a), set()), (str(b), set())]
    finally:
        conn.close()


def test_pending_files_does_not_call_ffprobe(tmp_path, monkeypatch):
    """Enumeration is stat+DB only: ffprobe must NOT be spawned here."""
    folder = tmp_path / "media"
    folder.mkdir()
    stats = {}
    for name in ("a.mp4", "b.mp4"):
        p = folder / name
        p.write_bytes(b"x" * 100)
        stats[str(p)] = os.stat(str(p))

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        conn.executescript(SCHEMA)
        for path, st in stats.items():
            conn.execute(
                "INSERT INTO songs (path, artist, title, letter, size) "
                "VALUES (?, ?, ?, ?, ?)",
                (path, "X", "x", "X", st.st_size),
            )
        conn.commit()

        calls = []

        def fail_ffprobe(path):
            calls.append(path)
            raise AssertionError("pending_files must not call ffprobe")

        monkeypatch.setattr(
            "ezkaraoke.loudness.audio_track_count", fail_ffprobe
        )
        got = sorted(pending_files(conn))
        expected = sorted(
            (path, st.st_size, st.st_mtime, set())
            for path, st in stats.items()
        )
        assert got == expected
        assert calls == []
    finally:
        conn.close()


# ------------------------------------------------------------------ worker
@requires_media_tools
def test_worker_is_idempotent(qapp, tmp_path):
    """Spec 9.4: after a full run, loudness_pending() is empty; rerun is a no-op."""
    folder = tmp_path / "media"
    folder.mkdir()
    _sine_mp4(folder / "A-a.mp4", -20.0)
    _sine_mp4(folder / "B-b.mp4", -6.0)

    db, db_path = _db_with_folder(str(folder), tmp_path)
    assert len(db.loudness_pending()) == 2

    errors = []
    worker = LoudnessWorker(db_path, workers=2)
    worker.error.connect(errors.append)
    worker.start()
    _wait_for_worker(qapp, worker)
    assert errors == []
    for name in ("A-a.mp4", "B-b.mp4"):
        rows = db.get_loudness(str(folder / name))
        assert len(rows) == 1
        assert rows[0].ok and rows[0].lufs is not None
        assert rows[0].size == os.path.getsize(str(folder / name))
    assert db.loudness_pending() == []

    # second run: nothing left to measure
    progress = []
    worker2 = LoudnessWorker(db_path, workers=2)
    worker2.progress.connect(lambda done, total: progress.append((done, total)))
    worker2.start()
    _wait_for_worker(qapp, worker2)
    assert progress and progress[-1] == (0, 0)
    assert db.loudness_pending() == []
    db.close()


def test_enumeration_total_known_before_measurement(qapp, tmp_path, monkeypatch):
    """The first total-carrying progress emission must already have the
    full candidate count, BEFORE any measurement completes.

    On a large library the old code ffprobe'd every file serially during
    enumeration, so the GUI sat on "0/0" for minutes; now enumeration is
    stat+DB only and total is known almost immediately.
    """
    folder = tmp_path / "media"
    folder.mkdir()
    for name in ("A-a.mp4", "B-b.mp4", "C-c.mp4"):
        (folder / name).write_bytes(b"x" * 100)
    db, db_path = _db_with_folder(str(folder), tmp_path)

    release = threading.Event()
    completed = []

    def stub_measure(path, track_pos, timeout=120.0, stop_event=None):
        release.wait(5)
        completed.append((path, track_pos))
        return LoudnessResult(lufs=-11.0, peak_db=-3.0, duration=1.0)

    monkeypatch.setattr("ezkaraoke.loudness.measure_file", stub_measure)
    monkeypatch.setattr(
        "ezkaraoke.loudness.audio_track_count", lambda path: 1
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)

    worker = LoudnessWorker(db_path, workers=3)
    progress = []
    worker.progress.connect(lambda done, total: progress.append((done, total)))
    worker.start()

    # pump until a total-carrying emission lands...
    deadline = time.time() + 10.0
    while time.time() < deadline:
        qapp.processEvents()
        if any(d == 0 and t == 3 for d, t in progress):
            break
        time.sleep(0.005)

    total_carrying = [(d, t) for d, t in progress if t]
    assert total_carrying and total_carrying[0] == (0, 3)
    # ...and no measurement had completed when it arrived
    assert completed == []

    release.set()
    _wait_for_worker(qapp, worker)
    assert len(completed) == 3
    assert db.loudness_pending() == []
    db.close()


@requires_media_tools
def test_mtime_change_requeues_file(qapp, tmp_path):
    """Spec 9.5: bumping the mtime invalidates the stored snapshot."""
    folder = tmp_path / "media"
    folder.mkdir()
    song = folder / "C-c.mp4"
    _sine_mp4(song, -20.0)

    db, db_path = _db_with_folder(str(folder), tmp_path)
    worker = LoudnessWorker(db_path, workers=1)
    worker.start()
    _wait_for_worker(qapp, worker)
    rows = db.get_loudness(str(song))
    assert rows and rows[0].ok and rows[0].lufs is not None

    conn = sqlite3.connect(db_path)
    try:
        assert pending_files(conn) == []
        # +10 s: the stored (size, mtime) snapshot no longer matches
        t = os.stat(str(song)).st_mtime
        os.utime(song, (t + 10, t + 10))
        pending = pending_files(conn)
    finally:
        conn.close()
    db.close()
    assert len(pending) == 1
    assert pending[0][0] == str(song)
    # the stored row's (size, mtime) snapshot no longer matches: nothing
    # is reusable, the worker will re-measure from scratch
    assert pending[0][3] == set()


def test_worker_without_ffmpeg_emits_error(qapp, tmp_path, monkeypatch):
    """Spec 9.6: missing ffmpeg -> finished + error, no exception."""
    song = tmp_path / "A-a.mp4"
    song.write_bytes(b"not audio" * 16)
    db, db_path = _db_with_folder(str(tmp_path), tmp_path)

    monkeypatch.setattr("shutil.which", lambda name: None)
    errors, finished = [], []
    worker = LoudnessWorker(db_path)
    worker.error.connect(errors.append)
    worker.finished.connect(lambda: finished.append(True))
    assert worker.daemon is True
    worker.start()
    _wait_for_worker(qapp, worker)

    assert finished == [True]
    assert len(errors) == 1
    assert "ffmpeg" in errors[0]
    db.close()


@requires_media_tools
def test_worker_measures_each_audio_track(qapp, tmp_path):
    """Spec 9.7: dual-track file -> rows for track_pos 0 and 1, >= 6 dB apart."""
    folder = tmp_path / "media"
    folder.mkdir()
    dual = folder / "D-d.mp4"
    _dual_track_mp4(dual)
    assert audio_track_count(str(dual)) == 2

    db, db_path = _db_with_folder(str(folder), tmp_path)
    worker = LoudnessWorker(db_path, workers=2)
    worker.start()
    _wait_for_worker(qapp, worker)

    rows = db.get_loudness(str(dual))
    assert [r.track_pos for r in rows] == [0, 1]
    assert all(r.ok and r.lufs is not None for r in rows)
    assert rows[0].lufs != rows[1].lufs
    assert abs(rows[0].lufs - rows[1].lufs) >= 6.0
    assert db.loudness_pending() == []
    db.close()


def test_worker_stop_is_prompt(qapp, tmp_path, monkeypatch):
    """stop() must return promptly even with a long measurement in flight."""
    folder = tmp_path / "media"
    folder.mkdir()
    song = folder / "A-a.mp4"
    song.write_bytes(b"x" * 1000)
    db, db_path = _db_with_folder(str(folder), tmp_path)

    def slow_measure(path, track_pos, timeout=120.0, stop_event=None):
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.1)
        return None

    monkeypatch.setattr("ezkaraoke.loudness.measure_file", slow_measure)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)

    worker = LoudnessWorker(db_path, workers=1)
    worker.start()
    time.sleep(0.5)  # let the worker get to the (slow) measurement
    worker.stop()

    assert worker.wait(5000) is True
    db.close()


def test_stop_does_not_poison_inflight(qapp, tmp_path, monkeypatch):
    """stop() while ffmpeg is in flight must not write ok=0 poison rows."""
    folder = tmp_path / "media"
    folder.mkdir()
    a, b = folder / "P-p.mp4", folder / "Q-q.mp4"
    a.write_bytes(b"x" * 1000)
    b.write_bytes(b"y" * 1000)
    db, db_path = _db_with_folder(str(folder), tmp_path)

    def blocking_measure(path, track_pos, timeout=120.0, stop_event=None):
        stop_event.wait(2)
        return None

    monkeypatch.setattr("ezkaraoke.loudness.measure_file", blocking_measure)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)

    errors, finished = [], []
    worker = LoudnessWorker(db_path, workers=2)
    worker.error.connect(errors.append)
    worker.finished.connect(lambda: finished.append(True))
    worker.start()
    time.sleep(0.4)  # let both measurements get in flight
    worker.stop()
    assert worker.wait(5000) is True
    for _ in range(200):  # flush queued cross-thread signals
        qapp.processEvents()

    assert finished == [True]
    assert errors == []
    # nothing written: no rows at all, so the files are NOT poisoned
    assert db.get_loudness(str(a)) == []
    assert db.get_loudness(str(b)) == []
    conn = sqlite3.connect(db_path)
    try:
        pending = pending_files(conn)
    finally:
        conn.close()
    assert {p for p, _s, _m, _matching in pending} == {str(a), str(b)}
    # nothing was measured: no stored row can match the current snapshot
    assert all(matching == set() for _p, _s, _m, matching in pending)
    db.close()


def test_stop_mid_first_measure_leaves_no_rows(qapp, tmp_path, monkeypatch):
    """A first-ever measurement interrupted mid-file leaves no rows at all."""
    folder = tmp_path / "media"
    folder.mkdir()
    song = folder / "R-r.mp4"
    song.write_bytes(b"w" * 500)
    db, db_path = _db_with_folder(str(folder), tmp_path)

    def blocking_measure(path, track_pos, timeout=120.0, stop_event=None):
        stop_event.wait(2)
        return None

    monkeypatch.setattr("ezkaraoke.loudness.measure_file", blocking_measure)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)

    errors = []
    worker = LoudnessWorker(db_path, workers=1)
    worker.error.connect(errors.append)
    worker.start()
    time.sleep(0.4)  # in-flight on track 0
    worker.stop()
    assert worker.wait(5000) is True
    for _ in range(200):  # flush queued cross-thread signals
        qapp.processEvents()

    assert errors == []
    assert db.get_loudness(str(song)) == []
    conn = sqlite3.connect(db_path)
    try:
        pending = pending_files(conn)
    finally:
        conn.close()
    assert [p for p, _s, _m, _matching in pending] == [str(song)]
    db.close()


def test_stop_keeps_other_track_rows(qapp, tmp_path, monkeypatch):
    """A stop mid-run must not wipe prior rows of the other tracks.

    The file is a candidate (snapshot bumped) with two pre-seeded rows from
    an older snapshot; an interrupted re-measurement must leave those prior
    rows untouched and the file must still pend.
    """
    folder = tmp_path / "media"
    folder.mkdir()
    song = folder / "M-m.mp4"
    song.write_bytes(b"z" * 2000)
    st0 = os.stat(str(song))
    db, db_path = _db_with_folder(str(folder), tmp_path)

    # pre-seed dual-track rows from an OLD (size, mtime) snapshot
    conn = sqlite3.connect(db_path)
    try:
        for pos, lufs in ((0, -20.0), (1, -30.0)):
            conn.execute(
                "INSERT INTO loudness (path, track_pos, lufs, peak_db, duration, "
                "size, mtime, ok, measured_at, tool) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1.0, 'ebur128')",
                (str(song), pos, lufs, None, 5.0, st0.st_size - 1,
                 st0.st_mtime - 100),
            )
        conn.commit()
    finally:
        conn.close()

    # bump the mtime so the file becomes a re-measurement candidate
    t = os.stat(str(song)).st_mtime
    os.utime(song, (t + 50, t + 50))

    def blocking_measure(path, track_pos, timeout=120.0, stop_event=None):
        stop_event.wait(2)
        return None

    monkeypatch.setattr(
        "ezkaraoke.loudness.audio_track_count", lambda path: 2
    )
    monkeypatch.setattr("ezkaraoke.loudness.measure_file", blocking_measure)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)

    errors = []
    worker = LoudnessWorker(db_path, workers=2)
    worker.error.connect(errors.append)
    worker.start()
    time.sleep(0.4)  # in-flight on track 0
    worker.stop()
    assert worker.wait(5000) is True
    for _ in range(200):  # flush queued cross-thread signals
        qapp.processEvents()

    assert errors == []
    # prior rows are untouched: same old snapshot, still ok=1
    rows = db.get_loudness(str(song))
    assert [(r.track_pos, r.lufs, r.size, r.mtime, r.ok) for r in rows] == [
        (0, -20.0, st0.st_size - 1, st0.st_mtime - 100, True),
        (1, -30.0, st0.st_size - 1, st0.st_mtime - 100, True),
    ]
    conn = sqlite3.connect(db_path)
    try:
        pending = pending_files(conn)
    finally:
        conn.close()
    assert {p for p, _s, _m, _matching in pending} == {str(song)}
    # both pre-seeded rows carry the OLD snapshot: no position matches,
    # so the worker will re-measure every track of the file
    assert [matching for _p, _s, _m, matching in pending] == [set()]
    db.close()


@requires_media_tools
def test_zero_audio_track_file_gets_final_failed_row(qapp, tmp_path):
    """A video-only file must not stay pending forever (acceptance-critical)."""
    folder = tmp_path / "media"
    folder.mkdir()
    video = folder / "V-v.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=16x16:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
        capture_output=True,
    )
    assert audio_track_count(str(video)) == 0

    db, db_path = _db_with_folder(str(folder), tmp_path)
    st = os.stat(str(video))
    conn = sqlite3.connect(db_path)
    try:
        pending = pending_files(conn)
    finally:
        conn.close()
    # one candidate carrying a real size/mtime snapshot; the track count
    # (0) is only discovered inside the pool by _measure_path
    assert pending == [(str(video), st.st_size, st.st_mtime, set())]

    worker = LoudnessWorker(db_path, workers=1)
    worker.start()
    _wait_for_worker(qapp, worker)

    rows = db.get_loudness(str(video))
    assert len(rows) == 1
    assert rows[0].track_pos == 0
    assert rows[0].ok is False
    assert rows[0].lufs is None
    assert rows[0].size == st.st_size
    assert rows[0].mtime == st.st_mtime
    assert db.loudness_pending() == []
    db.close()


@requires_media_tools
def test_track_count_shrink_drops_stale_rows(qapp, tmp_path):
    """A replaced file with fewer audio tracks must not leave stale rows."""
    folder = tmp_path / "media"
    folder.mkdir()
    song = folder / "S-s.mp4"
    _dual_track_mp4(song)
    assert audio_track_count(str(song)) == 2

    db, db_path = _db_with_folder(str(folder), tmp_path)
    worker = LoudnessWorker(db_path, workers=2)
    worker.start()
    _wait_for_worker(qapp, worker)
    rows = db.get_loudness(str(song))
    assert [r.track_pos for r in rows] == [0, 1]
    assert db.loudness_pending() == []

    # replace the dual-track file with a single-track file at the same path
    _sine_mp4(song, -20.0)
    assert audio_track_count(str(song)) == 1

    worker2 = LoudnessWorker(db_path, workers=2)
    worker2.start()
    _wait_for_worker(qapp, worker2)
    rows = db.get_loudness(str(song))
    assert len(rows) == 1  # the stale track_pos=1 row is gone
    assert rows[0].track_pos == 0
    assert rows[0].ok is True
    # The file-based measurement queue is empty; db.loudness_pending()
    # (DB-only) would still report the replaced song against its stale
    # songs.size snapshot until the next rescan, which is expected.
    conn = sqlite3.connect(db_path)
    try:
        assert pending_files(conn) == []
    finally:
        conn.close()

    # a third run must not re-pend the file
    progress = []
    worker3 = LoudnessWorker(db_path, workers=2)
    worker3.progress.connect(lambda done, total: progress.append((done, total)))
    worker3.start()
    _wait_for_worker(qapp, worker3)
    assert progress and progress[-1] == (0, 0)
    assert len(db.get_loudness(str(song))) == 1
    db.close()
