"""Tests for ezkaraoke.library (no Qt, no libvlc required)."""

from ezkaraoke.library import Library, Song


def make(path: str, artist: str, title: str) -> Song:
    return Song(artist=artist, title=title, path=path)


def test_song_display():
    assert make("/a/b.mp4", "周杰伦", "晴天").display == "周杰伦 - 晴天"


def test_load_and_len():
    lib = Library()
    assert len(lib) == 0
    lib.load([
        make("/m/1.mp4", "B", "b1"),
        make("/m/2.mp4", "A", "a1"),
        make("/m/3.mp4", "B", "b2"),
    ])
    assert len(lib) == 3


def test_load_dedup_replaces_by_path():
    lib = Library()
    lib.load([
        make("/m/1.mp4", "old-artist", "old-title"),
        make("/m/1.mp4", "new-artist", "new-title"),  # same path, later wins
    ])
    assert len(lib) == 1
    song = lib.songs[0]
    assert song.artist == "new-artist"
    assert song.title == "new-title"


def test_clear():
    lib = Library()
    lib.load([make("/m/1.mp4", "A", "a1")])
    lib.clear()
    assert len(lib) == 0
    assert lib.songs == []
    assert lib.artists == []
    assert lib.songs_by_artist("A") == []


def test_songs_sorted_order():
    lib = Library()
    lib.load([
        make("/m/3.mp4", "B", "a"),
        make("/m/1.mp4", "A", "z"),
        make("/m/2.mp4", "A", "a"),
    ])
    assert [(s.artist, s.title) for s in lib.songs] == [
        ("A", "a"), ("A", "z"), ("B", "a"),
    ]
    # tie on (artist, title) broken by path
    lib2 = Library()
    lib2.load([
        make("/m/z.mp4", "A", "t"),
        make("/m/a.mp4", "A", "t"),
    ])
    assert [s.path for s in lib2.songs] == ["/m/a.mp4", "/m/z.mp4"]


def test_artists_unique_in_song_order():
    lib = Library()
    lib.load([
        make("/m/1.mp4", "B", "a"),
        make("/m/2.mp4", "A", "z"),
        make("/m/3.mp4", "B", "b"),
        make("/m/4.mp4", "C", "c"),
    ])
    # songs sorted by artist -> artists in first-appearance order
    assert lib.artists == ["A", "B", "C"]


def test_songs_by_artist():
    lib = Library()
    lib.load([
        make("/m/1.mp4", "B", "b1"),
        make("/m/2.mp4", "A", "a1"),
        make("/m/3.mp4", "B", "b2"),
    ])
    assert [s.title for s in lib.songs_by_artist("B")] == ["b1", "b2"]
    assert [s.path for s in lib.songs_by_artist("A")] == ["/m/2.mp4"]
    assert lib.songs_by_artist("nobody") == []


def test_search_empty_query_returns_all():
    lib = Library()
    lib.load([
        make("/m/1.mp4", "A", "a1"),
        make("/m/2.mp4", "B", "b1"),
    ])
    assert len(lib.search("")) == 2
    assert len(lib.search("   ")) == 2


def test_search_artist_match():
    lib = Library()
    lib.load([
        make("/m/1.mp4", "周杰伦", "晴天"),
        make("/m/2.mp4", "陈奕迅", "浮夸"),
    ])
    assert [s.title for s in lib.search("周杰伦")] == ["晴天"]


def test_search_title_match():
    lib = Library()
    lib.load([
        make("/m/1.mp4", "周杰伦", "晴天"),
        make("/m/2.mp4", "陈奕迅", "浮夸"),
    ])
    assert [s.artist for s in lib.search("浮夸")] == ["陈奕迅"]


def test_search_case_insensitive():
    lib = Library()
    lib.load([make("/m/1.mp4", "AB Artist", "My Title")])
    assert len(lib.search("ab artist")) == 1
    assert len(lib.search("MY TITLE")) == 1
    assert len(lib.search("my ti")) == 1


def test_search_no_match():
    lib = Library()
    lib.load([make("/m/1.mp4", "A", "a1")])
    assert lib.search("zzz") == []


def test_search_matches_substring_in_either_field():
    lib = Library()
    lib.load([
        make("/m/1.mp4", "Queen", "Bohemian Rhapsody"),
        make("/m/2.mp4", "Beatles", "Hey Jude"),
    ])
    # "e" matches both artists (QuEen, BEatles)
    assert len(lib.search("e")) == 2
