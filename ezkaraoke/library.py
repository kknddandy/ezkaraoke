"""Song model and in-memory song library."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Song:
    artist: str
    title: str
    path: str

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}"


class Library:
    """In-memory song collection with an artist index."""

    def __init__(self) -> None:
        self._songs: list[Song] = []
        self._by_artist: dict[str, list[Song]] = {}

    def load(self, songs: list[Song]) -> None:
        """Replace contents with *songs* (duplicates by path: last one wins)."""
        by_path: dict[str, Song] = {}
        for song in songs:
            by_path[song.path] = song
        self._songs = sorted(
            by_path.values(), key=lambda s: (s.artist, s.title, s.path)
        )
        self._by_artist: dict[str, list[Song]] = {}
        for song in self._songs:
            self._by_artist.setdefault(song.artist, []).append(song)

    def clear(self) -> None:
        self._songs = []
        self._by_artist = {}

    def __len__(self) -> int:
        return len(self._songs)

    @property
    def songs(self) -> list[Song]:
        """All songs, sorted by (artist, title, path)."""
        return list(self._songs)

    @property
    def artists(self) -> list[str]:
        """Unique artists, in the order they appear in :attr:`songs`."""
        result: list[str] = []
        seen: set[str] = set()
        for song in self._songs:
            if song.artist not in seen:
                seen.add(song.artist)
                result.append(song.artist)
        return result

    def songs_by_artist(self, artist: str) -> list[Song]:
        return list(self._by_artist.get(artist, []))

    def search(self, query: str) -> list[Song]:
        """Case-insensitive substring match on artist OR title.

        Empty/whitespace-only query returns all songs.
        """
        q = query.strip().lower()
        if not q:
            return list(self._songs)
        return [
            song
            for song in self._songs
            if q in song.artist.lower() or q in song.title.lower()
        ]
