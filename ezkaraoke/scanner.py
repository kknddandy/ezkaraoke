"""Scan a folder tree for video files and parse artist/title from filenames."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ezkaraoke.library import Song

VIDEO_EXTS: set[str] = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v",
    ".mpg", ".mpeg", ".webm", ".rmvb", ".rm", ".3gp",
}


def parse_filename(name: str) -> tuple[str, str]:
    """Parse a bare filename (with extension) into (artist, title).

    Split on the FIRST "-" for the artist, then the FIRST "-" of the
    remainder for the title; anything after that is discarded.
    A missing artist becomes "未知歌手".
    """
    stem = Path(name).stem
    if "-" in stem:
        artist, rest = stem.split("-", 1)
        if "-" in rest:
            title, _ = rest.split("-", 1)
        else:
            title = rest
    else:
        artist = ""
        title = stem
    if not artist:
        artist = "未知歌手"
    return artist, title


def parse_version(name: str) -> str:
    """Version label from a ``歌手-歌名-版本`` filename.

    Everything after the second "-" is the version (e.g. 伴奏, 现场版,
    1080p); empty when the filename has no third segment.
    """
    stem = Path(name).stem
    parts = stem.split("-", 2)
    if len(parts) < 3:
        return ""
    return parts[2].strip()


def file_size(path: str) -> int | None:
    """st_size in bytes, or None if the file is missing/unreadable."""
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def scan_folder(root: str) -> list[Song]:
    """Recursively scan *root* for video files.

    Walks with sorted dirs/files for deterministic output. Extension
    matching is case-insensitive. Each song carries its file size.
    """
    songs: list[Song] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for filename in filenames:
            ext = Path(filename).suffix.lower()
            if ext not in VIDEO_EXTS:
                continue
            path = os.path.join(dirpath, filename)
            artist, title = parse_filename(filename)
            songs.append(
                Song(artist=artist, title=title, path=path, size=file_size(path))
            )
    return songs


class _ScanSignals(QObject):
    """Qt signals owned by the worker; emitted from the worker thread."""

    finished = Signal(list)
    error = Signal(str)


class ScanWorker(threading.Thread):
    """Background scanner on a daemon thread.

    Emits ``finished(list[Song])`` or ``error(str)``. Daemon, like the
    avatar worker: it can never block window close or process exit.
    """

    def __init__(self, root: str) -> None:
        super().__init__(daemon=True)
        self.root = root
        self._stop = threading.Event()
        self._signals = _ScanSignals()

    @property
    def finished(self):
        return self._signals.finished

    @property
    def error(self):
        return self._signals.error

    def stop(self) -> None:
        """Ask the worker to drop its result once done (walk cannot be interrupted)."""
        self._stop.set()

    def isRunning(self) -> bool:  # noqa: N802 - QThread-compatible name
        return self.is_alive()

    def wait(self, ms: int = 5000) -> bool:  # noqa: A003 - QThread-compatible name
        self.join(ms / 1000.0)
        return not self.is_alive()

    def run(self) -> None:
        try:
            songs = scan_folder(self.root)
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            if not self._stop.is_set():
                self._signals.error.emit(str(e))
            return
        if not self._stop.is_set():
            self._signals.finished.emit(songs)


class _SizeBackfillSignals(QObject):
    """Qt signals owned by the worker; emitted from the worker thread."""

    done = Signal()


class SizeBackfillWorker(threading.Thread):
    """Fill in missing song file sizes for a pre-existing database.

    Runs on a daemon thread so it never blocks window close or process
    exit. It opens its OWN read-write SQLite connection (the GUI thread's
    connection must stay on the GUI thread) and only stats paths whose
    ``size`` is still NULL, so a re-run is cheap and resumable.
    """

    _FLUSH_EVERY = 500

    def __init__(self, db_path: str) -> None:
        super().__init__(daemon=True)
        self.db_path = db_path
        self._stop = threading.Event()
        self._signals = _SizeBackfillSignals()

    @property
    def done(self):
        return self._signals.done

    def stop(self) -> None:
        self._stop.set()

    def isRunning(self) -> bool:  # noqa: N802 - QThread-compatible name
        return self.is_alive()

    def wait(self, ms: int = 5000) -> bool:  # noqa: A003 - QThread-compatible
        self.join(ms / 1000.0)
        return not self.is_alive()

    def run(self) -> None:
        import sqlite3

        try:
            conn = sqlite3.connect(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT path FROM songs WHERE size IS NULL"
                ).fetchall()
                pending: list[tuple[int, str]] = []
                for (path,) in rows:
                    if self._stop.is_set():
                        break
                    size = file_size(path)
                    if size is None:
                        continue
                    pending.append((size, path))
                    if len(pending) >= self._FLUSH_EVERY:
                        conn.executemany(
                            "UPDATE songs SET size = ? WHERE path = ?", pending
                        )
                        conn.commit()
                        pending = []
                if pending:
                    conn.executemany(
                        "UPDATE songs SET size = ? WHERE path = ?", pending
                    )
                    conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        if not self._stop.is_set():
            self._signals.done.emit()
