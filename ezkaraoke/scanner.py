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


def scan_folder(root: str) -> list[Song]:
    """Recursively scan *root* for video files.

    Walks with sorted dirs/files for deterministic output. Extension
    matching is case-insensitive.
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
            artist, title = parse_filename(filename)
            songs.append(
                Song(artist=artist, title=title, path=os.path.join(dirpath, filename))
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
