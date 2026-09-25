"""Tests for ezkaraoke.player queue logic.

These run with NO libvlc installed and NO display: vlc.Instance() fails on
this machine, so all playback is a no-op while queue bookkeeping and
signals must still work.
"""

import os

# Offscreen platform: no display server is available on this machine.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtWidgets import QApplication

from ezkaraoke.database import LoudnessRow, SongDatabase
from ezkaraoke.library import Song
from ezkaraoke.player import PlayerController


@pytest.fixture(scope="session")
def qapp():
    # A GUI app instance is required, not a bare QCoreApplication:
    # PySide6 keeps the first-created app instance alive for the whole
    # process, and widget creation aborts if that instance is not a
    # QApplication (the UI smoke test creates widgets later in the same
    # session). offscreen keeps this headless, and libvlc is never touched.
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def make(artist: str = "A", title: str = "T", path: str | None = None) -> Song:
    return Song(artist=artist, title=title, path=path or f"/music/{artist}-{title}.mp4")


class FakePlayer:
    """Mimics libvlc's real track semantics.

    ``count`` is the number of *real* audio tracks. The description list
    includes the -1 "Disable" pseudo-entry and real track IDs start at 1
    (a two-track file reports IDs 1 and 2, and track_count() is 3).
    """

    def __init__(self, count: int = 0, length: int = -1, position: int = -1) -> None:
        self.count = count
        self.set_calls: list[int] = []
        self.current: int = 1 if count else -1
        self.rates: list[float] = []
        self.length = length
        self.position = position
        self.equalizers: list = []
        self.seeks: list[int] = []
        # stand-in for python-vlc's _Ctype ctypes protocol attribute
        self._as_parameter_ = object()

    def stop(self) -> None:
        pass

    def play(self) -> None:
        pass

    def set_media(self, media) -> None:
        pass

    def set_rate(self, rate: float) -> None:
        self.rates.append(float(rate))

    def audio_get_track_count(self) -> int:
        return self.count + 1 if self.count else 0

    def audio_get_track(self) -> int:
        return self.current

    def audio_get_track_description(self) -> list[tuple[int, bytes]]:
        if not self.count:
            return []
        return [(-1, b"Disable")] + [
            (i + 1, f"Track {i + 1}".encode()) for i in range(self.count)
        ]

    def audio_set_track(self, i_track: int) -> int:
        self.set_calls.append(i_track)
        if 1 <= i_track <= self.count:
            self.current = i_track
            return 0
        return -1

    def audio_set_equalizer(self, eq) -> int:
        self.equalizers.append(eq)
        return 0

    def get_length(self) -> int:
        return self.length

    def get_time(self) -> int:
        return self.position

    def set_time(self, ms: int) -> int:
        self.seeks.append(ms)
        self.position = ms
        return 0

    def event_manager(self) -> "_FakeEventManager":
        return _FakeEventManager()


class _FakeEventManager:
    """No-op stand-in for the libvlc event manager (attach/detach)."""

    def event_attach(self, *args, **kwargs) -> None:
        pass

    def event_detach(self, *args, **kwargs) -> None:
        pass


def paths(queue: list[Song]) -> list[str]:
    return [s.path for s in queue]


# --------------------------------------------------------------------- basics
def test_initial_state(qapp):
    p = PlayerController()
    assert p.queue == []
    assert p.current_index == -1
    assert p.current_song is None
    assert not p.is_playing
    assert not p.is_paused


# --------------------------------------------------------------------- append
def test_append_ordering(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    assert p.append(s1) == 0
    assert p.append(s2) == 1
    assert p.append(s3) == 2
    assert paths(p.queue) == [s1.path, s2.path, s3.path]


def test_append_duplicate_returns_existing_index(qapp):
    p = PlayerController()
    s1 = make("A", "a1")
    p.append(s1)
    p.append(make("B", "b1"))
    assert p.append(Song("x", "y", s1.path)) == 0  # same path -> existing index
    assert len(p.queue) == 2


# -------------------------------------------------------------- insert_next
def test_insert_next_at_current_plus_one(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(1)  # current = s2 at index 1
    s4 = make("D", "d1")
    assert p.insert_next(s4) == 2
    assert paths(p.queue) == [s1.path, s2.path, s4.path, s3.path]


def test_insert_next_when_nothing_current(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    assert p.insert_next(make("C", "c1")) == 0
    assert paths(p.queue)[0] == "/music/C-c1.mp4"


def test_insert_next_duplicate_moves_existing(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(0)  # current = s1 at index 0 -> insert pos 1
    # duplicate of s3 (currently at index 2) moves to index 1
    assert p.insert_next(Song("c", "C", s3.path)) == 1
    assert paths(p.queue) == [s1.path, s3.path, s2.path]
    assert len(p.queue) == 3


def test_insert_next_duplicate_before_current_regression(qapp):
    # Repro: queue [A, B, C] with B playing (index 1). insert_next(A)
    # moves A behind B; current_index must follow B to index 0, not keep
    # pointing at the old slot (which now holds A).
    p = PlayerController()
    a, b, c = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (a, b, c):
        p.append(s)
    p.play_at(1)  # current = b at index 1
    changed: list[int] = []
    p.current_changed.connect(changed.append)
    assert p.insert_next(Song("a", "A", a.path)) == 1
    assert paths(p.queue) == [b.path, a.path, c.path]
    assert p.current_index == 0
    assert p.current_song is b
    # current_changed emitted exactly once, carrying the new index
    assert changed == [0]


# ------------------------------------------------------------------ play_now
def test_play_now_removes_existing_copy(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_now(Song("b", "B", s2.path))  # existing copy of s2
    assert paths(p.queue) == [s2.path, s1.path, s3.path]
    assert p.current_index == 0
    # the queue holds the Song object that was passed in (same path)
    assert p.current_song.path == s2.path
    assert p.is_playing


def test_play_now_new_song_goes_to_front(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.play_at(1)
    s3 = make("C", "c1")
    p.play_now(s3)
    assert paths(p.queue) == [s3.path, s1.path, s2.path]
    assert p.current_index == 0
    assert p.current_song is s3


# ------------------------------------------------------------------- play_at
def test_play_at(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(2)
    assert p.current_index == 2
    assert p.current_song is s3
    assert p.is_playing
    # out-of-range is a no-op
    p.play_at(99)
    p.play_at(-1)
    assert p.current_index == 2


# ---------------------------------------------------------------- next / prev
def test_next_advances(qapp):
    p = PlayerController()
    for i in range(3):
        p.append(make("A", f"a{i}"))
    p.play_at(0)
    p.next()
    assert p.current_index == 1
    p.next()
    assert p.current_index == 2


def test_next_at_end_stops(qapp):
    p = PlayerController()
    for i in range(2):
        p.append(make("A", f"a{i}"))
    p.play_at(1)
    p.next()  # nothing after index 1
    assert p.current_index == -1
    assert not p.is_playing
    assert not p.is_paused
    # the queue is kept
    assert len(p.queue) == 2
    assert p.current_song is None


def test_next_from_stopped_plays_first(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.next()  # stopped + non-empty queue -> play index 0
    assert p.current_index == 0
    assert p.current_song is s1


def test_next_from_stopped_empty_queue_noop(qapp):
    p = PlayerController()
    p.next()
    assert p.current_index == -1


def test_prev_goes_back(qapp):
    p = PlayerController()
    for i in range(3):
        p.append(make("A", f"a{i}"))
    p.play_at(2)
    p.prev()
    assert p.current_index == 1


def test_prev_at_zero_restarts_current(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.play_at(0)
    p.prev()  # at 0 -> restart current
    assert p.current_index == 0
    assert p.current_song is s1


def test_prev_from_stopped_noop(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.prev()
    assert p.current_index == -1


# ----------------------------------------------------------------- remove_at
def test_remove_current_advances_to_next(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(0)
    p.remove_at(0)  # remove current -> former next (s2) now plays at index 0
    assert paths(p.queue) == [s2.path, s3.path]
    assert p.current_index == 0
    assert p.current_song is s2


def test_remove_current_from_single_song_stops(qapp):
    p = PlayerController()
    s1 = make("A", "a1")
    p.append(s1)
    p.play_at(0)
    p.remove_at(0)
    assert p.queue == []
    assert p.current_index == -1
    assert not p.is_playing


def test_remove_non_current_before_current_shifts_index(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(2)  # current s3 at index 2
    p.remove_at(0)
    assert paths(p.queue) == [s2.path, s3.path]
    assert p.current_index == 1
    assert p.current_song is s3


def test_remove_non_current_after_current_keeps_index(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(0)
    p.remove_at(2)
    assert paths(p.queue) == [s1.path, s2.path]
    assert p.current_index == 0
    assert p.current_song is s1


def test_remove_out_of_range_noop(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.remove_at(5)
    p.remove_at(-1)
    assert len(p.queue) == 1


# ----------------------------------------------------------------------- move
def test_move_clamps(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.move(0, -5)   # clamped to 0 -> no-op
    assert paths(p.queue) == [s1.path, s2.path, s3.path]
    p.move(2, 99)   # clamped to 2 -> no-op
    assert paths(p.queue) == [s1.path, s2.path, s3.path]
    p.move(0, 2)    # s1 to the end
    assert paths(p.queue) == [s2.path, s3.path, s1.path]
    p.move(2, -2)   # s1 back to the front
    assert paths(p.queue) == [s1.path, s2.path, s3.path]


def test_move_current_song_follows(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(0)  # current s1 at index 0
    p.move(0, 2)
    assert paths(p.queue) == [s2.path, s3.path, s1.path]
    assert p.current_index == 2
    assert p.current_song is s1


def test_move_crossing_current_shifts_index(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(0)  # current s1 at index 0
    p.move(2, -2)  # s3 jumps to front: [s3, s1, s2]
    assert paths(p.queue) == [s3.path, s1.path, s2.path]
    assert p.current_index == 1
    assert p.current_song is s1


def test_move_crossing_current_both_directions_4_songs(qapp):
    p = PlayerController()
    s1, s2, s3, s4 = (
        make("A", "a1"), make("B", "b1"), make("C", "c1"), make("D", "d1")
    )
    for s in (s1, s2, s3, s4):
        p.append(s)
    p.play_at(1)  # current = s2 at index 1
    changed: list[int] = []
    p.current_changed.connect(changed.append)

    # Song BEFORE current (s1, index 0) jumps past it to index 2:
    # [s1, s2, s3, s4] -> [s2, s3, s1, s4]; current s2 shifts 1 -> 0
    p.move(0, 2)
    assert paths(p.queue) == [s2.path, s3.path, s1.path, s4.path]
    assert p.current_index == 0
    assert p.current_song is s2

    # Song AFTER current (s4, index 3) jumps past it to index 0:
    # [s2, s3, s1, s4] -> [s4, s2, s3, s1]; current s2 shifts 0 -> 1
    p.move(3, -3)
    assert paths(p.queue) == [s4.path, s2.path, s3.path, s1.path]
    assert p.current_index == 1
    assert p.current_song is s2

    assert changed == [0, 1]


def test_move_empty_queue_noop(qapp):
    p = PlayerController()
    p.move(0, 1)
    assert p.queue == []



# ----------------------------------------------------------------- move_rows
def test_move_rows_consecutive_block_shifts_as_unit(qapp):
    s1, s2, s3, s4, s5 = (
        make("A", "a1"), make("B", "b1"), make("C", "c1"),
        make("D", "d1"), make("E", "e1"),
    )
    p = PlayerController()
    for s in (s1, s2, s3, s4, s5):
        p.append(s)
    refreshes = []
    p.queue_changed.connect(lambda: refreshes.append(1))

    # Rows 2,3,4 form one block: it shifts up by one as a unit and the
    # row above (s2) drops to the block's old end.
    p.move_rows([2, 3, 4], -1)
    assert paths(p.queue) == [s1.path, s3.path, s4.path, s5.path, s2.path]
    assert refreshes == [1]  # one refresh no matter how many rows moved

    p = PlayerController()
    for s in (s1, s2, s3, s4, s5):
        p.append(s)
    # Block at rows 0,1 shifts down by one; the row below (s3) rises.
    p.move_rows([0, 1], 1)
    assert paths(p.queue) == [s3.path, s1.path, s2.path, s4.path, s5.path]


def test_move_rows_non_consecutive_rows(qapp):
    p = PlayerController()
    s1, s2, s3, s4, s5 = (
        make("A", "a1"), make("B", "b1"), make("C", "c1"),
        make("D", "d1"), make("E", "e1"),
    )
    for s in (s1, s2, s3, s4, s5):
        p.append(s)
    p.move_rows([1, 3], -1)  # each isolated row swaps with its neighbor
    assert paths(p.queue) == [s2.path, s1.path, s4.path, s3.path, s5.path]
    p.move_rows([1, 3], 1)
    assert paths(p.queue) == [s2.path, s4.path, s1.path, s5.path, s3.path]


def test_move_rows_boundary_noop_emits_nothing(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    events = []
    p.queue_changed.connect(lambda: events.append("q"))
    p.current_changed.connect(lambda i: events.append("c"))
    p.move_rows([0], -1)  # top row: already clamped
    p.move_rows([2], 1)   # bottom row: already clamped
    assert paths(p.queue) == [s1.path, s2.path, s3.path]
    assert events == []


def test_move_rows_current_inside_block_follows(qapp):
    p = PlayerController()
    s1, s2, s3, s4 = (
        make("A", "a1"), make("B", "b1"), make("C", "c1"), make("D", "d1")
    )
    for s in (s1, s2, s3, s4):
        p.append(s)
    p.play_at(1)  # current s2 at index 1
    p.move_rows([1, 2], 1)  # s2,s3 block shifts down one; s4 rises to 1
    assert paths(p.queue) == [s1.path, s4.path, s2.path, s3.path]
    assert p.current_index == 2
    assert p.current_song is s2

    changed = []
    p.current_changed.connect(changed.append)
    p.move_rows([0, 1], -1)  # block touches the top edge: clamped, no move
    assert paths(p.queue) == [s1.path, s4.path, s2.path, s3.path]
    assert changed == []


# --------------------------------------------------------------- remove_rows
def test_remove_rows_multiple_non_current(qapp):
    p = PlayerController()
    s1, s2, s3, s4, s5 = (
        make("A", "a1"), make("B", "b1"), make("C", "c1"),
        make("D", "d1"), make("E", "e1"),
    )
    for s in (s1, s2, s3, s4, s5):
        p.append(s)
    p.play_at(1)  # current s2 at index 1
    p.remove_rows([3, 4])
    assert paths(p.queue) == [s1.path, s2.path, s3.path]
    assert p.current_index == 1
    assert p.current_song is s2


def test_remove_rows_including_current_continues(qapp):
    p = PlayerController()
    s1, s2, s3, s4 = (
        make("A", "a1"), make("B", "b1"), make("C", "c1"), make("D", "d1")
    )
    for s in (s1, s2, s3, s4):
        p.append(s)
    p.play_at(1)  # current s2 at index 1
    p.remove_rows([0, 1])
    # first survivor after the removed current (s3) plays immediately
    assert paths(p.queue) == [s3.path, s4.path]
    assert p.current_index == 0
    assert p.current_song is s3


def test_remove_rows_current_last_falls_back_to_last_survivor(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(2)  # current s3, the last row
    p.remove_rows([0, 2])
    # nothing survived after current: the last survivor (s2) plays
    assert paths(p.queue) == [s2.path]
    assert p.current_index == 0
    assert p.current_song is s2


def test_remove_rows_all_stops(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    for s in (s1, s2):
        p.append(s)
    p.play_at(0)
    p.remove_rows([0, 1])
    assert p.queue == []
    assert p.current_index == -1
    assert not p.is_playing

# ------------------------------------------------------ jump_after_current
def test_jump_after_current_single(qapp):
    p = PlayerController()
    s1, s2, s3, s4 = make("A", "a1"), make("B", "b1"), make("C", "c1"), make("D", "d1")
    for s in (s1, s2, s3, s4):
        p.append(s)
    p.play_at(1)  # current = s2 at index 1
    p.jump_after_current([3])  # s4 jumps right after s2
    assert paths(p.queue) == [s1.path, s2.path, s4.path, s3.path]
    assert p.current_index == 1
    assert p.current_song is s2


def test_jump_after_current_multiple_preserves_selection_order(qapp):
    p = PlayerController()
    s1, s2, s3, s4, s5 = (
        make("A", "a1"), make("B", "b1"), make("C", "c1"),
        make("D", "d1"), make("E", "e1"),
    )
    for s in (s1, s2, s3, s4, s5):
        p.append(s)
    p.play_at(1)  # current = s2
    # both s1 (before current) and s5 (after) jump behind s2, top-to-bottom
    # selection order preserved: s1 then s5
    p.jump_after_current([4, 0])
    assert paths(p.queue) == [s2.path, s1.path, s5.path, s3.path, s4.path]
    assert p.current_index == 0
    assert p.current_song is s2


def test_jump_after_current_skips_current_song(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(1)
    changed: list[int] = []
    p.current_changed.connect(changed.append)
    p.jump_after_current([1, 2])  # current + s3
    assert paths(p.queue) == [s1.path, s2.path, s3.path]
    assert p.current_index == 1
    assert p.current_song is s2
    assert changed == []  # the current song never moves


def test_jump_after_current_already_next_is_noop(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(0)
    changed: list[int] = []
    p.queue_changed.connect(lambda: changed.append(1))
    p.jump_after_current([1])  # s2 already right after current
    assert changed == []  # no spurious refresh signal
    assert paths(p.queue) == [s1.path, s2.path, s3.path]


def test_jump_after_current_nothing_playing_goes_to_front(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.jump_after_current([2])
    assert paths(p.queue) == [s3.path, s1.path, s2.path]
    assert p.current_index == -1


def test_jump_after_current_current_at_end(qapp):
    p = PlayerController()
    s1, s2, s3 = make("A", "a1"), make("B", "b1"), make("C", "c1")
    for s in (s1, s2, s3):
        p.append(s)
    p.play_at(2)  # current = s3, last
    p.jump_after_current([0])  # s1 jumps past the current song; removing
    # it before the current song shifts current_index 2 -> 1
    assert paths(p.queue) == [s2.path, s3.path, s1.path]
    assert p.current_index == 1
    assert p.current_song is s3


def test_jump_after_current_ignores_invalid_indices(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    for s in (s1, s2):
        p.append(s)
    p.play_at(0)
    p.jump_after_current([])
    p.jump_after_current([99, -1])
    assert paths(p.queue) == [s1.path, s2.path]
    assert p.current_index == 0


# -------------------------------------------------------------------- replay
def test_replay_restarts_current(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.play_at(1)
    changed: list[int] = []
    p.current_changed.connect(changed.append)
    p.replay()
    assert p.current_index == 1
    assert p.current_song is s2
    assert p.is_playing
    assert changed == [1]
    # the queue order is untouched
    assert paths(p.queue) == [s1.path, s2.path]


def test_replay_while_paused_resumes(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.play_at(0)
    p.toggle_pause()
    assert p.is_paused
    p.replay()
    assert p.is_playing


def test_replay_from_stopped_noop(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.replay()
    assert p.current_index == -1
    assert not p.is_playing


# ----------------------------------------------------------------------- stop
def test_stop_keeps_queue(qapp):
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.play_at(1)
    p.stop()
    assert paths(p.queue) == [s1.path, s2.path]  # queue kept
    assert p.current_index == -1
    assert p.current_song is None
    assert not p.is_playing
    assert not p.is_paused


def test_toggle_pause_without_vlc(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.toggle_pause()  # stopped -> no-op
    assert not p.is_playing
    p.play_at(0)
    assert p.is_playing
    p.toggle_pause()
    assert p.is_paused
    p.toggle_pause()
    assert p.is_playing


def test_state_changed_emissions(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    states: list[str] = []
    p.state_changed.connect(states.append)
    p.play_at(0)
    assert p.is_playing
    p.toggle_pause()
    assert p.is_paused
    p.toggle_pause()
    assert p.is_playing
    p.stop()
    assert not p.is_playing
    assert not p.is_paused
    p.stop()  # already stopped: no duplicate emission
    # every transition emits exactly once, in order
    assert states == ["playing", "paused", "playing", "stopped"]


# --------------------------------------------------------------- media end
def test_media_end_advances_to_next(qapp):
    # python-vlc invokes the EndReached handler as callback(event, *args);
    # the epoch is bound at attach time and forwarded verbatim.
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.play_at(0)
    assert p.current_index == 0
    p._on_media_end(object(), p._epoch)  # must not raise
    assert p.current_index == 1
    assert p.is_playing
    p.stop()


def test_media_end_at_last_song_stops(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.play_at(0)
    p._on_media_end(object(), p._epoch)  # last song ended -> stop, no raise
    assert p.current_index == -1
    assert not p.is_playing


def test_media_end_from_libvlc_thread_advances_on_main_thread(qapp):
    """EndReached arrives on a libvlc worker thread.

    The callback must only forward + emit; the actual advance (which calls
    libvlc) is delivered as a queued signal on the main thread. Before the
    fix the callback called libvlc in place and deadlocked at the end of
    the queue, freezing the whole app ("Python 停止响应").
    """
    import threading

    p = PlayerController()
    p.append(make("A", "a1"))
    p.append(make("B", "b1"))
    p.play_at(0)
    assert p.current_index == 0

    def fire():
        p._on_media_end(object(), p._epoch)  # what python-vlc invokes on its thread

    t = threading.Thread(target=fire)
    t.start()
    deadline = time.time() + 5
    while p.current_index == 0 and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    t.join(5)
    assert p.current_index == 1
    assert p.is_playing
    p.stop()


def test_media_end_slot_ignores_stale_token(qapp):
    """A queued EndReached that is processed after the user already moved
    on must not advance a second time: its bound epoch no longer matches
    the epoch of the currently loaded media."""
    p = PlayerController()
    for i in range(3):
        p.append(make("A", f"a{i}"))
    p.play_at(0)
    old = p._epoch  # end event pending for the media loaded at index 0
    p.next()  # user moved on: the newly loaded media has a newer epoch
    p._on_media_ended(old)
    assert p.current_index == 1  # stale event ignored
    p._on_media_ended(p._epoch)  # matching event still advances
    assert p.current_index == 2
    p.stop()


def test_media_end_after_stop_does_not_resume(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.play_at(0)
    old = p._epoch  # captured before stop invalidates it
    p.stop()
    p._on_media_ended(old)  # a late end event for the stopped player
    assert p.current_index == -1
    assert not p.is_playing


def test_late_end_event_after_next_is_ignored(qapp):
    """The original bug: EndReached carries no media identity, so an
    in-flight end event delivered after the user pressed next used to
    fire next() a second time (skipping a song). The epoch bound at load
    time catches it."""
    p = PlayerController()
    p.append(make("A", "a1"))
    p.append(make("B", "b1"))
    p.play_at(0)
    old = p._epoch
    p.next()  # index 1, newer epoch
    p._on_media_ended(old)  # stale end for the song at index 0
    assert p.current_index == 1  # not advanced again
    p.stop()


def test_end_ignored_when_media_not_finished(qapp):
    # A spurious EndReached (decoder / video output teardown) mid-song
    # must not skip: the position is far from the end.
    p = PlayerController()
    p.append(make("A", "a1"))
    p.append(make("B", "b1"))
    p._begin_play(0)  # bookkeeping only (no libvlc on this machine)
    p._player = FakePlayer(length=100000, position=1000)
    assert p.current_index == 0
    p._on_media_ended(p._epoch)
    assert p.current_index == 0  # only 1s in of 100s: no advance
    assert p.is_playing
    p.stop()


def test_end_advances_when_media_finished(qapp):
    # A genuine end: position within 1s of (and past 90% of) the duration
    # advances.
    p = PlayerController()
    p.append(make("A", "a1"))
    p.append(make("B", "b1"))
    p._begin_play(0)
    p._player = FakePlayer(length=100000, position=99000)
    assert p.current_index == 0
    p._on_media_ended(p._epoch)
    assert p.current_index == 1
    assert p.is_playing
    p.stop()


def test_playlist_drains_to_stopped_without_freezing(qapp, tmp_path):
    """End-to-end freeze regression: play two short real files to the end.

    The queue must drain to stopped while the Qt main loop keeps handling
    events. Before the fix the EndReached handler called libvlc from the
    libvlc event thread (libvlc_media_player_stop), which blocked itself
    and dragged the main thread down with it — the app froze with a
    "Python 停止响应" dialog at the end of the playlist.
    """
    import shutil
    import subprocess

    if not _libvlc_available():
        pytest.skip("libvlc not available")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    from PySide6.QtCore import QTimer

    from ezkaraoke.frame_bridge import FrameBridge

    files = []
    for name in ("p1", "p2"):
        f = tmp_path / f"{name}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "mpeg4", "-q:v", "8", "-c:a", "aac", "-shortest",
                str(f),
            ],
            check=True,
        )
        files.append(f)

    p = PlayerController()
    p.append(Song("A", "p1", str(files[0])))
    p.append(Song("B", "p2", str(files[1])))
    bridge = FrameBridge()  # same software video path the app uses headless
    p.attach_video_callbacks(*bridge.video_callbacks, *bridge.format_callbacks)
    p.play_at(0)

    beats = {"n": 0}
    hb = QTimer()
    hb.timeout.connect(lambda: beats.__setitem__("n", beats["n"] + 1))
    hb.start(100)
    deadline = time.time() + 25
    while p.current_index != -1 and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    hb.stop()
    assert p.current_index == -1, "queue did not drain to stopped"
    assert not p.is_playing
    assert beats["n"] > 10, "main loop starved while the playlist played"


# ------------------------------------------------------------ set_video_output
def _libvlc_available() -> bool:
    try:
        import vlc

        vlc.Instance()
        return True
    except Exception:
        return False


def test_set_video_output(qapp):
    # Must never raise; outcome depends on whether libvlc is present.
    p = PlayerController()
    messages = []
    p.status_message.connect(messages.append)
    assert p.set_video_output(12345) is _libvlc_available()
    assert p.set_video_output(12345) is _libvlc_available()
    if _libvlc_available():
        assert messages == []
    else:
        # warning emitted exactly once
        assert len(messages) == 1
        assert "VLC" in messages[0]


def test_play_methods_noop_without_libvlc(qapp):
    # Queue bookkeeping still works when libvlc is missing.
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.play_at(0)     # no exception even though vlc.Instance() fails
    assert p.current_index == 0
    p.next()
    assert p.current_index == 1
    p.stop()
    assert p.current_index == -1


# ------------------------------------------------------ position & seek
def test_position_and_length_without_player(qapp):
    p = PlayerController()
    assert p.playback_position_ms() == -1
    assert p.media_length_ms() == -1
    assert p.can_seek() is False
    p.seek_to_ms(12345)  # no-op without a player, must not raise
    p.seek_to_ms(-1)


def test_position_and_length_unknown(qapp):
    p = PlayerController()
    p._player = FakePlayer(length=-1, position=-1)
    assert p.playback_position_ms() == -1
    assert p.media_length_ms() == -1
    assert p.can_seek() is False


def test_position_and_length_pass_through(qapp):
    p = PlayerController()
    p._player = FakePlayer(length=60000, position=15000)
    assert p.playback_position_ms() == 15000
    assert p.media_length_ms() == 60000
    assert p.can_seek() is True


def test_seek_to_ms_passes_through_and_clamps(qapp):
    p = PlayerController()
    fake = FakePlayer(length=60000, position=15000)
    p._player = fake
    p.seek_to_ms(30000)
    assert fake.seeks == [30000]
    p.seek_to_ms(-5000)  # a negative seek clamps to 0
    assert fake.seeks == [30000, 0]
    assert fake.position == 0


def test_seek_to_ms_never_raises(qapp):
    class BrokenPlayer:
        def set_time(self, ms: int) -> None:
            raise RuntimeError("boom")

    p = PlayerController()
    p._player = BrokenPlayer()
    p.seek_to_ms(1000)  # libvlc failures must stay silent
    p.playback_position_ms()
    p.media_length_ms()


# -------------------------------------------------------- audio track toggle
def test_toggle_noop_without_vlc(qapp):
    p = PlayerController()
    emissions: list[int] = []
    p.audio_track_changed.connect(emissions.append)
    p.toggle_audio_track()  # no _player -> no-op, no raise
    assert emissions == []
    assert p.audio_track_index == 0


def test_toggle_noop_single_track(qapp):
    p = PlayerController()
    p._player = FakePlayer(1)
    emissions: list[int] = []
    p.audio_track_changed.connect(emissions.append)
    p.toggle_audio_track()
    assert emissions == []
    assert p._player.set_calls == []
    assert p.audio_track_index == 0


def test_toggle_switches_tracks_and_emits(qapp):
    p = PlayerController()
    p._player = FakePlayer(2)
    emissions: list[int] = []
    messages: list[str] = []
    p.audio_track_changed.connect(emissions.append)
    p.status_message.connect(messages.append)

    p.toggle_audio_track()
    assert emissions == [1]
    assert any("伴奏" in m for m in messages)
    assert p._player.set_calls == [2]  # real track ID of the 2nd track
    assert p.audio_track_index == 1

    p.toggle_audio_track()
    assert emissions == [1, 0]
    assert any("原唱" in m for m in messages)
    assert p._player.set_calls == [2, 1]  # back to the 1st track's ID
    assert p.audio_track_index == 0


def test_toggle_ignores_failed_set(qapp):
    class FailingPlayer(FakePlayer):
        def audio_set_track(self, i_track: int) -> int:
            self.set_calls.append(i_track)
            return -1

    p = PlayerController()
    p._player = FailingPlayer(2)
    emissions: list[int] = []
    p.audio_track_changed.connect(emissions.append)
    p.toggle_audio_track()
    assert emissions == []
    assert p.audio_track_index == 0


def test_has_multi_audio_track(qapp):
    p = PlayerController()
    assert p.has_multi_audio_track() is False
    p._player = FakePlayer(2)
    assert p.has_multi_audio_track() is True
    p._player = FakePlayer(1)
    assert p.has_multi_audio_track() is False
    p._player = FakePlayer(0)
    assert p.has_multi_audio_track() is False


def test_sync_audio_track_applies_preferred(qapp):
    p = PlayerController()
    p._player = FakePlayer(2)
    p._audio_track_index = 1
    p._current_index = 0
    emissions: list[int] = []
    p.audio_track_changed.connect(emissions.append)
    p._sync_audio_track(0)
    assert p._player.set_calls == [2]  # preferred index 1 -> real ID 2
    assert emissions == [1]
    assert p.audio_track_index == 1


def test_sync_audio_track_clamps_single_track(qapp):
    p = PlayerController()
    p._player = FakePlayer(1)
    p._audio_track_index = 1
    p._current_index = 0
    emissions: list[int] = []
    p.audio_track_changed.connect(emissions.append)
    p._sync_audio_track(0)
    assert p._player.set_calls == []
    assert emissions == [0]
    assert p.audio_track_index == 0


def test_sync_audio_track_pending_when_not_ready(qapp):
    p = PlayerController()
    p._player = FakePlayer(0)
    p._current_index = 0
    emissions: list[int] = []
    p.audio_track_changed.connect(emissions.append)
    p._sync_audio_track(0)  # schedules a bounded retry timer; do NOT run events
    assert emissions == []


# ------------------------------------------------------------------ pitch
def test_pitch_default_zero(qapp):
    p = PlayerController()
    assert p.pitch_semitones == 0
    assert p.pitch_is_pure in (True, False)  # depends on runtime libvlc; no crash


def test_set_pitch_clamps(qapp):
    p = PlayerController()
    p._player = FakePlayer()
    emissions: list[int] = []
    p.pitch_changed.connect(emissions.append)
    p.set_pitch(99)
    assert p.pitch_semitones == 12
    assert emissions == [12]
    p.set_pitch(-99)
    assert p.pitch_semitones == -12
    assert emissions == [12, -12]


def test_pitch_fallback_uses_rate(qapp):
    # No real vlc here: _pitch_fn is None, so set_rate (tape speed) is used.
    p = PlayerController()
    p._player = FakePlayer()
    assert p._pitch_fn is None
    emissions: list[int] = []
    p.pitch_changed.connect(emissions.append)
    p.set_pitch(1)
    assert p._player.rates == pytest.approx([2.0 ** (1 / 12)])
    p.change_pitch(1)
    assert p._player.rates == pytest.approx([2.0 ** (1 / 12), 2.0 ** (2 / 12)])
    p.set_pitch(2)  # unchanged (already 2 after change_pitch): no re-apply
    assert p._player.rates == pytest.approx([2.0 ** (1 / 12), 2.0 ** (2 / 12)])
    # no duplicate emission: exactly [1, 2] over the whole sequence
    assert emissions == [1, 2]


def test_pitch_uses_pitch_fn_when_available(qapp):
    p = PlayerController()
    p._player = FakePlayer()
    recorder: list[float] = []
    # pct arrives as ctypes.c_float; .value (float(pct) is gone on 3.14)
    p._pitch_fn = lambda mp, pct: recorder.append(pct.value)
    p.set_pitch(-2)
    assert recorder == pytest.approx([100.0 * 2.0 ** (-2 / 12)])
    assert p._player.rates == []  # fallback not used when pitch fn is available


def test_pitch_reapplied_on_new_media(qapp, monkeypatch):
    from ezkaraoke import pitchshift as ps

    monkeypatch.setattr(ps, "shift_available", lambda: False)

    class FakeVlc:
        def media_new(self, path: str):
            return object()

    p = PlayerController()
    p._player = FakePlayer(2)
    p._vlc = FakeVlc()
    p.set_pitch(3)
    p.play_now(make("A", "t"))
    # _start_vlc resets the player; the preferred pitch must be reapplied.
    # (the pending singleShot timer is harmless; do NOT run the event loop)
    assert p._player.rates[-1] == pytest.approx(2.0 ** (3 / 12))


def test_pitch_fallback_without_ffmpeg_uses_rate(qapp, monkeypatch):
    from ezkaraoke import pitchshift as ps

    monkeypatch.setattr(ps, "shift_available", lambda: False)
    p = PlayerController()
    p._player = FakePlayer()
    p.set_pitch(1)
    assert p._player.rates == pytest.approx([2.0 ** (1 / 12)])


def test_resolve_play_path_prefers_ready_cache(qapp, monkeypatch, tmp_path):
    from ezkaraoke import pitchshift as ps

    monkeypatch.setattr(ps, "shift_available", lambda: True)
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    p = PlayerController()
    p._pitch_semitones = 3
    src = "/music/A-t.mp4"
    cache = ps.shifted_file_path(src, 3)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"variant")
    path, needs = p._resolve_play_path(src)
    assert path == str(cache)
    assert needs is False
    cache.unlink()
    path, needs = p._resolve_play_path(src)
    assert path == src
    assert needs is True


def test_resolve_play_path_original_at_semitones_zero(qapp):
    p = PlayerController()
    path, needs = p._resolve_play_path("/music/A-t.mp4")
    assert path == "/music/A-t.mp4"
    assert needs is False


def test_apply_pitch_starts_shift_worker(qapp, monkeypatch, tmp_path):
    import time

    from ezkaraoke import pitchshift as ps

    monkeypatch.setattr(ps, "shift_available", lambda: True)
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    calls: list[tuple[str, "object", int]] = []

    def fake_shift(src, dest, semis, progress_cb=None, cancel_event=None):
        calls.append((src, dest, semis))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"variant")
        return True

    monkeypatch.setattr(ps, "shift_audio", fake_shift)

    class FakeVlc:
        def __init__(self):
            self.media_paths: list[str] = []

        def media_new(self, path: str):
            self.media_paths.append(path)
            return object()

    p = PlayerController()
    p._player = FakePlayer()
    vlc = FakeVlc()
    p._vlc = vlc
    p.play_now(make("A", "t"))
    assert vlc.media_paths == ["/music/A-t.mp4"]

    statuses: list[str] = []
    p.pitch_status.connect(statuses.append)
    p.set_pitch(2)
    assert statuses == ["shifting"]
    worker = p._shift_worker
    assert worker is not None
    assert worker.wait(5000)
    deadline = time.time() + 5
    while p._shift_worker is not None and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert p._shift_worker is None
    assert statuses == ["shifting", ""]
    assert calls and calls[0][0] == "/music/A-t.mp4" and calls[0][2] == 2
    # the finished variant is hot-swapped in (original was still playing)
    assert vlc.media_paths[-1] == str(ps.shifted_file_path("/music/A-t.mp4", 2))
    assert p._active_path == vlc.media_paths[-1]


def test_shift_finished_ignored_when_song_moved_on(qapp, monkeypatch, tmp_path):
    from ezkaraoke import pitchshift as ps

    monkeypatch.setattr(ps, "shift_available", lambda: True)
    monkeypatch.setattr(ps, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ps, "shift_audio", lambda *a, **k: True)

    class FakeVlc:
        def __init__(self):
            self.media_paths: list[str] = []

        def media_new(self, path: str):
            self.media_paths.append(path)
            return object()

    p = PlayerController()
    p._player = FakePlayer()
    vlc = FakeVlc()
    p._vlc = vlc
    p.play_now(make("A", "t"))
    p.play_now(make("B", "u"))
    src = "/music/A-t.mp4"
    cache = ps.shifted_file_path(src, 2)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"variant")
    p._active_path = src
    p._on_shift_finished(src, 2)  # late callback for the old song
    assert vlc.media_paths[-1] == "/music/B-u.mp4"  # no swap happened


def test_new_media_disables_hardware_decoding(qapp):
    """h264 software decode: VLC's VA-API surface pool on Wayland/i965
    exhausts and spams get_buffer() failures (and can freeze video), so
    every media we build must carry :avcodec-hw=none."""

    class FakeMedia:
        def __init__(self, path: str) -> None:
            self.path = path
            self.options: list[str] = []

        def add_option(self, option: str) -> None:
            self.options.append(option)

    class FakeVlc:
        def media_new(self, path: str) -> "FakeMedia":
            return FakeMedia(path)

    p = PlayerController()
    p._vlc = FakeVlc()
    media = p._new_media("/music/a.mp4")
    assert media.path == "/music/a.mp4"
    assert ":avcodec-hw=none" in media.options


# ------------------------------------------------------------------ loudness
class _FakeEq:
    """Stand-in for vlc.AudioEqualizer capturing set_preamp."""

    def set_preamp(self, db: float) -> None:
        self.preamp = float(db)


@pytest.fixture
def loudness(qapp, tmp_path, monkeypatch):
    """PlayerController wired to a real SongDatabase plus a fake
    vlc.AudioEqualizer. Returns (controller, db, eqs) where *eqs* collects
    every fake equalizer handed to the player. The module-level ``import
    vlc`` inside _apply_loudness_gain resolves the real module, so patching
    its AudioEqualizer attribute is sufficient."""
    vlc = pytest.importorskip("vlc")  # libvlc is installed on this machine
    eqs: list[_FakeEq] = []

    def fake_eq() -> _FakeEq:
        eq = _FakeEq()
        eqs.append(eq)
        return eq

    monkeypatch.setattr(vlc, "AudioEqualizer", fake_eq)

    db = SongDatabase(tmp_path / "lib.db")
    p = PlayerController()
    p._player = FakePlayer(2)
    p._db = db
    song = make("A", "t")
    p._queue = [song]
    p._current_index = 0
    p._active_path = song.path
    return p, db, eqs


def _row(path: str, lufs: float, peak_db: float | None,
         track_pos: int = 0) -> LoudnessRow:
    return LoudnessRow(
        path=path, track_pos=track_pos, lufs=lufs, peak_db=peak_db,
        duration=None, size=None, mtime=None, ok=True, measured_at=0.0,
    )


def test_loudness_gain_applied(loudness):
    p, db, eqs = loudness
    assert p.loudness_enabled is True
    assert p.loudness_target == pytest.approx(-11.25)
    db.put_loudness([_row(p._queue[0].path, -20.0, None)])
    p._apply_loudness_gain()
    assert len(eqs) == 1
    assert eqs[0].preamp == pytest.approx(8.75)  # -11.25 - (-20.0)


def test_loudness_clipping_guard_attenuates(loudness):
    # True peak already above the -1.0 dB ceiling: only attenuation allowed,
    # gain = min(8.75, -1.0 - 2.7) = -3.7
    p, db, eqs = loudness
    db.put_loudness([_row(p._queue[0].path, -20.0, 2.7)])
    p._apply_loudness_gain()
    assert eqs[0].preamp == pytest.approx(-3.7)


def test_loudness_disabled_zeroes_preamp(loudness):
    p, db, eqs = loudness
    db.put_loudness([_row(p._queue[0].path, -20.0, None)])
    p.set_loudness_enabled(False)  # immediate; off -> preamp 0
    assert p.loudness_enabled is False
    assert eqs[0].preamp == pytest.approx(0.0)


def test_loudness_no_db_zeroes_preamp(loudness):
    p, db, eqs = loudness
    db.put_loudness([_row(p._queue[0].path, -20.0, None)])
    p.attach_database(None)  # None disables loudness alignment silently
    p._apply_loudness_gain()
    assert eqs[0].preamp == pytest.approx(0.0)


def test_loudness_variant_falls_back_to_source_path(loudness):
    # A pitch-shift cache variant is a different path with no measurement of
    # its own: the row of the original song must be used instead.
    p, db, eqs = loudness
    src = p._queue[0].path
    p._active_path = "/cache/shifted-variant.mp4"  # no row for this path
    db.put_loudness([_row(src, -20.0, None)])
    p._apply_loudness_gain()
    assert eqs[0].preamp == pytest.approx(8.75)  # fallback row applied


def test_toggle_audio_track_reapplies_gain(loudness):
    # Switching tracks must hand the player a NEW equalizer carrying the
    # preamp of the newly active track's row (not the previous track's).
    p, db, eqs = loudness
    path = p._queue[0].path
    db.put_loudness([
        _row(path, -20.0, None, track_pos=0),
        _row(path, -10.0, None, track_pos=1),
    ])
    p._apply_loudness_gain()  # index 0 -> row 0
    assert p.audio_track_index == 0
    assert eqs[-1].preamp == pytest.approx(8.75)  # -11.25 - (-20.0)
    before = len(eqs)
    p.toggle_audio_track()
    assert p.audio_track_index == 1
    assert len(eqs) > before  # a fresh equalizer was set on the toggle
    assert eqs[-1].preamp == pytest.approx(-1.25)  # -11.25 - (-10.0), row 1


def test_sync_audio_track_applies_on_retry_exhaust(loudness, monkeypatch):
    # libvlc reports no tracks yet and every retry exhausts: the final
    # (exhaustion) attempt must still apply the loudness gain, closing the
    # flat-EQ window at song start.
    from PySide6.QtCore import QTimer

    class NoTrackPlayer(FakePlayer):
        def audio_get_track_description(self) -> list[tuple[int, bytes]]:
            return []

    p, db, eqs = loudness
    p._player = NoTrackPlayer(2)
    db.put_loudness([_row(p._queue[0].path, -20.0, None)])

    def single_shot(ms: int, fn, *args, **kwargs) -> None:
        fn()  # run the 200 ms retry immediately

    monkeypatch.setattr(QTimer, "singleShot", single_shot)
    p._sync_audio_track(9)  # attempt 9 schedules 10, which runs and exhausts
    assert len(eqs) >= 1
    assert eqs[-1].preamp == pytest.approx(8.75)  # exhaustion apply happened


def test_loudness_row_lookup_clamps_track_index(loudness):
    # Only a row for track_pos 0 exists while the preferred index is 5:
    # the lookup must clamp to row 0 (no IndexError, preamp = row 0 gain).
    p, db, eqs = loudness
    db.put_loudness([_row(p._queue[0].path, -20.0, None)])
    p._audio_track_index = 5
    p._apply_loudness_gain()
    assert eqs[-1].preamp == pytest.approx(8.75)  # row 0's gain


# ---------------------------------------------------------------------- mic
class _FakeMicMixer:
    """Records stream-control calls; ``start_ok`` configures start().

    No real audio device is involved: start() flips an internal flag and
    is_running() reports it, mirroring MicMixer's contract.
    """

    def __init__(self, start_ok: bool = True) -> None:
        self.start_ok = start_ok
        self.running = False
        self.started = 0
        self.stopped = 0
        self.mutes: list[bool] = []
        self.params: dict[str, float] = {}

    def start(self) -> bool:
        self.started += 1
        if not self.start_ok:
            return False
        self.running = True
        return True

    def stop(self) -> None:
        self.stopped += 1
        self.running = False

    def set_muted(self, muted: bool) -> None:
        self.mutes.append(bool(muted))

    def is_running(self) -> bool:
        return self.running

    def set_gain_db(self, db: float) -> None:
        self.params["gain_db"] = float(db)

    def set_echo(self, amount: float) -> None:
        self.params["echo"] = float(amount)

    def set_bass_db(self, db: float) -> None:
        self.params["bass_db"] = float(db)

    def set_treble_db(self, db: float) -> None:
        self.params["treble_db"] = float(db)


def test_mic_starts_on_play_and_stops_on_stop(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)
    p.set_mic_enabled(True)  # stopped: no stream yet, just a stop() no-op
    p.play_at(0)
    assert fake.started == 1
    assert fake.running is True
    assert fake.mutes == [False]  # unmuted on start
    p.stop()
    assert fake.stopped >= 1
    assert fake.running is False


def test_mic_not_restarted_between_songs(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.append(make("B", "b1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)
    p.set_mic_enabled(True)
    p.play_at(0)
    p.next()  # state stays "playing": the stream must NOT restart
    assert fake.started == 1
    assert p.current_index == 1
    assert p.is_playing
    p.stop()
    assert fake.stopped >= 1
    assert fake.running is False


def test_mic_mutes_on_pause_resumes_on_play(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)
    p.set_mic_enabled(True)
    p.play_at(0)
    stopped_before_pause = fake.stopped
    p.toggle_pause()
    assert fake.mutes == [False, True]
    # pause mutes; it does NOT close the stream (no new stop() on pause)
    assert fake.stopped == stopped_before_pause
    assert fake.running is True
    p.toggle_pause()
    assert fake.mutes == [False, True, False]
    assert fake.started == 1  # resume does not restart the stream
    p.stop()


def test_mic_start_failure_is_tolerated(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    fake = _FakeMicMixer(start_ok=False)
    p.attach_mic_mixer(fake)
    p.set_mic_enabled(True)
    p.play_at(0)  # must not raise even though the mic cannot start
    assert p.is_playing  # playback continues without the mic
    assert fake.started == 1
    assert fake.mutes == []  # never (un)muted when the stream never opened
    p.stop()


def test_mic_disabled_never_starts(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)
    p.set_mic_enabled(False)
    p.play_at(0)
    assert fake.started == 0
    assert fake.mutes == []
    assert p.is_playing
    # enabling mid-play starts the stream...
    p.set_mic_enabled(True)
    assert fake.started == 1
    assert fake.mutes == [False]
    # ...and disabling while running stops it
    p.set_mic_enabled(False)
    assert fake.stopped >= 1
    assert fake.running is False
    p.stop()


def test_configure_mic_applies_params(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)  # injected first: configure_mic must reuse it
    p.configure_mic(
        enabled=True,
        gain_db=6.0,
        echo=0.5,
        bass_db=3.0,
        treble_db=-2.0,
        device="",
    )
    assert p.mic_mixer is fake
    assert fake.params == {
        "gain_db": 6.0,
        "echo": 0.5,
        "bass_db": 3.0,
        "treble_db": -2.0,
    }
    assert p._mic_enabled is True


def test_mic_closes_at_end_of_queue(qapp):
    # Advancing past the last song stops playback; the mic stream must close.
    p = PlayerController()
    p.append(make("A", "a1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)
    p.set_mic_enabled(True)
    p.play_at(0)
    assert fake.is_running() is True
    p.next()  # past the only song -> stop()
    assert not p.is_playing
    assert p.current_index == -1
    assert fake.stopped >= 1
    assert fake.is_running() is False


def test_mic_unmutes_without_restart_when_switching_from_pause(qapp):
    # Switching songs while paused goes paused->playing via _begin_play;
    # the mic must unmute but NOT restart the (still open) stream.
    p = PlayerController()
    p.append(make("A", "a1"))
    p.append(make("B", "b1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)
    p.set_mic_enabled(True)
    p.play_at(0)
    p.toggle_pause()  # paused -> mute
    assert fake.mutes[-1] is True
    p.play_at(1)  # paused -> playing via _begin_play
    assert p.current_index == 1
    assert p.is_playing
    assert fake.started == 1  # no restart
    assert fake.mutes[-1] is False  # last mute state is False (unmuted)
    assert fake.is_running() is True
    p.stop()


def test_configure_mic_while_playing_starts_stream(qapp):
    # Enabling the mic via configure_mic while already playing must start the
    # stream immediately (no wait for the next state transition).
    p = PlayerController()
    p.append(make("A", "a1"))
    fake = _FakeMicMixer()
    p.attach_mic_mixer(fake)
    p.play_at(0)  # mic disabled: nothing starts yet
    assert p.is_playing
    assert fake.started == 0
    p.configure_mic(
        enabled=True,
        gain_db=0,
        echo=0.3,
        bass_db=0,
        treble_db=0,
        device="",
    )
    assert fake.started == 1  # configuring while playing starts the stream
    assert fake.is_running() is True
    assert fake.params == {
        "gain_db": 0.0,
        "echo": 0.3,
        "bass_db": 0.0,
        "treble_db": 0.0,
    }
    p.stop()


def test_real_mixer_noop_under_dummy_audio(qapp, monkeypatch):
    # A REAL MicMixer (not a fake) under dummy audio: configuring and playing
    # must not raise, and the capture stream must stay closed.
    from ezkaraoke.mic_mixer import MicMixer

    monkeypatch.setenv("EZKARAOKE_DUMMY_AUDIO", "1")
    p = PlayerController()
    p.append(make("A", "a1"))
    p.configure_mic(
        enabled=True,
        gain_db=0,
        echo=0.3,
        bass_db=0,
        treble_db=0,
        device="",
    )
    assert isinstance(p.mic_mixer, MicMixer)  # a real mixer was created
    p.play_at(0)  # must not raise
    assert p.is_playing
    assert p.mic_mixer.is_running() is False  # dummy audio: stream stays closed
    p.stop()
