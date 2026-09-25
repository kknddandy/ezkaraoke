"""SQLite-backed song database (fast search/index, persisted across runs)."""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ezkaraoke import paths
from ezkaraoke.letters import LETTERS, compute_letter
from ezkaraoke.library import Song

DEFAULT_DB_PATH = paths.database_file()
AVATAR_RETRY_SECONDS = 86400  # failed avatar fetches may be retried after this

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    path TEXT PRIMARY KEY,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    letter TEXT NOT NULL,
    size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist);
CREATE INDEX IF NOT EXISTS idx_songs_letter ON songs(letter);
CREATE TABLE IF NOT EXISTS artists (
    name TEXT PRIMARY KEY,
    avatar BLOB,
    avatar_tried INTEGER NOT NULL DEFAULT 0,
    avatar_tried_at REAL,
    normalized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS favorites (
    path TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS loudness (
    path         TEXT    NOT NULL,
    track_pos    INTEGER NOT NULL DEFAULT 0,
    lufs         REAL,
    peak_db      REAL,
    duration     REAL,
    size         INTEGER,
    mtime        REAL,
    ok           INTEGER NOT NULL DEFAULT 1,
    measured_at  REAL    NOT NULL,
    tool         TEXT    NOT NULL DEFAULT 'ebur128',
    PRIMARY KEY (path, track_pos)
);
CREATE INDEX IF NOT EXISTS idx_loudness_path ON loudness(path);
"""


def _row_to_song(row: tuple) -> Song:
    return Song(artist=row[0], title=row[1], path=row[2], size=row[3])


@dataclass(frozen=True)
class LoudnessRow:
    path: str
    track_pos: int          # 0-based audio-track ordinal (NOT ffprobe stream index)
    lufs: float | None
    peak_db: float | None
    duration: float | None
    size: int | None
    mtime: float | None
    ok: bool
    measured_at: float
    tool: str = "ebur128"


class SongDatabase:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        # Migrate pre-0.2 databases that lack the retry timestamp.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(artists)")}
        if "avatar_tried_at" not in cols:
            self._conn.execute("ALTER TABLE artists ADD COLUMN avatar_tried_at REAL")
        # Migrate pre-0.3 databases that lack the normalization flag.
        if "normalized" not in cols:
            self._conn.execute(
                "ALTER TABLE artists ADD COLUMN normalized INTEGER NOT NULL DEFAULT 0"
            )
        song_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(songs)")}
        if "size" not in song_cols:
            self._conn.execute("ALTER TABLE songs ADD COLUMN size INTEGER")
        self._conn.commit()

    def rebuild(self, songs: list[Song]) -> None:
        """Replace the whole song set in ONE transaction.

        Avatars of surviving artists are preserved; artists that no longer
        have any song are dropped (their avatars included).
        """
        with self._conn:
            self._conn.execute("DELETE FROM songs")
            self._conn.executemany(
                "INSERT OR REPLACE INTO songs VALUES (?, ?, ?, ?, ?)",
                [
                    (s.path, s.artist, s.title, compute_letter(s.title), s.size)
                    for s in songs
                ],
            )
            self._conn.execute(
                "DELETE FROM artists WHERE name NOT IN "
                "(SELECT DISTINCT artist FROM songs)"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO artists (name) "
                "SELECT DISTINCT artist FROM songs"
            )
            self._conn.execute(
                "DELETE FROM favorites WHERE path NOT IN (SELECT path FROM songs)"
            )
            self._conn.execute(
                "DELETE FROM loudness WHERE path NOT IN (SELECT path FROM songs)"
            )

    def delete_song(self, path: str) -> None:
        """Remove one song; artists left without songs are dropped too."""
        with self._conn:
            self._conn.execute("DELETE FROM songs WHERE path = ?", (str(path),))
            self._conn.execute("DELETE FROM favorites WHERE path = ?", (str(path),))
            self._conn.execute("DELETE FROM loudness WHERE path = ?", (str(path),))
            self._conn.execute(
                "DELETE FROM artists WHERE name NOT IN "
                "(SELECT DISTINCT artist FROM songs)"
            )

    def song_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM songs").fetchone()
        return row[0]

    def all_songs(self) -> list[Song]:
        rows = self._conn.execute(
            "SELECT artist, title, path, size FROM songs "
            "ORDER BY artist, title, path"
        ).fetchall()
        return [_row_to_song(r) for r in rows]

    def count_missing_sizes(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM songs WHERE size IS NULL"
        ).fetchone()
        return row[0]

    def search(self, query: str) -> list[Song]:
        """Empty query -> all songs; else artist LIKE %q% OR title LIKE %q%.

        LIKE is case-insensitive for ASCII in SQLite.
        """
        q = query.strip()
        if not q:
            return self.all_songs()
        pattern = f"%{q}%"
        rows = self._conn.execute(
            "SELECT artist, title, path, size FROM songs "
            "WHERE artist LIKE ? OR title LIKE ? ORDER BY artist, title, path",
            (pattern, pattern),
        ).fetchall()
        return [_row_to_song(r) for r in rows]

    def get_song(self, path: str) -> Song | None:
        row = self._conn.execute(
            "SELECT artist, title, path, size FROM songs WHERE path = ?", (path,)
        ).fetchone()
        return _row_to_song(row) if row is not None else None

    def is_favorite(self, path: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM favorites WHERE path = ?", (str(path),)
        ).fetchone()
        return row is not None

    def toggle_favorite(self, path: str) -> bool:
        """Toggle a song's favorite flag; returns the new state."""
        if self.is_favorite(path):
            self._conn.execute("DELETE FROM favorites WHERE path = ?", (str(path),))
            new_state = False
        else:
            self._conn.execute(
                "INSERT OR IGNORE INTO favorites VALUES (?)", (str(path),)
            )
            new_state = True
        self._conn.commit()
        return new_state

    def favorite_songs(self) -> list[Song]:
        """All favorited songs in library order (artist, title, path)."""
        rows = self._conn.execute(
            "SELECT s.artist, s.title, s.path, s.size FROM favorites f "
            "JOIN songs s ON s.path = f.path "
            "ORDER BY s.artist, s.title, s.path"
        ).fetchall()
        return [_row_to_song(r) for r in rows]

    def artists(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM artists ORDER BY name").fetchall()
        return [r[0] for r in rows]

    def artist_counts(self) -> list[tuple[str, int]]:
        """(artist, song_count) pairs in one query, ordered by artist."""
        rows = self._conn.execute(
            "SELECT artist, COUNT(*) FROM songs GROUP BY artist ORDER BY artist"
        ).fetchall()
        return [(name, count) for name, count in rows]

    def songs_by_artist(self, artist: str) -> list[Song]:
        rows = self._conn.execute(
            "SELECT artist, title, path, size FROM songs WHERE artist = ? "
            "ORDER BY title, path",
            (artist,),
        ).fetchall()
        return [_row_to_song(r) for r in rows]

    def songs_by_letter(self, letter: str) -> list[Song]:
        rows = self._conn.execute(
            "SELECT artist, title, path, size FROM songs WHERE letter = ? "
            "ORDER BY artist, title, path",
            (letter,),
        ).fetchall()
        return [_row_to_song(r) for r in rows]

    def letters(self) -> list[tuple[str, int]]:
        counts = dict(
            self._conn.execute(
                "SELECT letter, COUNT(*) FROM songs GROUP BY letter"
            ).fetchall()
        )
        return [(letter, counts.get(letter, 0)) for letter in LETTERS]

    def get_avatar(self, name: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT avatar FROM artists WHERE name = ?", (name,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return bytes(row[0])

    def set_avatar(self, name: str, data: bytes) -> None:
        # Callers hand over data that already went through
        # normalize_avatar (see fetch_artist_avatar), so it can be
        # flagged normalized: future render passes skip re-decoding it.
        self._conn.execute(
            "INSERT INTO artists (name, avatar, avatar_tried, avatar_tried_at, normalized) "
            "VALUES (?, ?, 1, ?, 1) "
            "ON CONFLICT(name) DO UPDATE SET avatar = excluded.avatar, "
            "avatar_tried = 1, avatar_tried_at = excluded.avatar_tried_at, "
            "normalized = 1",
            (name, data, time.time()),
        )
        self._conn.commit()

    def mark_avatar_tried(self, name: str) -> None:
        self._conn.execute(
            "INSERT INTO artists (name, avatar_tried, avatar_tried_at) VALUES (?, 1, ?) "
            "ON CONFLICT(name) DO UPDATE SET avatar_tried = 1, "
            "avatar_tried_at = excluded.avatar_tried_at",
            (name, time.time()),
        )
        self._conn.commit()

    def artists_without_avatar(self) -> list[str]:
        """Artists to fetch: never tried, or failed long enough ago to retry."""
        cutoff = time.time() - AVATAR_RETRY_SECONDS
        rows = self._conn.execute(
            "SELECT name FROM artists WHERE avatar IS NULL "
            "AND (avatar_tried = 0 OR COALESCE(avatar_tried_at, 0) < ?) "
            "ORDER BY name",
            (cutoff,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_loudness(self, path: str) -> list[LoudnessRow]:
        """Loudness rows for a path, ordered by track_pos ascending."""
        rows = self._conn.execute(
            "SELECT path, track_pos, lufs, peak_db, duration, size, mtime, ok, "
            "measured_at, tool FROM loudness WHERE path = ? ORDER BY track_pos",
            (str(path),),
        ).fetchall()
        return [
            LoudnessRow(
                path=r[0],
                track_pos=r[1],
                lufs=r[2],
                peak_db=r[3],
                duration=r[4],
                size=r[5],
                mtime=r[6],
                ok=bool(r[7]),
                measured_at=r[8],
                tool=r[9],
            )
            for r in rows
        ]

    def put_loudness(self, rows: list[LoudnessRow]) -> None:
        """Insert or replace loudness rows in one executemany."""
        if not rows:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO loudness VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r.path,
                    r.track_pos,
                    r.lufs,
                    r.peak_db,
                    r.duration,
                    r.size,
                    r.mtime,
                    int(r.ok),
                    r.measured_at,
                    r.tool,
                )
                for r in rows
            ],
        )
        self._conn.commit()

    def mark_loudness_failed(self, path: str, track_pos: int) -> None:
        """Record a measurement failure so it is not retried every run.

        Stores the current size/mtime snapshot so the row counts as
        attempted for this file version; a missing file stores NULLs.
        """
        try:
            st = os.stat(path)
            size: int | None = st.st_size
            mtime: float | None = st.st_mtime
        except OSError:
            size = None
            mtime = None
        self._conn.execute(
            "INSERT OR REPLACE INTO loudness "
            "(path, track_pos, lufs, peak_db, duration, size, mtime, ok, "
            "measured_at, tool) VALUES (?, ?, NULL, NULL, NULL, ?, ?, 0, ?, ?)",
            (str(path), track_pos, size, mtime, time.time(), "ebur128"),
        )
        self._conn.commit()

    def loudness_pending(self) -> list[tuple[str, int]]:
        """(path, size) of songs without a loudness row for their current size.

        DB-only: a stored row matching the song's size snapshot means the
        file version was already attempted (including deliberate failures);
        a size change makes the song pending again.
        """
        rows = self._conn.execute(
            "SELECT s.path, s.size FROM songs s "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM loudness l WHERE l.path = s.path "
            "AND (s.size IS NULL OR l.size = s.size)"
            ") ORDER BY s.path"
        ).fetchall()
        return [(path, size) for path, size in rows]

    def loudness_stats(self) -> dict:
        """count/ok/failed plus min/median/max lufs over ok rows."""
        count, ok, failed = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(ok), 0), "
            "COALESCE(SUM(1 - ok), 0) FROM loudness"
        ).fetchone()
        values = [
            r[0]
            for r in self._conn.execute(
                "SELECT lufs FROM loudness "
                "WHERE ok = 1 AND lufs IS NOT NULL ORDER BY lufs"
            ).fetchall()
        ]
        median: float | None = None
        if values:
            n = len(values)
            mid = n // 2
            median = (values[mid - 1] + values[mid]) / 2 if n % 2 == 0 else values[mid]
        return {
            "count": count,
            "ok": ok,
            "failed": failed,
            "min": values[0] if values else None,
            "median": median,
            "max": values[-1] if values else None,
        }

    def clear_loudness(self) -> None:
        self._conn.execute("DELETE FROM loudness")
        self._conn.commit()

    def prune_loudness(self) -> int:
        """Drop rows for paths no longer in songs; returns the deleted count."""
        cursor = self._conn.execute(
            "DELETE FROM loudness WHERE path NOT IN (SELECT path FROM songs)"
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()
