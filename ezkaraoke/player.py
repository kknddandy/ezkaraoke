"""VLC-backed playback controller.

Owns a python-vlc instance (created lazily) and a song queue. All queue
operations work even when the native libvlc runtime is missing: playback
becomes a no-op and ``status_message`` is emitted once.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes.util import find_library
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer

from ezkaraoke import pitchshift
from ezkaraoke.library import Song
from ezkaraoke.loudness import compute_gain

_NO_VLC_MESSAGE = "未检测到 VLC 运行库，请安装 VLC 后重启"

# Software h264 decoding. VLC 3.0.x's VA-API path on Wayland/i965 can exhaust
# its surface pool and spam "get_buffer() failed / thread_get_buffer() failed /
# no frame!", and in the worst case freeze the picture (videolan/vlc#25701).
# Karaoke video is low-resolution, so software decode is a cheap, stable fix.
_MEDIA_OPTIONS = (":avcodec-hw=none",)


def _vlc_library_names() -> tuple[str, ...]:
    """Candidate libvlc shared-library names for the current platform."""
    if sys.platform == "win32":
        return ("libvlc.dll", "vlc.dll")
    if sys.platform == "darwin":
        return ("libvlc.5.dylib", "libvlc.4.dylib", "libvlc.dylib")
    return (
        "libvlc.so.5",
        "libvlc.so.4",
        "libvlc.so.3",
        "libvlc.so.12",
        "libvlc.so.11",
    )


def _detect_pitch_fn(vlc_module):
    """libvlc_media_player_set_audio_pitch when the runtime has it (VLC >= 4)."""
    libs = []
    lib = getattr(vlc_module, "libvlc", None)
    if lib is not None:
        libs.append(lib)
    names = list(_vlc_library_names())
    found = find_library("vlc")
    if found:
        names.append(found)
    for name in names:
        try:
            libs.append(ctypes.CDLL(name))
        except OSError:
            continue
    for lib in libs:
        fn = getattr(lib, "libvlc_media_player_set_audio_pitch", None)
        if fn is not None:
            return fn
    return None


class _ShiftSignals(QObject):
    """Thread-safe signal bridge for the pitch-shift worker thread."""

    finished = Signal(str, int)  # source path, semitones
    progress = Signal(float)     # 0.0 .. 1.0
    failed = Signal(str, int)    # source path, semitones


class _PitchShiftWorker(threading.Thread):
    """Background rubberband shift; signals report the outcome."""

    def __init__(self, source: str, semitones: int, dest: Path,
                 signals: _ShiftSignals) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.semitones = semitones
        self.dest = dest
        self.signals = signals
        self._cancel = threading.Event()

    def stop(self) -> None:
        """Cancel after the current ffmpeg step (kills the subprocess)."""
        self._cancel.set()

    def wait(self, ms: int = 5000) -> bool:  # noqa: A003 - QThread-compatible
        self.join(ms / 1000.0)
        return not self.is_alive()

    def run(self) -> None:
        ok = pitchshift.shift_audio(
            self.source,
            self.dest,
            self.semitones,
            progress_cb=self.signals.progress.emit,
            cancel_event=self._cancel,
        )
        if self._cancel.is_set():
            return
        if ok:
            self.signals.finished.emit(self.source, self.semitones)
        else:
            self.signals.failed.emit(self.source, self.semitones)


class PlayerController(QObject):
    current_changed = Signal(int)    # index of current song in queue, -1 if none
    queue_changed = Signal()         # membership or order changed
    state_changed = Signal(str)      # "playing" | "paused" | "stopped"
    status_message = Signal(str)     # warnings, e.g. missing libvlc
    audio_track_changed = Signal(int)  # active track index (0=原唱, 1=伴奏)
    pitch_changed = Signal(int)        # semitones, 0 = 原调
    pitch_status = Signal(str)         # "" idle | "shifting" generating variant
    pitch_progress = Signal(float)     # 0.0 .. 1.0 while shifting
    _media_ended = Signal(int)         # internal: EndReached (libvlc thread) -> main thread (epoch)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: list[Song] = []
        self._current_index: int = -1
        self._state: str = "stopped"
        self._pitch_semitones = 0
        self._pitch_fn = None
        # vlc objects, lazily created (None until first playback request)
        self._vlc = None
        self._player = None
        self._current_media = None
        self._vlc_available: bool | None = None  # None = not yet attempted
        self._warned_no_vlc = False
        self._advancing = False  # main-thread re-entrancy flag for EndReached handling
        self._audio_track_index = 0  # preferred track, persists across songs
        self._shift_worker: _PitchShiftWorker | None = None
        self._active_path: str | None = None  # file currently loaded in VLC
        self._epoch = 0  # identity of the currently loaded media; any in-flight EndReached from an earlier media is stale
        self._ev_end = None  # stash of vlc.EventType.MediaPlayerEndReached
        self._media_ended.connect(self._on_media_ended)
        # Loudness alignment (docs/loudness-normalization-spec.md §7)
        self._db = None
        self._loudness_enabled = True
        self._loudness_target = -11.25
        # Mic mixing: created lazily (None = no mixer); the capture stream
        # lives across songs and is driven by _sync_mic on state changes.
        self._mic_mixer = None
        self._mic_device = None  # input device the current mixer was built for
        self._mic_enabled = False

    # ------------------------------------------------------------------ state
    @property
    def queue(self) -> list[Song]:
        return list(self._queue)

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def current_song(self) -> Song | None:
        if 0 <= self._current_index < len(self._queue):
            return self._queue[self._current_index]
        return None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_playing(self) -> bool:
        return self._state == "playing"

    @property
    def is_paused(self) -> bool:
        return self._state == "paused"

    @property
    def audio_track_index(self) -> int:
        return self._audio_track_index

    @property
    def pitch_semitones(self) -> int:
        return self._pitch_semitones

    @property
    def pitch_is_pure(self) -> bool:
        """True when a real pitch shift (no tempo change) is available."""
        return self._pitch_fn is not None or pitchshift.shift_available()

    # ------------------------------------------------------------------- vlc
    def _ensure_vlc(self) -> bool:
        """Lazily create the vlc Instance + MediaPlayer.

        Returns False (and warns once) when the native libvlc runtime is
        missing; never raises.
        """
        if self._player is not None:
            return True
        if self._vlc_available is False:
            return False
        try:
            import vlc

            # Tests set EZKARAOKE_DUMMY_AUDIO=1 (tests/conftest.py) so the
            # suite's generated media never reaches the real speakers.
            vlc_args = (
                ["--aout=dummy"]
                if os.environ.get("EZKARAOKE_DUMMY_AUDIO") == "1"
                else []
            )
            self._vlc = vlc.Instance(*vlc_args)
            self._player = self._vlc.media_player_new()
        except Exception:  # ImportError/OSError/TypeError/NameError ...
            self._vlc_available = False
            self._player = None
            self._warn_no_vlc()
            return False
        self._vlc_available = True
        try:
            self._pitch_fn = _detect_pitch_fn(vlc)
        except Exception:  # noqa: BLE001 - detection is best-effort
            self._pitch_fn = None
        try:
            # Stash the event type only; EndReached is (re)attached per
            # media load in _attach_end_events with the epoch bound in.
            self._ev_end = vlc.EventType.MediaPlayerEndReached
        except Exception:
            pass
        return True

    def _warn_no_vlc(self) -> None:
        if not self._warned_no_vlc:
            self._warned_no_vlc = True
            self.status_message.emit(_NO_VLC_MESSAGE)

    def _attach_end_events(self) -> None:
        """(Re)register EndReached bound to the current media epoch.

        libvlc allows only one callback per event type, so detach the old
        registration first. The epoch is bound as an event argument, so the
        libvlc-thread handler never reads mutable controller state: an event
        belonging to an already-replaced media can never be mistaken for the
        current one.
        """
        player = self._player
        if player is None or self._ev_end is None:
            return
        try:
            em = player.event_manager()
        except Exception:  # noqa: BLE001
            return
        try:
            em.event_detach(self._ev_end)
        except Exception:  # noqa: BLE001
            pass
        try:
            em.event_attach(self._ev_end, self._on_media_end, self._epoch)
        except Exception:  # noqa: BLE001
            pass

    def _on_media_end(self, event, epoch: int) -> None:
        """libvlc EndReached handler — runs on the libvlc event thread.

        Forwards only; `epoch` was bound at attach/load time. Must not call
        libvlc (stop/set_media/play) or touch Qt here: the event fires while
        the input thread is still finishing the media and a player call from
        this callback deadlocks against it. Post to the Qt main thread.
        """
        self._media_ended.emit(epoch)

    def _on_media_ended(self, epoch: int) -> None:
        """Main-thread half of EndReached: advance the queue.

        Ignored when the event belongs to a media that has already been
        replaced (epoch mismatch), when playback was stopped/paused away, or
        when the current media has not actually played to its end.
        """
        if epoch != self._epoch:
            return
        if self._state not in ("playing", "paused"):
            return
        if not self._reached_end():
            return
        self._advancing = True
        try:
            self.next()
        finally:
            self._advancing = False

    def _reached_end(self) -> bool:
        """True when the current media plausibly played to its end.

        Guards against libvlc emitting EndReached spuriously (decoder / video
        output teardown, hwaccel failure) while the song is still part-way.
        Unknown duration or position counts as reached, so genuine ends still
        advance and streams are never stalled.

        Tolerance: libvlc's reported position at a genuine EndReached can sit
        slightly behind the nominal length (the software video path reports
        ~891ms for a 1000ms clip), so accept anything within the last 2s or
        5% of the duration (whichever is larger). A spurious end tens of
        seconds into a multi-minute song is still rejected.
        """
        if self._player is None:
            return True
        try:
            length = self._player.get_length()
        except Exception:  # noqa: BLE001
            return True
        if length <= 0:
            return True
        try:
            t = self._player.get_time()
        except Exception:  # noqa: BLE001
            return True
        if t < 0:
            return True
        return t >= length - max(2000, length * 0.05)

    def _new_media(self, path: str):
        """Create a libvlc Media with the project's decode options applied."""
        media = self._vlc.media_new(path)
        for option in _MEDIA_OPTIONS:
            try:
                media.add_option(option)
            except Exception:  # noqa: BLE001 - options are best-effort
                pass
        return media

    def _start_vlc(self) -> None:
        """Start actual playback of the current song. No-op without libvlc."""
        if self._current_index < 0 or self._current_index >= len(self._queue):
            return
        if not self._ensure_vlc():
            return
        song = self._queue[self._current_index]
        path, needs_shift = self._resolve_play_path(song.path)
        try:
            self._player.stop()
            self._current_media = self._new_media(path)
            self._player.set_media(self._current_media)
            self._attach_end_events()
            self._player.play()
            self._active_path = path
            QTimer.singleShot(0, lambda: self._sync_audio_track(0))
            self._apply_loudness_gain()
            # VLC resets rate/pitch per new media; reapply the preferred pitch.
            self._apply_pitch()
            if needs_shift:
                self._request_shift(song.path)
        except Exception as e:  # noqa: BLE001 - playback failure is a warning
            self.status_message.emit(str(e))

    def _resolve_play_path(self, source: str) -> tuple[str, bool]:
        """(file to play now, shifted variant still needed?)

        A ready cache variant is played directly; otherwise the original is
        played immediately and a background shift fills the cache.
        """
        if self._pitch_semitones != 0 and pitchshift.shift_available():
            cache = pitchshift.shifted_file_path(source, self._pitch_semitones)
            if cache.exists() and cache.stat().st_size > 0:
                return str(cache), False
        return source, self._pitch_semitones != 0 and pitchshift.shift_available()

    def _stop_vlc(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:  # noqa: BLE001
                pass
        self._current_media = None
        self._active_path = None

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
            # Every playing/paused/stopped transition drives the mic.
            self._sync_mic()

    def _begin_play(self, index: int) -> None:
        """Bookkeeping + playback start for *index* (a valid queue index)."""
        self._epoch += 1
        self._current_index = index
        self.current_changed.emit(index)
        self._set_state("playing")
        self._start_vlc()

    # --------------------------------------------------------- queue control
    def append(self, song: Song) -> int:
        """Add *song* to the end; duplicate paths return the existing index."""
        for i, existing in enumerate(self._queue):
            if existing.path == song.path:
                return i
        self._queue.append(song)
        self.queue_changed.emit()
        return len(self._queue) - 1

    def insert_next(self, song: Song) -> int:
        """Insert at current_index+1 (or 0 if none current).

        A duplicate path is moved to that spot instead of duplicated.
        """
        pos = self._current_index + 1 if self._current_index >= 0 else 0
        old_current = self._current_index
        for i, existing in enumerate(self._queue):
            if existing.path == song.path:
                del self._queue[i]
                if i < pos:
                    pos -= 1
                # Keep current_index pointing at the same song after the
                # remove + insert round trip.
                if i < self._current_index:
                    # something before the current song was removed
                    self._current_index -= 1
                elif i == self._current_index:
                    # the moved song IS the current one; it will sit at
                    # its new insertion position
                    self._current_index = pos
                elif pos <= self._current_index:
                    # moved song was not current and the insertion lands
                    # at/before the current song
                    self._current_index += 1
                self._queue.insert(pos, song)
                self.queue_changed.emit()
                if self._current_index != old_current:
                    self.current_changed.emit(self._current_index)
                return pos
        self._queue.insert(pos, song)
        self.queue_changed.emit()
        return pos

    def play_now(self, song: Song) -> None:
        """Remove any existing copy, put *song* at the front and play it."""
        for i, existing in enumerate(self._queue):
            if existing.path == song.path:
                del self._queue[i]
                break
        self._queue.insert(0, song)
        self.queue_changed.emit()
        if self._current_index != -1:
            self._stop_vlc()
        self._begin_play(0)

    def play_at(self, index: int) -> None:
        if not 0 <= index < len(self._queue):
            return
        if index == self._current_index:
            return
        if self._current_index != -1:
            self._stop_vlc()
        self._begin_play(index)

    def next(self) -> None:
        if self._current_index == -1:
            if self._queue:
                self._begin_play(0)
            return
        ni = self._current_index + 1
        if ni < len(self._queue):
            self._stop_vlc()
            self._begin_play(ni)
        else:
            self.stop()

    def prev(self) -> None:
        if self._current_index == -1:
            return
        if self._current_index == 0:
            self._stop_vlc()
            self._begin_play(0)
        else:
            self._stop_vlc()
            self._begin_play(self._current_index - 1)

    def remove_at(self, index: int) -> None:
        if not 0 <= index < len(self._queue):
            return
        if index == self._current_index:
            del self._queue[index]
            self.queue_changed.emit()
            if self._queue:
                # the song now sitting at this index (the former next) plays
                self._stop_vlc()
                self._begin_play(index)
            else:
                self.stop()
        else:
            del self._queue[index]
            if index < self._current_index:
                self._current_index -= 1
                self.current_changed.emit(self._current_index)
            self.queue_changed.emit()

    def move(self, index: int, delta: int) -> None:
        if not self._queue:
            return
        new_index = min(max(index + delta, 0), len(self._queue) - 1)
        if new_index == index:
            return
        song = self._queue.pop(index)
        self._queue.insert(new_index, song)
        old_current = self._current_index
        if self._current_index == index:
            self._current_index = new_index
        elif new_index > index and index < self._current_index <= new_index:
            self._current_index -= 1
        elif new_index < index and new_index <= self._current_index < index:
            self._current_index += 1
        if self._current_index != old_current:
            self.current_changed.emit(self._current_index)
        self.queue_changed.emit()

    def move_rows(self, rows: list[int], delta: int) -> None:
        """Shift the queue rows in *rows* by one step toward *delta* (clamped).

        Each consecutive run of selected rows shifts as one unit: the row
        just beyond the run's moving edge rotates into the run's vacated
        slot, and a run already at the queue boundary stays put.
        Batched: regardless of how many rows move, the queue table
        refreshes once and at most one ``current_changed`` fires. For
        arbitrary-distance moves of one row, use :meth:`move`; for "put
        right after current", :meth:`jump_after_current`.
        """
        if not self._queue or delta == 0:
            return
        n = len(self._queue)
        selected = {i for i in rows if 0 <= i < n}
        if not selected:
            return
        moved = list(self._queue)
        if delta < 0:
            idx = 0
            while idx < n:
                if idx not in selected:
                    idx += 1
                    continue
                start = idx
                while start + 1 < n and start + 1 in selected:
                    start += 1
                if idx > 0:
                    # Run shifts up one; the row above drops to its end.
                    moved[idx - 1 : start + 1] = moved[idx : start + 1] + [
                        moved[idx - 1]
                    ]
                idx = start + 1
        else:
            idx = n - 1
            while idx >= 0:
                if idx not in selected:
                    idx -= 1
                    continue
                end = idx
                while end - 1 >= 0 and end - 1 in selected:
                    end -= 1
                if idx < n - 1:
                    # Run shifts down one; the row below rises to its start.
                    moved[end : idx + 2] = [moved[idx + 1]] + moved[end : idx + 1]
                idx = end - 1
        if [s.path for s in moved] == [s.path for s in self._queue]:
            return
        old_current = self._current_index
        current_song = (
            self._queue[old_current] if 0 <= old_current < n else None
        )
        self._queue = moved
        if current_song is not None:
            self._current_index = next(
                i for i, s in enumerate(moved) if s is current_song
            )
        if self._current_index != old_current:
            self.current_changed.emit(self._current_index)
        self.queue_changed.emit()

    def remove_rows(self, rows: list[int]) -> None:
        """Remove all queue rows in *rows* in one pass.

        If the playing row is among them, playback continues with the
        next surviving song (first survivor after the removed row, or the
        last survivor when nothing followed it); with nothing left,
        playback stops. The queue table refreshes once.
        """
        n = len(self._queue)
        valid = {i for i in rows if 0 <= i < n}
        if not valid:
            return
        removing_current = 0 <= self._current_index < n and self._current_index in valid
        old_current = self._current_index
        next_song = None
        if removing_current:
            after = [i for i in range(old_current + 1, n) if i not in valid]
            before = [i for i in range(old_current) if i not in valid]
            target = after[0] if after else (before[-1] if before else None)
            if target is not None:
                next_song = self._queue[target]
        self._queue = [s for i, s in enumerate(self._queue) if i not in valid]
        if removing_current:
            if next_song is not None:
                self._stop_vlc()
                self._begin_play(
                    next(i for i, s in enumerate(self._queue) if s is next_song)
                )
            else:
                self.stop()
        else:
            if 0 <= old_current:
                self._current_index = old_current - sum(1 for i in valid if i < old_current)
                self.current_changed.emit(self._current_index)
        self.queue_changed.emit()

    def jump_after_current(self, indices: list[int]) -> None:
        """Move the queue rows at *indices* right after the playing song.

        The current song itself is skipped (it is already in front of the
        insertion point); with nothing playing the songs go to the front.
        The relative order of the moved songs is preserved, and no signals
        are emitted when the order does not actually change.
        """
        if not self._queue:
            return
        valid = {i for i in indices if 0 <= i < len(self._queue)}
        old_current = self._current_index
        current_song = (
            self._queue[old_current] if 0 <= old_current < len(self._queue) else None
        )
        drop = {i for i in valid if i != old_current}
        moved = [self._queue[i] for i in sorted(drop)]
        if not moved:
            return
        rest = [s for i, s in enumerate(self._queue) if i not in drop]
        if current_song is not None:
            cur_pos = next(i for i, s in enumerate(rest) if s is current_song)
            target = cur_pos + 1
        else:
            cur_pos = -1
            target = 0
        new_queue = rest[:target] + moved + rest[target:]
        if [s.path for s in new_queue] == [s.path for s in self._queue]:
            return
        self._queue = new_queue
        self._current_index = cur_pos  # insertions land after it: unchanged
        self.queue_changed.emit()

    def replay(self) -> None:
        """Restart the current song from the beginning."""
        if not 0 <= self._current_index < len(self._queue):
            return
        self._begin_play(self._current_index)

    def clear_queue(self) -> None:
        """Stop playback and drop every queued song."""
        if self._state != "stopped":
            self.stop()
        if not self._queue:
            return
        self._queue = []
        self.queue_changed.emit()

    # --------------------------------------------------------- transport keys
    def toggle_pause(self) -> None:
        if self._state == "playing":
            self._set_state("paused")
            if self._player is not None:
                try:
                    self._player.pause()
                except Exception:  # noqa: BLE001
                    pass
        elif self._state == "paused":
            self._set_state("playing")
            if self._player is not None:
                try:
                    self._player.play()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        """Stop playback; the queue is kept, current_index becomes -1."""
        self._epoch += 1  # invalidate any in-flight end event
        self._stop_vlc()
        if self._current_index != -1:
            self._current_index = -1
            self.current_changed.emit(-1)
        self._set_state("stopped")

    # ------------------------------------------------------- position & seek
    def playback_position_ms(self) -> int:
        """Current playback position in milliseconds; -1 when unknown."""
        if self._player is None:
            return -1
        try:
            position = self._player.get_time()
        except Exception:  # noqa: BLE001
            return -1
        return int(position) if position is not None and position >= 0 else -1

    def media_length_ms(self) -> int:
        """Length of the current media in milliseconds; -1 when unknown."""
        if self._player is None:
            return -1
        try:
            length = self._player.get_length()
        except Exception:  # noqa: BLE001
            return -1
        return int(length) if length is not None and length >= 0 else -1

    def seek_to_ms(self, ms: int) -> None:
        """Seek the current media to *ms* (clamped to >= 0). No-op without a player."""
        if self._player is None:
            return
        try:
            self._player.set_time(max(0, int(ms)))
        except Exception:  # noqa: BLE001
            pass

    def can_seek(self) -> bool:
        """True when the current media has a known, non-zero length."""
        return self.media_length_ms() > 0

    def set_video_output(self, hwnd: int) -> bool:
        """Attach the video output to *hwnd*. False if libvlc is missing."""
        if not self._ensure_vlc():
            return False
        try:
            self._player.set_hwnd(hwnd)
        except Exception:  # noqa: BLE001
            return False
        return True

    def _audio_track_ids(self) -> list[int]:
        """Real audio track IDs of the current media, in media order.

        libvlc's track list contains a -1 "Disable" pseudo-entry and the IDs
        are not guaranteed to be 0..n-1 (a two-track file may report IDs 1
        and 2), so resolve them instead of assuming index == id.
        """
        if self._player is None:
            return []
        try:
            desc = self._player.audio_get_track_description()
        except Exception:  # noqa: BLE001
            return []
        ids: list[int] = []
        for entry in desc:
            try:
                tid = entry[0] if isinstance(entry, tuple) else entry.id
            except Exception:  # noqa: BLE001
                continue
            if tid >= 0:
                ids.append(tid)
        return ids

    def has_multi_audio_track(self) -> bool:
        """True when the current media exposes two or more audio tracks."""
        return len(self._audio_track_ids()) >= 2

    def toggle_audio_track(self) -> None:
        """Toggle 原唱 (track 0) / 伴奏 (track 1) on the current media.

        No-op when libvlc is missing or the media has fewer than two
        audio tracks.
        """
        if self._player is None:
            return
        ids = self._audio_track_ids()
        if len(ids) < 2:
            return
        new_index = 1 if self._audio_track_index == 0 else 0
        try:
            if self._player.audio_set_track(ids[new_index]) != 0:
                return
        except Exception:  # noqa: BLE001
            return
        self._audio_track_index = new_index
        self._apply_loudness_gain()
        self.audio_track_changed.emit(new_index)
        self.status_message.emit("已切换为伴奏" if new_index else "已切换为原唱")

    def _sync_audio_track(self, attempt: int) -> None:
        """Resolve the new media's audio tracks and apply the preferred one.

        Track info may not be parsed yet right after set_media, so retry a
        few times. Emits audio_track_changed with the settled index.

        Re-applies loudness on every entry (including retries): libvlc may
        rebuild the audio output asynchronously after set_media and drop the
        EQ, which would otherwise leave a flat-EQ window at song start.
        """
        self._apply_loudness_gain()
        player = self._player
        if player is None or self._current_index < 0:
            return
        ids = self._audio_track_ids()
        if not ids:
            if attempt < 10:
                QTimer.singleShot(200, lambda: self._sync_audio_track(attempt + 1))
            else:
                self._apply_loudness_gain()
                self.audio_track_changed.emit(self._audio_track_index)
            return
        preferred = self._audio_track_index if len(ids) >= 2 else 0
        if preferred >= len(ids):
            preferred = 0
        try:
            current = player.audio_get_track()
        except Exception:  # noqa: BLE001
            current = -1
        if current != ids[preferred]:
            try:
                ok = player.audio_set_track(ids[preferred]) == 0
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                if attempt < 10:
                    QTimer.singleShot(200, lambda: self._sync_audio_track(attempt + 1))
                    return
                preferred = 0
        self._audio_track_index = preferred
        self._apply_loudness_gain()
        self.audio_track_changed.emit(preferred)

    # ------------------------------------------------------------- loudness
    def attach_database(self, db) -> None:
        """Loudness source; None disables loudness alignment silently."""
        self._db = db

    @property
    def loudness_enabled(self) -> bool:
        return self._loudness_enabled

    @property
    def loudness_target(self) -> float:
        return self._loudness_target

    def set_loudness_enabled(self, enabled: bool) -> None:
        self._loudness_enabled = bool(enabled)
        self._apply_loudness_gain()  # immediate; off -> preamp 0

    def set_loudness_target(self, target: float) -> None:
        self._loudness_target = float(target)
        self._apply_loudness_gain()

    def _loudness_rows_for_active(self):
        """Rows for the active path, falling back to the current song's path
        (pitch-shift cache variants have no own measurement)."""
        if self._db is None or self._current_index < 0:
            return []
        song = self._queue[self._current_index]
        for path in (self._active_path, song.path):
            if path:
                try:
                    rows = self._db.get_loudness(path)
                except Exception:  # noqa: BLE001 - loudness must not break playback
                    rows = []
                if rows:
                    return rows
        return []

    def _apply_loudness_gain(self) -> None:
        """Align the active track to the target loudness via EQ preamp.

        No libvlc / no DB / nothing measured -> preamp 0. Never raises:
        loudness alignment must not break playback.
        """
        player = self._player
        if player is None:
            return
        gain = 0.0
        if self._loudness_enabled:
            rows = self._loudness_rows_for_active()
            if rows:
                row = rows[min(max(self._audio_track_index, 0), len(rows) - 1)]
                if row.lufs is not None:
                    gain = compute_gain(row.lufs, row.peak_db, self._loudness_target)
        try:
            import vlc

            eq = vlc.AudioEqualizer()
            eq.set_preamp(gain)
            player.audio_set_equalizer(eq)
        except Exception:  # noqa: BLE001 - loudness must never break playback
            pass

    # ------------------------------------------------------------------- mic
    def attach_mic_mixer(self, mixer) -> None:
        """Inject the mic mixer (tests, future mixer window); None disables it.

        Syncs immediately: injecting or replacing a mixer while playback is
        running starts the stream right away instead of waiting for the
        next state transition.
        """
        self._mic_mixer = mixer
        self._sync_mic()

    @property
    def mic_mixer(self):
        return self._mic_mixer

    def configure_mic(
        self,
        *,
        enabled: bool,
        gain_db: float,
        echo: float,
        bass_db: float,
        treble_db: float,
        device: str = "",
    ) -> None:
        """Create or reuse the mic mixer and apply the parameters.

        The capture stream itself stays closed until playback starts;
        ``_sync_mic`` opens it on the first "playing" transition. Requesting
        a different input device stops the old mixer and rebuilds it; the
        same device reuses the existing one. Mic problems (missing deps,
        PortAudio failure, a bad device) never crash startup: they are
        swallowed and playback continues without the mic.
        """
        try:
            want = device or None
            if self._mic_mixer is None or want != self._mic_device:
                old = self._mic_mixer
                if old is not None:
                    try:
                        old.stop()
                    except Exception:  # noqa: BLE001
                        pass
                # Lazy import: sounddevice/numpy must never become a hard
                # module-level dependency of the player.
                from ezkaraoke.mic_mixer import MicMixer

                self._mic_mixer = MicMixer(input_device=want)
                self._mic_device = want
            m = self._mic_mixer
            m.set_gain_db(gain_db)
            m.set_echo(echo)
            m.set_bass_db(bass_db)
            m.set_treble_db(treble_db)
            self._mic_enabled = bool(enabled)
            self._sync_mic()
        except Exception:  # noqa: BLE001 - mic problems must never crash startup
            pass

    def set_mic_enabled(self, enabled: bool) -> None:
        """Enable/disable mic mixing; syncs the stream with the state."""
        self._mic_enabled = bool(enabled)
        self._sync_mic()

    def _sync_mic(self) -> None:
        """Drive the mic stream from the playback state.

        playing: start (once — the stream persists between songs) + unmute;
        paused:  mute (the stream stays open);
        stopped: close the stream.
        Mic problems never break playback.
        """
        try:
            m = self._mic_mixer
            if m is None:
                return
            if not self._mic_enabled:
                m.stop()
                return
            if self._state == "playing":
                if not m.is_running():
                    if not m.start():  # no mic / deps missing / dummy audio
                        return  # normal: keep playing without the mic
                m.set_muted(False)
            elif self._state == "paused":
                m.set_muted(True)
            else:  # stopped
                m.stop()
        except Exception:  # noqa: BLE001 - mic problems never break playback
            pass

    # ------------------------------------------------------------------ pitch
    def set_pitch(self, semitones: int) -> None:
        """Set pitch shift in semitones, clamped to [-12, 12]. No-op when unchanged."""
        target = max(-12, min(12, int(semitones)))
        if target == self._pitch_semitones:
            return
        self._pitch_semitones = target
        self._apply_pitch()
        self.pitch_changed.emit(target)

    def change_pitch(self, delta: int) -> None:
        self.set_pitch(self._pitch_semitones + delta)

    def shutdown_shift(self) -> None:
        """Cancel a running pitch-shift job (window close / app exit)."""
        if self._shift_worker is not None:
            self._shift_worker.stop()

    def _apply_pitch(self) -> None:
        """Make the loaded media match the preferred pitch.

        Priority: real libvlc pitch API > offline rubberband variant
        (original plays first, hot-swapped when the variant is ready) >
        tape speed (pitch AND tempo shift, last resort).
        """
        player = self._player
        if player is None:
            return
        semis = self._pitch_semitones
        song = self.current_song
        if semis == 0:
            if (
                song is not None
                and self._active_path is not None
                and self._active_path != song.path
            ):
                self._swap_to(song.path)  # back from a shifted variant
            return
        if self._pitch_fn is not None:
            try:
                # player._as_parameter_ is the python-vlc _Ctype ctypes
                # protocol attribute: raw c_void_p + c_float, no argtypes.
                self._pitch_fn(
                    player._as_parameter_,
                    ctypes.c_float(100.0 * (2.0 ** (semis / 12.0))),
                )
                return
            except Exception:  # noqa: BLE001
                pass
        if song is not None and pitchshift.shift_available():
            cache = pitchshift.shifted_file_path(song.path, semis)
            if cache.exists() and cache.stat().st_size > 0:
                if self._active_path != str(cache):
                    self._swap_to(str(cache))
            elif self._active_path == song.path:
                self._request_shift(song.path)
            return
        # Last resort: tape speed — pitch AND tempo shift together
        try:
            player.set_rate(2.0 ** (semis / 12.0))
        except Exception:  # noqa: BLE001
            pass

    def _request_shift(self, source: str) -> None:
        """Start (or reuse) a background shift of *source* to the current pitch."""
        semis = self._pitch_semitones
        if semis == 0:
            return
        cache = pitchshift.shifted_file_path(source, semis)
        if cache.exists() and cache.stat().st_size > 0:
            return
        worker = self._shift_worker
        if (
            worker is not None
            and worker.is_alive()
            and worker.source == source
            and worker.semitones == semis
        ):
            return
        if worker is not None:
            worker.stop()  # superseded by a new pitch choice
        pitchshift.warm_cache()
        signals = _ShiftSignals()
        signals.finished.connect(self._on_shift_finished)
        signals.progress.connect(self.pitch_progress)
        signals.failed.connect(self._on_shift_failed)
        worker = _PitchShiftWorker(source, semis, cache, signals)
        self._shift_worker = worker
        worker.start()
        self.pitch_status.emit("shifting")

    def _on_shift_finished(self, source: str, semis: int) -> None:
        self._shift_worker = None
        self.pitch_status.emit("")
        pitchshift.prune_cache()
        song = self.current_song
        if song is None or song.path != source or self._pitch_semitones != semis:
            return  # user moved on: the file is cached for next time
        cache = pitchshift.shifted_file_path(source, semis)
        if self._player is not None and self._active_path == source:
            self._swap_to(str(cache))

    def _on_shift_failed(self, source: str, semis: int) -> None:
        self._shift_worker = None
        self.pitch_status.emit("")
        if self.current_song is not None and self.current_song.path == source:
            self.status_message.emit("变调生成失败（ffmpeg），保持原曲播放")

    def _swap_to(self, path: str) -> None:
        """Hot-swap the loaded media to *path*, preserving position + track."""
        player = self._player
        if player is None or self._vlc is None:
            return
        try:
            position = player.get_time()
        except Exception:  # noqa: BLE001
            position = -1
        try:
            player.stop()
            self._epoch += 1
            self._current_media = self._new_media(path)
            player.set_media(self._current_media)
            self._attach_end_events()
            player.play()
            if position > 0:
                player.set_time(position)
            self._active_path = path
            self._apply_loudness_gain()
            QTimer.singleShot(0, lambda: self._sync_audio_track(0))
        except Exception as e:  # noqa: BLE001 - keep the old media playing
            self.status_message.emit(str(e))

    def attach_video_callbacks(self, lock_cb, unlock_cb, setup_cb, cleanup_cb) -> bool:
        """Register custom video output callbacks (software rendering).

        Two separate libvlc calls are required: the lock/unlock/display pair and
        the format setup/cleanup pair live on different libvlc entry points.
        Used on platforms without a native window handle (Wayland) or as a
        fallback when set_hwnd fails. False if libvlc is missing.
        """
        if not self._ensure_vlc():
            return False
        try:
            self._player.video_set_callbacks(lock_cb, unlock_cb, None, None)
            self._player.video_set_format_callbacks(setup_cb, cleanup_cb)
            return True
        except Exception:  # noqa: BLE001
            return False

    def detach_video_callbacks(self) -> None:
        """Reset the video output callbacks (no-op without libvlc)."""
        if self._player is not None:
            try:
                self._player.video_set_callbacks(None, None, None, None)
                self._player.video_set_format_callbacks(None, None)
            except Exception:  # noqa: BLE001
                pass
