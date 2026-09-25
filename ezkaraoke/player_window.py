from pathlib import Path
import sys

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ezkaraoke import i18n
from ezkaraoke.config import Config, save_config
from ezkaraoke.frame_bridge import FrameBridge, video_output_mode
from ezkaraoke.i18n import off_language_changed, on_language_changed, tr
from ezkaraoke.mic_mixer import list_input_devices
from ezkaraoke.player import PlayerController


class VideoWidget(QWidget):
    """Native window handle anchor used for X11 (set_hwnd) video output."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 180)
        self.setStyleSheet("background-color: #050508; border-radius: 8px;")


class VideoArea(QWidget):
    """Stacks the hwnd video widget and the software-render label.

    Only one of the two is visible at a time; double-click toggles
    fullscreen regardless of which surface is active.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 180)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            window = self.window()
            if isinstance(window, PlayerWindow):
                window.toggle_fullscreen()
        super().mouseDoubleClickEvent(event)


def _format_time_ms(ms: int) -> str:
    """Milliseconds as 'm:ss', or 'h:mm:ss' when the time reaches one hour."""
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class SeekSlider(QSlider):
    """Horizontal seek slider for the player window.

    A press anywhere on the groove jumps to that position immediately
    (emitting ``seekRequested``); the usual drag/release signals
    (``sliderMoved`` / ``sliderReleased``) keep working so the window can
    update the time label live while dragging and seek on release.
    """

    seekRequested = Signal(int)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            event.button() == Qt.LeftButton
            and self.isEnabled()
            and self.width() > 0
            and self.maximum() > self.minimum()
        ):
            value = QStyle.sliderValueFromPosition(
                self.minimum(),
                self.maximum(),
                int(event.position().x()),
                self.width(),
            )
            self.setValue(value)
            self.seekRequested.emit(value)
        super().mousePressEvent(event)


class PlayerWindow(QMainWindow):
    def __init__(
        self,
        controller: PlayerController,
        parent: QWidget | None = None,
        *,
        config: Config | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._config = config
        # Mic mixer panel: built on demand; the dialog reference (and the
        # widget attributes below) are cleared when the dialog closes.
        self._mixer_dialog: QDialog | None = None
        self._mic_enable_btn: QPushButton | None = None
        self._mic_gain_slider: QSlider | None = None
        self._mic_gain_value: QLabel | None = None
        self._mic_echo_slider: QSlider | None = None
        self._mic_echo_value: QLabel | None = None
        self._mic_bass_slider: QSlider | None = None
        self._mic_bass_value: QLabel | None = None
        self._mic_treble_slider: QSlider | None = None
        self._mic_treble_value: QLabel | None = None
        self._mic_device_combo: QComboBox | None = None
        self._mic_status_label: QLabel | None = None
        self._mic_status_timer: QTimer | None = None
        self.setWindowTitle(tr("ezkaraoke · 播放器"))
        self.resize(1024, 640)
        self._fs_timer = QTimer(self)
        self._fs_timer.setSingleShot(True)
        self._fs_timer.timeout.connect(self._hide_fs_controls)
        # Coalesces save_config writes: a slider drag fires valueChanged
        # many times, so the persistence write is deferred until the drag
        # settles (single shot, ~400 ms).
        self._mic_save_timer = QTimer(self)
        self._mic_save_timer.setSingleShot(True)
        self._mic_save_timer.setInterval(400)
        self._mic_save_timer.timeout.connect(self._save_mic_config)
        self._banner_key = ""

        # Central widget with vertical layout: video + controls
        central = QWidget(self)
        self._central = central
        self._central.installEventFilter(self)
        self.setCentralWidget(central)
        self._main_layout = QVBoxLayout(central)
        self._main_layout.setContentsMargins(12, 12, 12, 12)
        self._main_layout.setSpacing(10)
        layout = self._main_layout

        # Video area: hwnd anchor (X11) + software-render label (Wayland),
        # stacked in the same grid cell; only one is visible at a time.
        self._video_area = VideoArea(self)
        video_grid = QGridLayout(self._video_area)
        video_grid.setContentsMargins(0, 0, 0, 0)
        self._video = VideoWidget(self._video_area)
        self._frame_label = QLabel(self._video_area)
        self._frame_label.setStyleSheet("background-color: #050508;")
        self._frame_label.setScaledContents(True)
        self._frame_label.hide()
        video_grid.addWidget(self._video, 0, 0)
        video_grid.addWidget(self._frame_label, 0, 0)
        layout.addWidget(self._video_area, stretch=1)

        # Warning banner (hidden by default)
        self._banner = QLabel(self)
        self._banner.setObjectName("BannerLabel")
        self._banner.setWordWrap(True)
        self._banner.setAlignment(Qt.AlignCenter)
        self._banner.hide()
        layout.addWidget(self._banner)

        # Bottom control bar
        control_bar = QWidget(self)
        control_layout = QHBoxLayout(control_bar)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(10)

        self._btn_prev = QPushButton(tr("上一首"), self)
        self._btn_prev.setObjectName("ToolButton")
        self._btn_prev.clicked.connect(controller.prev)
        control_layout.addWidget(self._btn_prev)

        self._btn_play = QPushButton(tr("播放"), self)
        self._btn_play.setObjectName("AccentButton")
        self._btn_play.clicked.connect(controller.toggle_pause)
        control_layout.addWidget(self._btn_play)

        self._btn_next = QPushButton(tr("下一首"), self)
        self._btn_next.setObjectName("ToolButton")
        self._btn_next.clicked.connect(controller.next)
        control_layout.addWidget(self._btn_next)

        self._btn_replay = QPushButton(tr("重播"), self)
        self._btn_replay.setObjectName("ToolButton")
        self._btn_replay.setToolTip(tr("重新播放当前歌曲"))
        self._btn_replay.clicked.connect(controller.replay)
        control_layout.addWidget(self._btn_replay)

        self._btn_pitch_down = QPushButton(tr("降调"), self)
        self._btn_pitch_down.setObjectName("ToolButton")
        self._btn_pitch_down.clicked.connect(lambda: controller.change_pitch(-1))
        control_layout.addWidget(self._btn_pitch_down)

        self._btn_pitch_up = QPushButton(tr("升调"), self)
        self._btn_pitch_up.setObjectName("ToolButton")
        self._btn_pitch_up.clicked.connect(lambda: controller.change_pitch(1))
        control_layout.addWidget(self._btn_pitch_up)

        self._pitch_label = QLabel(tr("原调"), self)
        self._pitch_label.setObjectName("PitchLabel")
        self._pitch_label.setMinimumWidth(56)
        control_layout.addWidget(self._pitch_label)

        self._btn_track = QPushButton(tr("原唱/伴奏"), self)
        self._btn_track.setObjectName("ToolButton")
        self._btn_track.setCheckable(True)
        self._btn_track.setEnabled(False)
        self._btn_track.setToolTip(tr("该歌曲没有可切换的音轨"))
        self._btn_track.clicked.connect(controller.toggle_audio_track)
        control_layout.addWidget(self._btn_track)

        self._title_label = QLabel(tr("未在播放"), self)
        self._title_label.setObjectName("CurrentSongLabel")
        self._title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        control_layout.addWidget(self._title_label, stretch=1)

        self._position_label = QLabel("- / -", self)
        self._position_label.setObjectName("PositionLabel")
        control_layout.addWidget(self._position_label)

        self._state_label = QLabel(tr("停止"), self)
        self._state_label.setObjectName("StateLabel")
        control_layout.addWidget(self._state_label)

        self._btn_fullscreen = QPushButton(tr("全屏"), self)
        self._btn_fullscreen.setObjectName("ToolButton")
        self._btn_fullscreen.setToolTip(tr("全屏 (F11)"))
        self._btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        control_layout.addWidget(self._btn_fullscreen)

        self._btn_mixer = QPushButton(tr("混音台"), self)
        self._btn_mixer.setObjectName("ToolButton")
        self._btn_mixer.setCheckable(True)
        self._btn_mixer.toggled.connect(self._on_mixer_button_toggled)
        control_layout.addWidget(self._btn_mixer)

        # Seek row: slider (stretch) + time label on its own row
        seek_row = QWidget(self)
        seek_layout = QHBoxLayout(seek_row)
        seek_layout.setContentsMargins(0, 0, 0, 0)
        seek_layout.setSpacing(10)
        self._seek = SeekSlider(Qt.Horizontal, seek_row)
        self._seek.setRange(0, 0)
        self._seek.setEnabled(False)
        self._seek.setToolTip(tr("拖动或点击跳转播放位置"))
        seek_layout.addWidget(self._seek, stretch=1)
        self._time_label = QLabel("-:-- / -:--", self)
        self._time_label.setObjectName("TimeLabel")
        seek_layout.addWidget(self._time_label)

        # Seek row above the existing bottom control bar
        controls = QWidget(self)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        controls_layout.addWidget(seek_row)
        controls_layout.addWidget(control_bar)
        self._control_bar = controls
        layout.addWidget(controls)

        # Software video output bridge (created on demand for Wayland /
        # X11 fallback; kept alive for the lifetime of this window).
        self._bridge: FrameBridge | None = None

        # Connect controller signals
        self._controller.current_changed.connect(self._on_current_changed)
        self._controller.state_changed.connect(self._on_state_changed)
        self._controller.status_message.connect(self._on_status_message)
        self._controller.audio_track_changed.connect(self._on_audio_track)
        self._controller.pitch_changed.connect(self._on_pitch)
        self._controller.pitch_status.connect(self._on_pitch_status)
        self._controller.pitch_progress.connect(self._on_pitch_progress)
        self._pitch_semis = controller.pitch_semitones
        self._shifting = False
        self._shifting_frac = 0.0
        self._update_pitch_tooltip()
        self._update_pitch_label()

        self._seek.seekRequested.connect(self._on_seek_requested)
        self._seek.sliderMoved.connect(self._on_slider_moved)
        self._seek.sliderReleased.connect(self._on_slider_released)

        # Poll the controller a few times a second to keep the seek row in
        # step with playback (skipped while the user drags the handle).
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(500)
        self._progress_timer.timeout.connect(self._update_progress)
        self._progress_timer.start()

        # Re-translate every label when the language changes.
        on_language_changed(self.retranslate)

        # Defer video output setup until winId is valid
        QTimer.singleShot(0, self._setup_video_output)

        self._update_from_controller()
        self._update_progress()

    def _setup_video_output(self) -> None:
        platform = QGuiApplication.platformName()
        if video_output_mode(platform) == "hwnd":
            try:
                wid = int(self._video.winId())
            except Exception:  # noqa: BLE001
                wid = 0
            if wid and self._controller.set_video_output(wid):
                return  # Hardware path (X11 / XWayland)
        self._enable_software_video()

    def _enable_software_video(self) -> None:
        """Wayland (or X11 fallback): render frames via libvlc callbacks."""
        self._bridge = FrameBridge(self)
        self._bridge.frame_ready.connect(self._on_frame)
        if self._controller.attach_video_callbacks(
            *self._bridge.video_callbacks, *self._bridge.format_callbacks
        ):
            self._video.hide()
            self._frame_label.show()
        else:
            self._show_status(tr("警告：无法设置视频输出（请确认 VLC 已安装）"))

    def _on_frame(self, img: QImage) -> None:
        self._frame_label.setPixmap(QPixmap.fromImage(img))

    def _on_current_changed(self, index: int) -> None:
        if index == -1:
            self._frame_label.clear()
            self._reset_progress()
        self._update_from_controller()
        self._btn_track.setEnabled(self._controller.is_playing and self._controller.has_multi_audio_track())

    def _update_state_label(self, state: str) -> None:
        if state == "playing":
            self._btn_play.setText(tr("暂停"))
            self._state_label.setText(tr("播放中"))
        elif state == "paused":
            self._btn_play.setText(tr("播放"))
            self._state_label.setText(tr("已暂停"))
        else:
            self._btn_play.setText(tr("播放"))
            self._state_label.setText(tr("停止"))

    def _on_state_changed(self, state: str) -> None:
        self._update_state_label(state)
        if state not in ("playing", "paused"):
            self._frame_label.clear()
            self._reset_progress()
        self._btn_track.setEnabled(self._controller.is_playing and self._controller.has_multi_audio_track())

    def _update_track_tooltip(self) -> None:
        index = self._controller.audio_track_index
        multi = self._controller.has_multi_audio_track()
        if multi and index:
            tooltip = tr("当前音轨：伴奏")
        elif multi:
            tooltip = tr("当前音轨：原唱")
        else:
            tooltip = tr("该歌曲没有可切换的音轨")
        self._btn_track.setToolTip(tooltip)

    def _on_audio_track(self, index: int) -> None:
        multi = self._controller.has_multi_audio_track()
        playing = self._controller.is_playing
        self._btn_track.setEnabled(multi and playing)
        self._btn_track.setChecked(index == 1)
        self._update_track_tooltip()

    def _update_pitch_tooltip(self) -> None:
        self._pitch_label.setToolTip(
            tr("纯变调（rubberband，首次使用需后台生成，约 1 分钟）")
            if self._controller.pitch_is_pure
            else tr("缺少 ffmpeg：变调同时改变速度")
        )

    def _on_pitch(self, semis: int) -> None:
        self._pitch_semis = semis
        self._update_pitch_tooltip()
        self._update_pitch_label()

    def _on_pitch_status(self, status: str) -> None:
        self._shifting = status == "shifting"
        self._update_pitch_label()

    def _on_pitch_progress(self, frac: float) -> None:
        self._shifting_frac = frac
        self._update_pitch_label()

    def _update_pitch_label(self) -> None:
        semis = self._pitch_semis
        if semis == 0:
            text = tr("原调")
        elif semis > 0:
            text = tr("升{semis}", semis=semis)
        else:
            text = tr("降{semis}", semis=-semis)
        if self._shifting:
            text += f" · {tr('生成{pct}%', pct=int(self._shifting_frac * 100))}"
        self._pitch_label.setText(text)

    def _show_status(self, key: str) -> None:
        self._banner_key = key
        self._banner.setText(tr(key))
        self._banner.show()

    def _on_status_message(self, message: str) -> None:
        if message:
            self._show_status(message)
        else:
            self._banner_key = ""
            self._banner.hide()

    def _update_from_controller(self) -> None:
        song = self._controller.current_song
        if song is not None:
            self._title_label.setText(song.display)
            total = len(self._controller.queue)
            idx = self._controller.current_index + 1
            self._position_label.setText(f"{idx}/{total}")
        else:
            self._title_label.setText(tr("未在播放"))
            self._position_label.setText("- / -")

    # ===== Progress / seek =====

    def _update_progress(self) -> None:
        """Poll the controller and refresh the seek row.

        Skipped while the user is dragging the handle (the drag itself is
        reflected live by ``sliderMoved``).
        """
        if self._seek.isSliderDown():
            return
        length = self._controller.media_length_ms()
        if length <= 0:
            self._reset_progress()
            return
        position = self._controller.playback_position_ms()
        if position < 0:
            position = 0
        position = min(position, length)
        self._seek.setRange(0, length)
        self._seek.setEnabled(True)
        self._seek.setValue(position)
        self._time_label.setText(
            f"{_format_time_ms(position)} / {_format_time_ms(length)}"
        )

    def _reset_progress(self) -> None:
        """Nothing playing / unknown duration: slider off, '-:-- / -:--'."""
        self._seek.setRange(0, 0)
        self._seek.setValue(0)
        self._seek.setEnabled(False)
        self._time_label.setText("-:-- / -:--")

    def _on_seek_requested(self, ms: int) -> None:
        """Press on the groove: seek immediately and update the label."""
        length = self._controller.media_length_ms()
        if length > 0:
            self._time_label.setText(
                f"{_format_time_ms(ms)} / {_format_time_ms(length)}"
            )
        self._controller.seek_to_ms(ms)

    def _on_slider_moved(self, value: int) -> None:
        """Live label update while dragging; the seek happens on release."""
        length = self._seek.maximum()
        if length <= 0:
            return
        self._time_label.setText(
            f"{_format_time_ms(value)} / {_format_time_ms(length)}"
        )

    def _on_slider_released(self, value: int) -> None:
        """Seek to the position where the handle was released."""
        self._controller.seek_to_ms(value)

    # ===== Fullscreen =====

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._video.setStyleSheet("background-color: #050508;")
        self.showFullScreen()
        self._show_fs_controls()

    def _exit_fullscreen(self) -> None:
        self._fs_timer.stop()
        self._main_layout.setContentsMargins(12, 12, 12, 12)
        self._video.setStyleSheet("background-color: #050508; border-radius: 8px;")
        self._control_bar.show()
        self.showNormal()

    def _show_fs_controls(self) -> None:
        """Reveal the control bar; hide it again after a short idle delay."""
        if not self.isFullScreen():
            return
        self._control_bar.show()
        self._fs_timer.start(2500)

    def _hide_fs_controls(self) -> None:
        if self.isFullScreen():
            self._control_bar.hide()

    def _sync_fullscreen_ui(self) -> None:
        if hasattr(self, "_btn_fullscreen"):
            self._btn_fullscreen.setText(
                tr("退出全屏") if self.isFullScreen() else tr("全屏")
            )

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.WindowStateChange:
            self._sync_fullscreen_ui()
        super().changeEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._central and event.type() in (
            QEvent.MouseMove,
            QEvent.MouseButtonPress,
            QEvent.Enter,
        ):
            self._show_fs_controls()
        return super().eventFilter(obj, event)

    # ===== Mic mixer (混音台) =====

    def _on_mixer_button_toggled(self, checked: bool) -> None:
        """The toolbar button mirrors the dialog: checked == dialog open."""
        if checked:
            self._open_mixer_panel()
        elif self._mixer_dialog is not None:
            self._mixer_dialog.close()

    def _open_mixer_panel(self) -> None:
        """Build (or re-raise) the mic mixer dialog.

        Mirrors the select window's loudness panel: one reusable
        ``QDialog`` per window; the reference is cleared on ``finished``
        so the next click builds a fresh one.
        """
        if self._mixer_dialog is not None:
            self._mixer_dialog.raise_()
            self._mixer_dialog.activateWindow()
            return

        cfg = self._config
        enabled = bool(cfg.mic_enabled) if cfg is not None else True
        gain = int(round(cfg.mic_gain_db)) if cfg is not None else 0
        echo = int(round(cfg.mic_echo * 100.0)) if cfg is not None else 35
        bass = int(round(cfg.mic_bass_db)) if cfg is not None else 0
        treble = int(round(cfg.mic_treble_db)) if cfg is not None else 0
        device = cfg.mic_device if cfg is not None else ""

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("混音台"))
        layout = QVBoxLayout(dlg)
        layout.setSpacing(8)

        self._mic_enable_btn = QPushButton(tr("启用麦克风"), dlg)
        self._mic_enable_btn.setObjectName("ToolButton")
        self._mic_enable_btn.setCheckable(True)
        self._mic_enable_btn.setChecked(enabled)
        layout.addWidget(self._mic_enable_btn)

        def slider_row(
            title: str, minimum: int, maximum: int, value: int, fmt: str
        ) -> tuple[QSlider, QLabel]:
            row = QHBoxLayout()
            row.addWidget(QLabel(title, dlg))
            slider = QSlider(Qt.Horizontal, dlg)
            slider.setRange(minimum, maximum)
            slider.setSingleStep(1)
            slider.setValue(value)
            row.addWidget(slider, stretch=1)
            value_label = QLabel(fmt.format(value), dlg)
            value_label.setObjectName("StateLabel")
            value_label.setFixedWidth(60)
            value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(value_label)
            layout.addLayout(row)
            return slider, value_label

        self._mic_gain_slider, self._mic_gain_value = slider_row(
            tr("麦克风音量"), -24, 24, gain, "{:+d} dB"
        )
        self._mic_echo_slider, self._mic_echo_value = slider_row(
            tr("回声"), 0, 100, echo, "{}%"
        )
        self._mic_bass_slider, self._mic_bass_value = slider_row(
            tr("低音"), -12, 12, bass, "{:+d} dB"
        )
        self._mic_treble_slider, self._mic_treble_value = slider_row(
            tr("高音"), -12, 12, treble, "{:+d} dB"
        )

        device_row = QHBoxLayout()
        device_row.addWidget(QLabel(tr("输入设备"), dlg))
        self._mic_device_combo = QComboBox(dlg)
        # First item = system default; the rest carry the device NAME as
        # item data (sounddevice treats strings as name prefixes, an index
        # would be unstable across launches).
        self._mic_device_combo.addItem(tr("系统默认"), "")
        for _index, name in list_input_devices():
            self._mic_device_combo.addItem(name, name)
        index = self._mic_device_combo.findData(device)
        if index >= 0:
            self._mic_device_combo.setCurrentIndex(index)
        elif device:
            # The stored device is no longer in the list (unplugged or
            # renamed): keep it as an extra selectable item so it is not
            # silently reset to 系统默认 on the next change.
            self._mic_device_combo.addItem(device, device)
            self._mic_device_combo.setCurrentIndex(
                self._mic_device_combo.count() - 1
            )
        device_row.addWidget(self._mic_device_combo, stretch=1)
        layout.addLayout(device_row)

        self._mic_status_label = QLabel(dlg)
        self._mic_status_label.setObjectName("StateLabel")
        layout.addWidget(self._mic_status_label)

        warning = QLabel(tr("建议佩戴耳机，避免啸叫"), dlg)
        warning.setObjectName("BannerLabel")
        layout.addWidget(warning)

        # All initial values are set before any signal is wired, so
        # building the panel never fires a change handler.
        self._mic_enable_btn.toggled.connect(self._on_mic_enabled_toggled)
        self._mic_gain_slider.valueChanged.connect(self._on_mic_gain_changed)
        self._mic_echo_slider.valueChanged.connect(self._on_mic_echo_changed)
        self._mic_bass_slider.valueChanged.connect(self._on_mic_bass_changed)
        self._mic_treble_slider.valueChanged.connect(self._on_mic_treble_changed)
        self._mic_device_combo.currentIndexChanged.connect(
            self._on_mic_device_changed
        )

        # The stream can only start/stop while the dialog is open (playback
        # state transitions), so a short timer keeps the status line honest.
        self._mic_status_timer = QTimer(dlg)
        self._mic_status_timer.setInterval(500)
        self._mic_status_timer.timeout.connect(self._update_mic_status)
        self._mic_status_timer.start()

        self._update_mic_status()

        self._mixer_dialog = dlg
        dlg.setAttribute(Qt.WA_DeleteOnClose)

        def _on_dialog_finished(_result: int) -> None:
            self._mixer_dialog = None
            self._mic_enable_btn = None
            self._mic_gain_slider = None
            self._mic_gain_value = None
            self._mic_echo_slider = None
            self._mic_echo_value = None
            self._mic_bass_slider = None
            self._mic_bass_value = None
            self._mic_treble_slider = None
            self._mic_treble_value = None
            self._mic_device_combo = None
            self._mic_status_label = None
            self._mic_status_timer = None
            # Closing the dialog unchecks the toolbar button (signals
            # blocked so the toggled slot never sees a stale dialog).
            self._btn_mixer.blockSignals(True)
            self._btn_mixer.setChecked(False)
            self._btn_mixer.blockSignals(False)

        dlg.finished.connect(_on_dialog_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _save_mic_config(self) -> None:
        """Persist the in-memory mic settings (no-op without a config).

        Called by the debounce timer after a slider drag settles, and
        immediately by the enable-toggle and device handlers (single user
        actions, so nothing to coalesce). Cancels any pending debounced
        write so an action never results in a double save.
        """
        self._mic_save_timer.stop()
        if self._config is not None:
            save_config(self._config)

    def _on_mic_enabled_toggled(self, checked: bool) -> None:
        self._controller.set_mic_enabled(checked)
        if self._config is not None:
            self._config.mic_enabled = checked
        self._save_mic_config()
        self._update_mic_status()

    def _on_mic_gain_changed(self, value: int) -> None:
        self._mic_gain_value.setText(f"{value:+d} dB")
        self._apply_mic_settings()

    def _on_mic_echo_changed(self, value: int) -> None:
        self._mic_echo_value.setText(f"{value}%")
        self._apply_mic_settings()

    def _on_mic_bass_changed(self, value: int) -> None:
        self._mic_bass_value.setText(f"{value:+d} dB")
        self._apply_mic_settings()

    def _on_mic_treble_changed(self, value: int) -> None:
        self._mic_treble_value.setText(f"{value:+d} dB")
        self._apply_mic_settings()

    def _on_mic_device_changed(self, _index: int) -> None:
        self._apply_mic_settings()
        self._save_mic_config()

    def _apply_mic_settings(self) -> None:
        """Push the current mixer controls to the controller and persist.

        The in-memory config is updated immediately; the ``save_config``
        write is coalesced by the single-shot debounce timer so a slider
        drag (many ``valueChanged`` signals) writes once.
        """
        device = self._mic_device_combo.currentData() or ""
        enabled = self._mic_enable_btn.isChecked()
        self._controller.configure_mic(
            enabled=enabled,
            gain_db=float(self._mic_gain_slider.value()),
            echo=self._mic_echo_slider.value() / 100.0,
            bass_db=float(self._mic_bass_slider.value()),
            treble_db=float(self._mic_treble_slider.value()),
            device=device,
        )
        if self._config is not None:
            self._config.mic_enabled = enabled
            self._config.mic_gain_db = float(self._mic_gain_slider.value())
            self._config.mic_echo = self._mic_echo_slider.value() / 100.0
            self._config.mic_bass_db = float(self._mic_bass_slider.value())
            self._config.mic_treble_db = float(self._mic_treble_slider.value())
            self._config.mic_device = device
            self._mic_save_timer.start()  # debounced persistence
        self._update_mic_status()

    def _update_mic_status(self) -> None:
        mixer = self._controller.mic_mixer
        if mixer is None:
            text = tr("麦克风不可用")
        elif mixer.is_running():
            text = tr("麦克风：已启用")
        elif (
            self._controller.state == "playing"
            and self._mic_enable_btn is not None
            and self._mic_enable_btn.isChecked()
        ):
            # Enabled and playing, yet the stream is not running: start()
            # failed (or the DSP latched an error) -- say so instead of
            # pretending the mic is simply off.
            text = getattr(mixer, "last_error", None) or tr("麦克风不可用")
        else:
            # Stopped: "not running" is the normal condition.
            text = tr("麦克风：未启用")
        if self._mic_status_label is not None:
            self._mic_status_label.setText(text)

    # ===== Language =====

    def retranslate(self) -> None:
        """Re-apply the active language to every label and tooltip."""
        self.setWindowTitle(tr("ezkaraoke · 播放器"))
        self._btn_prev.setText(tr("上一首"))
        self._btn_next.setText(tr("下一首"))
        self._btn_replay.setText(tr("重播"))
        self._btn_replay.setToolTip(tr("重新播放当前歌曲"))
        self._btn_pitch_down.setText(tr("降调"))
        self._btn_pitch_up.setText(tr("升调"))
        self._btn_track.setText(tr("原唱/伴奏"))
        self._btn_fullscreen.setToolTip(tr("全屏 (F11)"))
        self._btn_mixer.setText(tr("混音台"))
        if self._mixer_dialog is not None:
            self._mixer_dialog.setWindowTitle(tr("混音台"))
        self._seek.setToolTip(tr("拖动或点击跳转播放位置"))
        self._update_state_label(self._controller.state)
        self._update_track_tooltip()
        self._update_pitch_tooltip()
        self._update_pitch_label()
        self._update_from_controller()
        self._sync_fullscreen_ui()
        if self._banner.isVisible():
            self._banner.setText(tr(self._banner_key))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_F11:
            self.toggle_fullscreen()
            return
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self._exit_fullscreen()
            return
        if event.key() == Qt.Key_Space:
            self._controller.toggle_pause()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._progress_timer.stop()
        if self._bridge is not None:
            self._controller.detach_video_callbacks()
        self._controller.shutdown_shift()
        self._controller.stop()
        off_language_changed(self.retranslate)
        super().closeEvent(event)
