"""Tests for ezkaraoke.pitchshift (ffmpeg rubberband offline pitch shift)."""

import os
import stat

import pytest

from ezkaraoke import pitchshift as ps

FAKE_OK = """#!/bin/bash
echo "frame=  10 fps=  10 q=20.0 size=N/A time=00:00:05.20 bitrate=N/A speed=1x" >&2
out="${@: -1}"
printf "fake-shifted-media" > "$out"
exit 0
"""

FAKE_FAIL = """#!/bin/bash
echo "Conversion failed" >&2
exit 1
"""

FAKE_FILTERS_WITH = " rubberband  A->A       Apply time-stretching and pitch-shifting.\n"
FAKE_FILTERS_WITHOUT = " atempo     A->A       Adjust audio tempo.\n"


def _install(tmp_path, monkeypatch, ffmpeg_body: str, with_rubberband: bool):
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(ffmpeg_body)
    ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IEXEC)
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text("#!/bin/bash\necho 10.0\n")
    ffprobe.chmod(ffprobe.stat().st_mode | stat.S_IEXEC)
    mapping = {
        "ffmpeg": str(ffmpeg),
        "ffprobe": str(ffprobe),
        "filters": with_rubberband,
    }

    def fake_which(name, *a, **k):
        return mapping.get(name)

    monkeypatch.setattr(ps.shutil, "which", fake_which)

    class R:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0

    def fake_run(args, *a, **k):
        if "-filters" in args:
            return R(FAKE_FILTERS_WITH if with_rubberband else FAKE_FILTERS_WITHOUT)
        return R("10.0")  # ffprobe

    monkeypatch.setattr(ps.subprocess, "run", fake_run)


def test_ratio_for_semitones():
    assert ps.ratio_for_semitones(0) == pytest.approx(1.0)
    assert ps.ratio_for_semitones(12) == pytest.approx(2.0)
    assert ps.ratio_for_semitones(-12) == pytest.approx(0.5)
    assert ps.ratio_for_semitones(1) == pytest.approx(2.0 ** (1 / 12))


def test_shifted_file_path_stable_and_distinct(tmp_path):
    src = str(tmp_path / "song.mp4")
    open(src, "wb").write(b"x")
    a = ps.shifted_file_path(src, 2)
    b = ps.shifted_file_path(src, 2)
    c = ps.shifted_file_path(src, 3)
    assert a == b
    assert a != c
    assert a.suffix == ".mkv"
    os.utime(src, (2000, 2000))  # touch: mtime changes -> new variant
    assert ps.shifted_file_path(src, 2) != a


def test_shifted_file_path_missing_source(tmp_path):
    p = ps.shifted_file_path(str(tmp_path / "gone.mp4"), 1)
    assert p.name.startswith("gone_+1_")


def test_shift_available_probe(tmp_path, monkeypatch):
    ps.reset_probe()
    _install(tmp_path, monkeypatch, FAKE_OK, with_rubberband=True)
    assert ps.shift_available() is True
    assert ps.shift_available() is True  # cached
    ps.reset_probe()
    _install(tmp_path, monkeypatch, FAKE_OK, with_rubberband=False)
    assert ps.shift_available() is False
    ps.reset_probe()
    monkeypatch.setattr(ps.shutil, "which", lambda name: None)
    assert ps.shift_available() is False
    ps.reset_probe()


def test_duration_seconds(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, FAKE_OK, with_rubberband=True)
    src = tmp_path / "song.mp4"
    src.write_bytes(b"x")
    assert ps.duration_seconds(str(src)) == pytest.approx(10.0)
    monkeypatch.setattr(ps.shutil, "which", lambda name: None)
    assert ps.duration_seconds(str(src)) is None


def test_shift_audio_success_and_progress(tmp_path, monkeypatch):
    ps.reset_probe()
    _install(tmp_path, monkeypatch, FAKE_OK, with_rubberband=True)
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "song.mp4"
    src.write_bytes(b"original")
    dest = ps.shifted_file_path(str(src), 2)
    progress: list[float] = []
    ok = ps.shift_audio(str(src), dest, 2, progress_cb=progress.append)
    assert ok is True
    assert dest.exists()
    assert not dest.with_suffix(".part").exists()
    assert progress == [pytest.approx(0.52)]  # 5.2s of a 10s source


def test_shift_audio_reports_failure(tmp_path, monkeypatch):
    ps.reset_probe()
    _install(tmp_path, monkeypatch, FAKE_FAIL, with_rubberband=True)
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "song.mp4"
    src.write_bytes(b"original")
    dest = ps.shifted_file_path(str(src), 2)
    assert ps.shift_audio(str(src), dest, 2) is False
    assert not dest.exists()
    assert not dest.with_suffix(".part").exists()


def test_shift_audio_cancelled(tmp_path, monkeypatch):
    import threading

    ps.reset_probe()
    _install(tmp_path, monkeypatch, FAKE_OK, with_rubberband=True)
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "song.mp4"
    src.write_bytes(b"original")
    dest = ps.shifted_file_path(str(src), 2)
    cancel = threading.Event()
    cancel.set()
    assert ps.shift_audio(str(src), dest, 2, cancel_event=cancel) is False
    assert not dest.exists()


def test_prune_cache_lru(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ps, "MAX_CACHE_BYTES", 1000)
    monkeypatch.setattr(ps, "KEEP_CACHE_BYTES", 600)
    (tmp_path / "cache").mkdir()
    names = []
    for i in range(3):
        p = tmp_path / "cache" / f"v{i}.mkv"
        p.write_bytes(b"x" * 400)
        os.utime(p, (1000 + i, 1000 + i))  # v0 oldest
        names.append(p)
    ps.prune_cache()
    assert not names[0].exists()
    assert not names[1].exists()
    assert names[2].exists()  # newest kept; 400 <= 600


def test_prune_cache_under_limit_keeps_all(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    p = tmp_path / "cache" / "v.mkv"
    p.write_bytes(b"x" * 100)
    ps.prune_cache()
    assert p.exists()


def test_remove_cached_for_deletes_only_matching_stem(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    a1 = tmp_path / "cache" / "song_+2_abc.mkv"
    a2 = tmp_path / "cache" / "song_-3_def.mkv"
    b1 = tmp_path / "cache" / "song2_+2_ghi.mkv"  # different stem, untouched
    for p in (a1, a2, b1):
        p.write_bytes(b"x")
    removed = ps.remove_cached_for("/music/song.mp4")
    assert removed == 2
    assert not a1.exists() and not a2.exists()
    assert b1.exists()
    # empty stem (no extension) removes nothing
    assert ps.remove_cached_for("/music/") == 0
    assert b1.exists()


def test_warm_cache_removes_orphan_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    part = tmp_path / "cache" / "v.mkv.part"
    part.write_bytes(b"half")
    good = tmp_path / "cache" / "v.mkv"
    good.write_bytes(b"ok")
    ps.warm_cache()
    assert not part.exists()
    assert good.exists()
