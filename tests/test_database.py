"""Tests for ezkaraoke.database (SQLite song store)."""

from ezkaraoke.database import SongDatabase
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
