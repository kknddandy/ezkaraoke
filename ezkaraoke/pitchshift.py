"""Offline pitch shifting via ffmpeg's rubberband filter.

Ubuntu's libvlc5 (3.0.23) exports no pure pitch-shift API — only the
tape-speed ``set_rate`` — so shifted playback is precomputed instead: the
audio is pitch-shifted with rubberband (length preserved, so video sync is
untouched) and remuxed next to the copied video stream. Results are cached
per (source, semitones) and invalidated when the source file changes.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

BASE_DIR = Path.home() / ".local" / "share" / "ezkaraoke"
CACHE_DIR = BASE_DIR / "pitch_cache"

MAX_CACHE_BYTES = 2 * 1024**3
KEEP_CACHE_BYTES = 1_500_000_000

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

_rubberband_ok: bool | None = None


def cache_dir() -> Path:
    return CACHE_DIR


def shift_available() -> bool:
    """True when ffmpeg exists AND was built with the rubberband filter."""
    global _rubberband_ok
    if _rubberband_ok is None:
        _rubberband_ok = False
        exe = shutil.which("ffmpeg")
        if exe:
            try:
                out = subprocess.run(
                    [exe, "-hide_banner", "-filters"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                ).stdout
                _rubberband_ok = " rubberband " in out
            except Exception:  # noqa: BLE001 - probe failure means "no"
                _rubberband_ok = False
    return _rubberband_ok


def reset_probe() -> None:
    """Clear the cached probe result (tests)."""
    global _rubberband_ok
    _rubberband_ok = None


def ratio_for_semitones(semitones: int) -> float:
    return 2.0 ** (semitones / 12.0)


def _source_stamp(source: str) -> str:
    try:
        st = os.stat(source)
    except OSError:
        return "missing"
    return f"{st.st_size}:{int(st.st_mtime)}"


def shifted_file_path(source: str, semitones: int) -> Path:
    """Cache path for a shifted variant.

    The name embeds a hash of (path, size, mtime), so a replaced file never
    hits a stale cached variant.
    """
    stem = Path(source).stem or "song"
    digest = hashlib.sha1(f"{source}|{_source_stamp(source)}".encode()).hexdigest()
    return CACHE_DIR / f"{stem}_{semitones:+d}_{digest[:10]}.mkv"


def remove_cached_for(path: str | Path) -> int:
    """Delete every cached pitch variant for *path* (its source was removed).

    Variants are named ``{stem}_{+/-}{semitones}_{hash}.mkv``; a same-named
    file in another folder shares the stem and loses its variants too
    (they regenerate on demand).  Returns the number of files removed.
    """
    stem = Path(path).stem
    if not stem:
        return 0
    prefix = f"{stem}_"
    removed = 0
    if CACHE_DIR.is_dir():
        for f in CACHE_DIR.iterdir():
            if f.is_file() and f.name.startswith(prefix):
                f.unlink(missing_ok=True)
                removed += 1
    return removed


def duration_seconds(source: str) -> float | None:
    """Media duration via ffprobe, or None when it cannot be determined."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", source],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return float(out.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def shift_audio(
    source: str,
    dest: Path,
    semitones: int,
    progress_cb=None,
    cancel_event=None,
) -> bool:
    """Pitch-shift *source* into *dest* (video stream copied, audio re-encoded).

    Writes to a ``.part`` sibling and atomically renames on success, so a
    killed/interrupted job never leaves a half-written cache file. Returns
    True when the shifted media is ready. *progress_cb* receives a 0..1
    fraction when the duration is known; *cancel_event* aborts the job.
    """
    exe = shutil.which("ffmpeg")
    if not exe or not source:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    if tmp.exists():
        tmp.unlink()
    cmd = [
        exe, "-y", "-v", "info", "-nostdin", "-i", source,
        "-map", "0",
        "-af", f"rubberband=pitch={ratio_for_semitones(semitones):.6f}",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "copy",
        "-f", "matroska",
        str(tmp),
    ]
    total = duration_seconds(source)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
    except OSError:
        return False
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.wait(timeout=5)
                tmp.unlink(missing_ok=True)
                return False
            if progress_cb is not None and total and total > 0:
                m = _TIME_RE.search(line)
                if m:
                    done = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    progress_cb(min(1.0, done / total))
        returncode = proc.wait(timeout=300)
    except (subprocess.TimeoutExpired, OSError):
        proc.kill()
        tmp.unlink(missing_ok=True)
        return False
    finally:
        try:
            proc.stderr.close()
        except Exception:  # noqa: BLE001
            pass
    if returncode != 0:
        tmp.unlink(missing_ok=True)
        return False
    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False
    # Guard against remuxed output VLC cannot play (e.g. FLV1 in matroska):
    # the result must probe as media with a sensible duration.
    if duration_seconds(str(tmp)) is None:
        tmp.unlink(missing_ok=True)
        return False
    try:
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def prune_cache(now: float | None = None) -> None:
    """LRU-delete cache entries once the directory exceeds MAX_CACHE_BYTES."""
    if not CACHE_DIR.is_dir():
        return
    entries = []
    for p in CACHE_DIR.iterdir():
        try:
            if p.is_file():
                st = p.stat()
                entries.append((st.st_mtime if now is None else now, p, st.st_size))
        except OSError:
            continue
    total = sum(size for _, _, size in entries)
    if total <= MAX_CACHE_BYTES:
        return
    for _, p, size in sorted(entries, key=lambda e: e[0]):  # oldest first
        if total <= KEEP_CACHE_BYTES:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass


def warm_cache() -> None:
    """Drop orphaned ``.part`` files and enforce the size cap (startup)."""
    if CACHE_DIR.is_dir():
        for p in CACHE_DIR.iterdir():
            if p.suffix == ".part":
                try:
                    p.unlink()
                except OSError:
                    pass
    prune_cache()


def cache_age_seconds(path: Path, now: float | None = None) -> float:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    return max(0.0, (time.time() if now is None else now) - mtime)
