"""SQLite-backed song database (fast search/index, persisted across runs)."""

from __future__ import annotations

import sqlite3
import time
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
"""


def _row_to_song(row: tuple) -> Song:
    return Song(artist=row[0], title=row[1], path=row[2], size=row[3])


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

    def delete_song(self, path: str) -> None:
        """Remove one song; artists left without songs are dropped too."""
        with self._conn:
            self._conn.execute("DELETE FROM songs WHERE path = ?", (str(path),))
            self._conn.execute("DELETE FROM favorites WHERE path = ?", (str(path),))
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

    def close(self) -> None:
        self._conn.close()
