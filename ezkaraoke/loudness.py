"""One-shot EBU R128 loudness measurement for the song library.

Measures every audio track of every song with ``ffmpeg -af ebur128``
(READ-ONLY: the media files are never modified) and stores the results in
the ``loudness`` table, keyed by ``(path, track_pos)`` with a
``(size, mtime)`` snapshot so replaced files are re-measured. See
``docs/loudness-normalization-spec.md`` for the design decisions (D2/D3/D5).

CLI (no GUI required)::

    python -m ezkaraoke.loudness --scan /mnt/NAS/Karaoke --workers 8
    python -m ezkaraoke.loudness --stats
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from ezkaraoke.database import LoudnessRow, SCHEMA
from ezkaraoke.i18n import tr

#: Spec D5: static gain applied at playback time.
MAX_BOOST_DB = 12.0      # more amplification would also amplify the noise floor
MAX_ATTENUATE_DB = -20.0  # libvlc 3.x preamp hard floor
CEILING_DB = -1.0        # headroom so the true peak stays below 0 dBFS

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)")
_I_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+\.\d+)\s*LUFS")
_PEAK_RE = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?\d+\.\d+)\s*dBFS")


@dataclass(frozen=True)
class LoudnessResult:
    lufs: float
    peak_db: float | None
    duration: float | None


def build_cmd(path: str, track_pos: int) -> list[str]:
    """ffmpeg arguments for one audio track; -vn skips video decoding."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        path,
        "-vn",
        "-map",
        f"0:a:{track_pos}",
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]


def parse_ebur128(stderr: str) -> LoudnessResult | None:
    """Parse the ebur128 summary out of ffmpeg's stderr.

    Takes the LAST ``Integrated loudness`` block's ``I:`` value (progress
    output may contain earlier ones); ``Peak:`` is optional. The input
    ``Duration: HH:MM:SS.xx`` line becomes ``duration`` in seconds (None
    when absent). Returns None when ``I:`` cannot be parsed.
    """
    matches = _I_RE.findall(stderr)
    if not matches:
        return None
    lufs = float(matches[-1])
    peak_matches = _PEAK_RE.findall(stderr)
    peak_db = float(peak_matches[-1]) if peak_matches else None
    duration: float | None = None
    duration_match = _DURATION_RE.search(stderr)
    if duration_match:
        h, m, s = duration_match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)
    return LoudnessResult(lufs=lufs, peak_db=peak_db, duration=duration)


def measure_file(
    path: str,
    track_pos: int,
    timeout: float = 120.0,
    stop_event: threading.Event | None = None,
) -> LoudnessResult | None:
    """Measure one audio track synchronously.

    ffmpeg missing / timeout / unreadable file / unparseable output all
    yield None. Read-only: the media file is never written. The ffmpeg
    process is polled (not blocked on) so that a set *stop_event* or the
    *timeout* deadline kills the in-flight process instead of leaving an
    orphan behind.
    """
    if shutil.which("ffmpeg") is None:
        return None
    try:
        proc = subprocess.Popen(
            build_cmd(path, track_pos),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    proc.kill()
                    proc.wait()
                    return None
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.wait()
                    return None
    except OSError:  # e.g. broken pipe: do not leak the child process
        proc.kill()
        proc.wait()
        return None
    return parse_ebur128(err)


def audio_track_count(path: str) -> int:
    """Number of audio tracks (positions 0..n-1), NOT ffprobe stream index.

    ``ffprobe -select_streams a -show_entries stream=index`` prints one
    line per audio stream; missing ffprobe or any failure yields 1.
    """
    if shutil.which("ffprobe") is None:
        return 1
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 1
    if proc.returncode != 0:
        return 1
    return sum(1 for line in proc.stdout.splitlines() if line.strip())


def compute_gain(
    lufs: float,
    peak_db: float | None,
    target: float,
    max_boost: float = MAX_BOOST_DB,
    ceiling: float = CEILING_DB,
    max_attenuate: float = MAX_ATTENUATE_DB,
) -> float:
    """Static gain (dB) aligning *lufs* to *target*, with clipping guard.

    Spec D5: a source whose true peak is at/above the ceiling can only be
    attenuated, and the result is clamped to [max_attenuate, max_boost].
    """
    gain = target - lufs
    if peak_db is not None:
        gain = min(gain, ceiling - peak_db)
    return max(max_attenuate, min(max_boost, gain))


def pending_files(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    stop_event: threading.Event | None = None,
) -> list[tuple[str, int, int, float]]:
    """(path, track_pos, size, mtime) entries that need measurement.

    For every song the file is statted (missing files are skipped) and the
    enumeration stops early when *stop_event* is set. A file is a candidate
    when *force* is set, when it has no stored loudness rows, or when any
    stored row's (size, mtime) snapshot differs from the current one --
    including rows with ok=0 (a deliberate failure whose snapshot matches
    is NOT retried). For candidates, one entry is emitted per track
    position lacking a snapshot-matching row (all positions when *force*).
    A file with no audio tracks at all yields one placeholder entry for
    track 0: the worker records an ok=0 row with a real snapshot, so it is
    final and never pends again.
    """
    pending: list[tuple[str, int, int, float]] = []
    for (path,) in conn.execute("SELECT path FROM songs"):
        if stop_event is not None and stop_event.is_set():
            break
        try:
            st = os.stat(path)
        except OSError:
            continue
        size, mtime = st.st_size, st.st_mtime
        rows = conn.execute(
            "SELECT track_pos, size, mtime FROM loudness WHERE path = ?",
            (path,),
        ).fetchall()
        if force:
            count = audio_track_count(path)
        else:
            if rows and all(r[1] == size and r[2] == mtime for r in rows):
                continue
            count = audio_track_count(path)
        if count == 0:
            pending.append((path, 0, size, mtime))
            continue
        if force:
            positions = range(count)
        else:
            matching = {r[0] for r in rows if r[1] == size and r[2] == mtime}
            positions = (p for p in range(count) if p not in matching)
        for pos in positions:
            pending.append((path, pos, size, mtime))
    return pending


def _measure_path(path, positions, stop_event):
    """Measure all pending track positions of one file.

    Returns {pos: LoudnessResult | None} (a None value = genuine measurement
    failure for that track), or None when *stop_event* fired mid-way so the
    caller must discard the partial result and leave prior rows untouched.
    """
    out = {}
    for pos in positions:
        if stop_event is not None and stop_event.is_set():
            return None
        result = measure_file(path, pos, 120.0, stop_event)
        if stop_event is not None and stop_event.is_set() and result is None:
            return None  # killed by stop, not a real failure
        out[pos] = result
    return out


class _LoudnessSignals(QObject):
    """Qt signals owned by the worker; emitted from the worker thread."""

    progress = Signal(int, int)  # done, total
    finished = Signal()
    error = Signal(str)


class LoudnessWorker(threading.Thread):
    """Measure loudness for pending songs on a daemon thread.

    Mirrors ``scanner.SizeBackfillWorker``: it opens its OWN SQLite
    connection (the GUI thread's connection must stay on the GUI thread),
    commits every ``_FLUSH_EVERY`` completions so a re-run resumes where it
    left off, and never raises -- failures surface via the ``error``
    signal instead.
    """

    _FLUSH_EVERY = 200

    _INSERT = (
        "INSERT OR REPLACE INTO loudness "
        "(path, track_pos, lufs, peak_db, duration, size, mtime, ok, "
        "measured_at, tool) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def __init__(
        self,
        db_path: str,
        workers: int = 8,
        target: float = -11.25,
        force: bool = False,
    ) -> None:
        super().__init__(daemon=True)
        self.db_path = db_path
        # 0 = auto: half the cores (spec 8.1), never fewer than 1
        self.workers = workers or max(1, (os.cpu_count() or 2) // 2)
        self.target = target  # applied at playback time (see compute_gain)
        self.force = force
        self._stop = threading.Event()
        self._signals = _LoudnessSignals()

    @property
    def progress(self):
        return self._signals.progress

    @property
    def finished(self):
        return self._signals.finished

    @property
    def error(self):
        return self._signals.error

    def stop(self) -> None:
        """Ask the worker to stop; in-flight ffmpeg is killed (resumable)."""
        self._stop.set()

    def isRunning(self) -> bool:  # noqa: N802 - QThread-compatible name
        return self.is_alive()

    def wait(self, ms: int = 5000) -> bool:  # noqa: A003 - QThread-compatible name
        self.join(ms / 1000.0)
        return not self.is_alive()

    def run(self) -> None:
        try:
            if shutil.which("ffmpeg") is None:
                self._signals.error.emit(tr("未找到 ffmpeg，无法测量响度"))
                self._signals.finished.emit()
                return
            conn = sqlite3.connect(self.db_path)
            try:
                conn.executescript(SCHEMA)
                conn.commit()
                # (0, 0) means "enumerating"; (0, total) follows below
                self._signals.progress.emit(0, 0)
                items = pending_files(
                    conn, force=self.force, stop_event=self._stop
                )
                total = len(items)
                self._signals.progress.emit(0, total)
                # One future per path: all pending tracks of a file are
                # measured and written atomically, so a stop() can never
                # leave a partial row set (or ok=0 poison rows) behind.
                expected: dict[str, list[int]] = {}
                for path, pos, _size, _mtime in items:
                    expected.setdefault(path, []).append(pos)
                done = 0
                pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, self.workers)
                )
                try:
                    futures = {}
                    for path, positions in expected.items():
                        positions.sort()
                        futures[
                            pool.submit(
                                _measure_path, path, positions, self._stop
                            )
                        ] = (path, positions)
                    for future in concurrent.futures.as_completed(futures):
                        if self._stop.is_set():
                            break  # never consume a stopped future
                        path, positions = futures[future]
                        try:
                            results = future.result()
                        except Exception:
                            results = None
                        if results is None:
                            # Killed by stop (or a hard error): leave the
                            # row(s) untouched so the file re-pends.
                            done += len(positions)
                            self._signals.progress.emit(done, total)
                            continue
                        try:
                            st = os.stat(path)
                            size, mtime = st.st_size, st.st_mtime
                        except OSError:
                            size, mtime = None, None  # file vanished mid-run
                        if size is not None:
                            # A replaced file may carry fewer audio tracks:
                            # drop stale rows (file changed, or track count
                            # shrank) while preserving valid rows for
                            # positions not measured this run.
                            conn.execute(
                                "DELETE FROM loudness WHERE path = ? "
                                "AND NOT (size = ? AND mtime = ?)",
                                (path, size, mtime),
                            )
                        for pos in positions:
                            result = results[pos]
                            row = LoudnessRow(
                                path=path,
                                track_pos=pos,
                                lufs=result.lufs if result else None,
                                peak_db=result.peak_db if result else None,
                                duration=result.duration if result else None,
                                size=size,
                                mtime=mtime,
                                ok=result is not None,
                                measured_at=time.time(),
                                tool="ebur128",
                            )
                            conn.execute(
                                self._INSERT,
                                (
                                    row.path,
                                    row.track_pos,
                                    row.lufs,
                                    row.peak_db,
                                    row.duration,
                                    row.size,
                                    row.mtime,
                                    int(row.ok),
                                    row.measured_at,
                                    row.tool,
                                ),
                            )
                        prev_done = done
                        done += len(positions)
                        if done // self._FLUSH_EVERY > prev_done // self._FLUSH_EVERY:
                            conn.commit()
                        self._signals.progress.emit(done, total)
                finally:
                    # wait=True: no pool thread (non-daemon) or ffmpeg
                    # process may survive stop()
                    pool.shutdown(wait=True, cancel_futures=True)
                conn.commit()
            finally:
                conn.close()
            self._signals.finished.emit()
        except Exception as e:  # noqa: BLE001 - workers must never raise
            self._signals.error.emit(str(e))
            self._signals.finished.emit()


def _ensure_qapplication():
    """A QApplication exists so queued worker signals are delivered.

    Falls back to the offscreen platform when no display is available.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        return app
    try:
        return QApplication([])
    except Exception:  # noqa: BLE001 - e.g. no display server on a headless box
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        return QApplication([])


def _pump_until_done(worker: LoudnessWorker) -> None:
    """Pump the event loop until *worker* finishes; Ctrl-C stops it.

    Progress is a queued cross-thread signal, so the loop must keep
    processing events for the prints to appear.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    try:
        while worker.isRunning():
            if app is not None:
                app.processEvents()
            time.sleep(0.01)
    except KeyboardInterrupt:
        worker.stop()
        while worker.isRunning():
            if app is not None:
                app.processEvents()
            time.sleep(0.01)
    if app is not None:
        for _ in range(100):  # flush the final queued progress/finished signals
            app.processEvents()


def _db_path_from(args: argparse.Namespace) -> str:
    from ezkaraoke.config import load_config
    from ezkaraoke.database import DEFAULT_DB_PATH

    if args.db:
        return args.db
    return load_config().db_path or str(DEFAULT_DB_PATH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ezkaraoke.loudness",
        description=(
            "Measure EBU R128 loudness for every song (read-only) and store "
            "it in the song database."
        ),
    )
    parser.add_argument(
        "--scan",
        metavar="PATH",
        help="rescan PATH, rebuild the library, then measure pending files",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="print loudness statistics and the pending count (no measurement)",
    )
    parser.add_argument(
        "--workers", type=int, default=8, help="parallel ffmpeg measurements"
    )
    parser.add_argument(
        "--target",
        type=float,
        default=-11.25,
        help="target loudness in LUFS (informational here; applied at playback)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore stored snapshots and re-measure everything",
    )
    parser.add_argument(
        "--db", metavar="PATH", help="database path (default: config or standard)"
    )
    args = parser.parse_args(argv)

    if bool(args.scan) == bool(args.stats):
        parser.error("exactly one of --scan or --stats is required")

    db_path = _db_path_from(args)

    if args.stats:
        from ezkaraoke.database import SongDatabase

        db = SongDatabase(db_path)
        stats = db.loudness_stats()
        pending = db.loudness_pending()
        db.close()
        def fmt(v):
            return f"{v:.2f}" if v is not None else "n/a"

        print(
            "loudness rows: "
            f"{stats['count']} (ok {stats['ok']}, failed {stats['failed']})"
        )
        print(
            "lufs min/median/max: "
            f"{fmt(stats['min'])} / {fmt(stats['median'])} / {fmt(stats['max'])}"
        )
        print(f"pending files: {len(pending)}")
        return 0

    from ezkaraoke.database import SongDatabase
    from ezkaraoke.scanner import scan_folder

    if not os.path.isdir(args.scan):
        print(f"error: folder not found: {args.scan}", file=sys.stderr)
        return 2

    songs = scan_folder(args.scan)
    db = SongDatabase(db_path)
    db.rebuild(songs)
    db.close()  # the worker opens its own connection
    print(f"scanned {len(songs)} songs in {args.scan}")

    _ensure_qapplication()
    worker = LoudnessWorker(
        db_path, workers=args.workers, target=args.target, force=args.force
    )
    errors: list[str] = []
    worker.error.connect(lambda message: errors.append(message))
    worker.progress.connect(
        lambda done, total: print(f"measuring loudness {done}/{total}", flush=True)
    )
    worker.start()
    _pump_until_done(worker)
    if errors:
        print(f"error: {errors[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
