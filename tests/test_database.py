"""Tests for ezkaraoke.database (SQLite song store)."""

from ezkaraoke.database import LoudnessRow, SongDatabase
from ezkaraoke.letters import LETTERS
from ezkaraoke.library import Song

SONGS = [
    Song("周杰伦", "晴天", "/m/jay-qing.mp4"),
    Song("周杰伦", "Hello", "/m/jay-hello.mp4"),
    Song("周杰伦", "稻香", "/m/jay-dao.mp4"),
    Song("林俊杰", "江南", "/m/lin-jiang.mp4"),
    Song("林俊杰", "修炼爱情", "/m/lin-lian.mp4"),
    Song("A-Band", "Hey You", "/m/aband-hey.mp4"),
]


def make_db(tmp_path) -> SongDatabase:
    return SongDatabase(tmp_path / "songs.db")


def loudness_row(
    path: str,
    track_pos: int,
    lufs: float | None = None,
    size: int | None = None,
    ok: bool = True,
) -> LoudnessRow:
    return LoudnessRow(
        path=path,
        track_pos=track_pos,
        lufs=lufs,
        peak_db=None,
        duration=None,
        size=size,
        mtime=None,
        ok=ok,
        measured_at=1.0,
    )


def test_rebuild_and_all_songs_roundtrip(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    assert db.song_count() == 6
    expected = sorted(SONGS, key=lambda s: (s.artist, s.title, s.path))
    assert db.all_songs() == expected
    db.close()


def test_rebuild_dedupes_by_path(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS + [Song("替换歌手", "新标题", "/m/jay-qing.mp4")])
    assert db.song_count() == 6
    assert all(s.path != "/m/jay-qing.mp4" or s.artist == "替换歌手" for s in db.all_songs())
    db.close()


def test_size_stored_and_roundtrip(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(
        [
            Song("甲", "歌一", "/m/jia-1.mp4", size=1024),
            Song("甲", "歌二", "/m/jia-2.mp4", size=None),
        ]
    )
    by_path = {s.path: s.size for s in db.all_songs()}
    assert by_path == {"/m/jia-1.mp4": 1024, "/m/jia-2.mp4": None}
    stored = db.get_song("/m/jia-1.mp4")
    assert stored is not None and stored.size == 1024
    db.close()


def test_count_missing_sizes(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(
        [
            Song("甲", "歌一", "/m/jia-1.mp4", size=1),
            Song("甲", "歌二", "/m/jia-2.mp4", size=None),
            Song("乙", "歌三", "/m/yi-3.mp4", size=None),
        ]
    )
    assert db.count_missing_sizes() == 2
    db.close()


def test_old_db_missing_size_column_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE songs (path TEXT PRIMARY KEY, artist TEXT NOT NULL, "
        "title TEXT NOT NULL, letter TEXT NOT NULL);"
        "INSERT INTO songs VALUES ('/m/a.mp4', '甲', '歌一', 'G');"
    )
    conn.commit()
    conn.close()
    db = SongDatabase(path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(songs)")}
    assert "size" in cols
    migrated = db.get_song("/m/a.mp4")
    assert migrated is not None and migrated.size is None
    assert db.count_missing_sizes() == 1
    db.close()


def test_letter_column_stored_and_letters_order(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    assert db.songs_by_letter("Q") == [Song("周杰伦", "晴天", "/m/jay-qing.mp4")]
    letters = db.letters()
    assert [l for l, _ in letters] == LETTERS  # A..Z, # in order, zeros included
    counts = dict(letters)
    assert counts["Q"] == 1
    assert counts["H"] == 2  # Hello + Hey You
    assert counts["D"] == 1
    assert counts["J"] == 1
    assert counts["X"] == 1
    assert counts["A"] == 0
    assert counts["#"] == 0
    db.close()


def test_search(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    assert db.search("晴") == [Song("周杰伦", "晴天", "/m/jay-qing.mp4")]
    # case-insensitive ASCII: matches title "Hello"
    assert db.search("HELLO") == [Song("周杰伦", "Hello", "/m/jay-hello.mp4")]
    # substring on raw text, artist side; ordered by (artist, title, path)
    assert [s.path for s in db.search("周杰")] == [
        "/m/jay-hello.mp4",
        "/m/jay-qing.mp4",
        "/m/jay-dao.mp4",
    ]
    assert len(db.search("")) == 6
    assert db.search("   ") == db.all_songs()
    assert db.search("不存在的歌") == []
    db.close()


def test_artists_and_songs_by_artist(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    assert db.artists() == ["A-Band", "周杰伦", "林俊杰"]
    assert db.songs_by_artist("周杰伦") == [
        Song("周杰伦", "Hello", "/m/jay-hello.mp4"),
        Song("周杰伦", "晴天", "/m/jay-qing.mp4"),
        Song("周杰伦", "稻香", "/m/jay-dao.mp4"),
    ]
    assert db.songs_by_artist("没人") == []
    db.close()


def test_rebuild_preserves_avatars_of_surviving_artists(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS + [Song("临时歌手", "临时歌", "/m/tmp.mp4")])
    db.set_avatar("周杰伦", b"img1")
    db.set_avatar("临时歌手", b"img2")
    # Rebuild without 临时歌手's song: its artist row (and avatar) is dropped.
    db.rebuild(SONGS)
    assert db.get_avatar("周杰伦") == b"img1"
    assert db.get_avatar("临时歌手") is None
    assert "临时歌手" not in db.artists()
    db.close()


def test_avatar_lifecycle(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    assert db.get_avatar("周杰伦") is None
    assert db.artists_without_avatar() == db.artists()
    db.set_avatar("周杰伦", b"img-data")
    assert "周杰伦" not in db.artists_without_avatar()
    assert db.get_avatar("周杰伦") == b"img-data"
    db.mark_avatar_tried("林俊杰")
    assert "林俊杰" not in db.artists_without_avatar()
    assert db.get_avatar("林俊杰") is None  # tried, but no avatar stored
    db.close()


def test_failed_avatar_is_retried_after_window(tmp_path):
    import time

    from ezkaraoke.database import AVATAR_RETRY_SECONDS

    db = make_db(tmp_path)
    db.rebuild([Song("周传雄", "星空", "/m/zcx.mp4")])
    db.mark_avatar_tried("周传雄")
    assert db.artists_without_avatar() == []  # within the retry window
    db._conn.execute(
        "UPDATE artists SET avatar_tried_at = ? WHERE name = '周传雄'",
        (time.time() - AVATAR_RETRY_SECONDS - 60,),
    )
    db._conn.commit()
    assert db.artists_without_avatar() == ["周传雄"]
    db.close()


def test_old_db_missing_retry_column_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE artists (name TEXT PRIMARY KEY, avatar BLOB, "
        "avatar_tried INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO artists (name) VALUES ('老歌手');"
    )
    conn.commit()
    conn.close()
    db = SongDatabase(path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(artists)")}
    assert "avatar_tried_at" in cols
    assert db.artists() == ["老歌手"]
    db.close()


def test_delete_song_removes_row_and_orphan_artist(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(
        [
            Song("甲", "歌一", "/m/jia-1.mp4"),
            Song("甲", "歌二", "/m/jia-2.mp4"),
            Song("乙", "歌三", "/m/yi-3.mp4"),
        ]
    )
    db.delete_song("/m/jia-1.mp4")
    assert db.song_count() == 2
    assert "甲" in db.artists()  # still has 歌二
    db.delete_song("/m/jia-2.mp4")
    assert db.song_count() == 1
    assert db.artists() == ["乙"]  # 甲 dropped with its last song
    # deleting a missing path is a no-op
    db.delete_song("/m/never.mp4")
    assert db.song_count() == 1
    db.close()


def test_favorites_toggle_and_library_order(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    assert db.favorite_songs() == []
    assert db.toggle_favorite("/m/jay-qing.mp4") is True
    assert db.is_favorite("/m/jay-qing.mp4")
    assert db.toggle_favorite("/m/lin-jiang.mp4") is True
    assert [s.path for s in db.favorite_songs()] == [
        "/m/jay-qing.mp4",
        "/m/lin-jiang.mp4",
    ]
    # toggling again removes the favorite
    assert db.toggle_favorite("/m/jay-qing.mp4") is False
    assert db.favorite_songs() == [Song("林俊杰", "江南", "/m/lin-jiang.mp4")]
    db.close()


def test_rebuild_drops_favorites_of_removed_songs(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    db.toggle_favorite("/m/jay-qing.mp4")
    db.toggle_favorite("/m/lin-jiang.mp4")
    db.rebuild(SONGS[:2])  # keeps only 晴天 + Hello
    assert db.favorite_songs() == [Song("周杰伦", "晴天", "/m/jay-qing.mp4")]
    db.close()


def test_delete_song_removes_favorite(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(SONGS)
    db.toggle_favorite("/m/jay-qing.mp4")
    db.delete_song("/m/jay-qing.mp4")
    assert db.favorite_songs() == []
    db.close()


def test_old_db_missing_loudness_table_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE songs (path TEXT PRIMARY KEY, artist TEXT NOT NULL, "
        "title TEXT NOT NULL, letter TEXT NOT NULL, size INTEGER);"
        "CREATE TABLE artists (name TEXT PRIMARY KEY, avatar BLOB, "
        "avatar_tried INTEGER NOT NULL DEFAULT 0, avatar_tried_at REAL, "
        "normalized INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE favorites (path TEXT PRIMARY KEY);"
        "INSERT INTO songs VALUES ('/m/a.mp4', '甲', '歌一', 'G', 100);"
    )
    conn.commit()
    conn.close()
    db = SongDatabase(path)
    info = db._conn.execute("PRAGMA table_info(loudness)").fetchall()
    assert info, "loudness table missing"
    pk = {name: pos for _, name, _, _, _, pos in info if pos}
    assert pk == {"path": 1, "track_pos": 2}
    assert db.get_song("/m/a.mp4") is not None  # existing rows survive
    db.close()


def test_loudness_put_get_roundtrip(tmp_path):
    rows = [
        LoudnessRow(
            path="/m/jay-qing.mp4",
            track_pos=1,
            lufs=-11.83,
            peak_db=-2.7,
            duration=263.5,
            size=5000,
            mtime=1700000.5,
            ok=True,
            measured_at=1700001.0,
            tool="ebur128",
        ),
        LoudnessRow(
            path="/m/jay-qing.mp4",
            track_pos=0,
            lufs=None,
            peak_db=None,
            duration=None,
            size=5000,
            mtime=1700000.5,
            ok=False,
            measured_at=1700001.0,
            tool="ffmpeg",
        ),
    ]
    db = make_db(tmp_path)
    db.put_loudness(rows)
    got = db.get_loudness("/m/jay-qing.mp4")
    # ordered by track_pos ascending, all fields preserved
    assert got == [rows[1], rows[0]]
    assert all(isinstance(r.ok, bool) for r in got)
    assert got[0].ok is False and got[1].ok is True
    assert db.get_loudness("/m/missing.mp4") == []
    # empty list is a no-op
    db.put_loudness([])
    assert len(db.get_loudness("/m/jay-qing.mp4")) == 2
    db.close()


def test_loudness_pending(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(
        [
            Song("甲", "歌一", "/m/jia-1.mp4", size=1000),
            Song("乙", "歌二", "/m/yi-2.mp4", size=2000),
            Song("丙", "歌三", "/m/bing-3.mp4"),
        ]
    )
    assert db.loudness_pending() == [
        ("/m/bing-3.mp4", None),
        ("/m/jia-1.mp4", 1000),
        ("/m/yi-2.mp4", 2000),
    ]
    # a row whose size matches songs.size -> no longer pending
    db.put_loudness([loudness_row("/m/jia-1.mp4", 0, lufs=-11.0, size=1000)])
    assert [p for p, _ in db.loudness_pending()] == ["/m/bing-3.mp4", "/m/yi-2.mp4"]
    # size change -> pending again
    db._conn.execute("UPDATE songs SET size = 9999 WHERE path = '/m/jia-1.mp4'")
    db._conn.commit()
    assert [p for p, _ in db.loudness_pending()] == [
        "/m/bing-3.mp4",
        "/m/jia-1.mp4",
        "/m/yi-2.mp4",
    ]
    # ok=0 row with matching size (songs.size NULL matches) -> not retried
    db.mark_loudness_failed("/m/bing-3.mp4", 0)
    failed = db.get_loudness("/m/bing-3.mp4")
    assert len(failed) == 1
    assert failed[0].ok is False and failed[0].lufs is None
    assert [p for p, _ in db.loudness_pending()] == ["/m/jia-1.mp4", "/m/yi-2.mp4"]
    # brand-new song enters the queue
    db.rebuild(
        [
            Song("甲", "歌一", "/m/jia-1.mp4", size=9999),
            Song("乙", "歌二", "/m/yi-2.mp4", size=2000),
            Song("丙", "歌三", "/m/bing-3.mp4"),
            Song("丁", "歌四", "/m/ding-4.mp4", size=3000),
        ]
    )
    assert [p for p, _ in db.loudness_pending()] == [
        "/m/ding-4.mp4",
        "/m/jia-1.mp4",
        "/m/yi-2.mp4",
    ]
    db.close()


def test_mark_loudness_failed_stores_snapshot_and_stays_not_pending(tmp_path):
    real = tmp_path / "a.mp4"
    real.write_bytes(b"x" * 1234)
    db = make_db(tmp_path)
    db.rebuild(
        [
            Song("甲", "歌一", str(real), size=real.stat().st_size),
            Song("乙", "歌二", "/m/yi-2.mp4", size=2000),
        ]
    )
    db.mark_loudness_failed(str(real), 0)
    # the failed song is attempted for its version -> not retried
    assert db.loudness_pending() == [("/m/yi-2.mp4", 2000)]
    row = db.get_loudness(str(real))
    assert len(row) == 1
    r = row[0]
    assert r.ok is False
    assert r.lufs is None and r.peak_db is None and r.duration is None
    assert r.size == 1234 and r.mtime is not None
    db.close()


def test_mark_loudness_failed_missing_file_stores_nulls(tmp_path):
    db = make_db(tmp_path)
    db.rebuild([Song("甲", "歌一", "/m/missing.mp4", size=500)])
    # must not raise even though the file does not exist
    db.mark_loudness_failed("/m/missing.mp4", 0)
    row = db.get_loudness("/m/missing.mp4")
    assert len(row) == 1
    r = row[0]
    assert r.ok is False
    assert r.size is None and r.mtime is None
    db.close()


def test_loudness_stats(tmp_path):
    db = make_db(tmp_path)
    assert db.loudness_stats() == {
        "count": 0,
        "ok": 0,
        "failed": 0,
        "min": None,
        "median": None,
        "max": None,
    }
    db.put_loudness(
        [
            loudness_row("/m/a.mp4", 0, lufs=-8.0, size=1),
            loudness_row("/m/a.mp4", 1, lufs=-10.0, size=1),
            loudness_row("/m/b.mp4", 0, lufs=-12.0, size=2),
            loudness_row("/m/c.mp4", 0, ok=False),
        ]
    )
    stats = db.loudness_stats()
    assert stats["count"] == 4
    assert stats["ok"] == 3
    assert stats["failed"] == 1
    assert stats["min"] == -12.0
    assert stats["median"] == -10.0
    assert stats["max"] == -8.0
    # even count -> mean of the two middle values
    db.put_loudness([loudness_row("/m/d.mp4", 0, lufs=-6.0, size=4)])
    stats = db.loudness_stats()
    assert stats["count"] == 5
    assert stats["ok"] == 4
    assert stats["median"] == -9.0
    db.close()


def test_clear_loudness(tmp_path):
    db = make_db(tmp_path)
    db.put_loudness(
        [
            loudness_row("/m/a.mp4", 0, lufs=-10.0, size=1),
            loudness_row("/m/b.mp4", 0, ok=False),
        ]
    )
    assert db.loudness_stats()["count"] == 2
    db.clear_loudness()
    assert db.loudness_stats()["count"] == 0
    db.close()


def test_prune_loudness(tmp_path):
    db = make_db(tmp_path)
    db.rebuild([Song("甲", "歌一", "/m/keep.mp4", size=1)])
    db.put_loudness(
        [
            loudness_row("/m/keep.mp4", 0, lufs=-10.0, size=1),
            loudness_row("/m/gone-1.mp4", 0, lufs=-11.0, size=2),
            loudness_row("/m/gone-2.mp4", 1, lufs=-12.0, size=3),
        ]
    )
    assert db.prune_loudness() == 2
    assert [r.path for r in db.get_loudness("/m/keep.mp4")] == ["/m/keep.mp4"]
    assert db.get_loudness("/m/gone-1.mp4") == []
    # nothing left to prune
    assert db.prune_loudness() == 0
    db.close()


def test_rebuild_drops_loudness_of_removed_songs(tmp_path):
    db = make_db(tmp_path)
    db.rebuild(
        [
            Song("甲", "歌一", "/m/jia-1.mp4", size=100),
            Song("乙", "歌二", "/m/yi-2.mp4", size=200),
        ]
    )
    db.put_loudness(
        [
            loudness_row("/m/jia-1.mp4", 0, lufs=-10.0, size=100),
            loudness_row("/m/yi-2.mp4", 0, lufs=-11.0, size=200),
        ]
    )
    db.rebuild([Song("甲", "歌一", "/m/jia-1.mp4", size=100)])
    assert db.get_loudness("/m/yi-2.mp4") == []
    assert len(db.get_loudness("/m/jia-1.mp4")) == 1
    db.close()


def test_delete_song_removes_loudness_rows(tmp_path):
    db = make_db(tmp_path)
    db.rebuild([Song("甲", "歌一", "/m/jia-1.mp4", size=100)])
    db.put_loudness(
        [
            loudness_row("/m/jia-1.mp4", 0, lufs=-10.0, size=100),
            loudness_row("/m/jia-1.mp4", 1, lufs=-11.0, size=100),
        ]
    )
    db.delete_song("/m/jia-1.mp4")
    assert db.get_loudness("/m/jia-1.mp4") == []
    db.close()
