"""VLC-backed playback controller.

Owns a python-vlc instance (created lazily) and a song queue. All queue
operations work even when the native libvlc runtime is missing: playback
becomes a no-op and ``status_message`` is emitted once.
"""

from __future__ import annotations

import ctypes
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer

from ezkaraoke import pitchshift
from ezkaraoke.library import Song

_NO_VLC_MESSAGE = "未检测到 VLC 运行库，请安装 VLC 后重启 (sudo apt install vlc)"


def _detect_pitch_fn(vlc_module):
    """libvlc_media_player_set_audio_pitch when the runtime has it (VLC >= 4)."""
    libs = []
    lib = getattr(vlc_module, "libvlc", None)
    if lib is not None:
        libs.append(lib)
    for soname in ("libvlc.so.5", "libvlc.so.4", "libvlc.so.3", "libvlc.so.12", "libvlc.so.11"):
        try:
            libs.append(ctypes.CDLL(soname))
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
        self._advancing = False  # re-entrancy guard for EndReached handling
        self._audio_track_index = 0  # preferred track, persists across songs
        self._shift_worker: _PitchShiftWorker | None = None
        self._active_path: str | None = None  # file currently loaded in VLC

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

            self._vlc = vlc.Instance()
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
            event_manager = self._player.event_manager()
            # Note: no trailing positional argument — python-vlc forwards
            # every extra arg to the callback, which only takes (event).
            event_manager.event_attach(
                vlc.EventType.MediaPlayerEndReached, self._on_media_end
            )
        except Exception:
            pass
        return True

    def _warn_no_vlc(self) -> None:
        if not self._warned_no_vlc:
            self._warned_no_vlc = True
            self.status_message.emit(_NO_VLC_MESSAGE)

    def _on_media_end(self, event) -> None:
        if self._advancing:
            return
        self._advancing = True
        try:
            self.next()
        finally:
            self._advancing = False

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
            self._current_media = self._vlc.media_new(path)
            self._player.set_media(self._current_media)
            self._player.play()
            self._active_path = path
            QTimer.singleShot(0, lambda: self._sync_audio_track(0))
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

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def _begin_play(self, index: int) -> None:
        """Bookkeeping + playback start for *index* (a valid queue index)."""
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
        self._stop_vlc()
        if self._current_index != -1:
            self._current_index = -1
            self.current_changed.emit(-1)
        self._set_state("stopped")

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
        self.audio_track_changed.emit(new_index)
        self.status_message.emit("已切换为伴奏" if new_index else "已切换为原唱")

    def _sync_audio_track(self, attempt: int) -> None:
        """Resolve the new media's audio tracks and apply the preferred one.

        Track info may not be parsed yet right after set_media, so retry a
        few times. Emits audio_track_changed with the settled index.
        """
        player = self._player
        if player is None or self._current_index < 0:
            return
        ids = self._audio_track_ids()
        if not ids:
            if attempt < 10:
                QTimer.singleShot(200, lambda: self._sync_audio_track(attempt + 1))
            else:
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
        self.audio_track_changed.emit(preferred)

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
            self._current_media = self._vlc.media_new(path)
            player.set_media(self._current_media)
            player.play()
            if position > 0:
                player.set_time(position)
            self._active_path = path
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
