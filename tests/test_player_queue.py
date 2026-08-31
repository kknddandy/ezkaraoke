"""Tests for ezkaraoke.player queue logic.

These run with NO libvlc installed and NO display: vlc.Instance() fails on
this machine, so all playback is a no-op while queue bookkeeping and
signals must still work.
"""

import os

# Offscreen platform: no display server is available on this machine.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

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

    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.set_calls: list[int] = []
        self.current: int = 1 if count else -1
        self.rates: list[float] = []
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
    # python-vlc invokes the EndReached handler as callback(event) —
    # exactly one argument. Regression: a stray extra argument used to be
    # attached, raising TypeError inside the handler and breaking
    # auto-advance.
    p = PlayerController()
    s1, s2 = make("A", "a1"), make("B", "b1")
    p.append(s1)
    p.append(s2)
    p.play_at(0)
    assert p.current_index == 0
    p._on_media_end(object())  # must not raise
    assert p.current_index == 1
    assert p.is_playing
    p.stop()


def test_media_end_at_last_song_stops(qapp):
    p = PlayerController()
    p.append(make("A", "a1"))
    p.play_at(0)
    p._on_media_end(object())  # last song ended -> stop, no raise
    assert p.current_index == -1
    assert not p.is_playing


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


def test_pitch_reapplied_on_new_media(qapp):
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
