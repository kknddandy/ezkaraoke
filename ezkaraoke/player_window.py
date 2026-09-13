from pathlib import Path
import sys

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage, QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ezkaraoke import i18n
from ezkaraoke.frame_bridge import FrameBridge, video_output_mode
from ezkaraoke.i18n import off_language_changed, on_language_changed, tr
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


class PlayerWindow(QMainWindow):
    def __init__(self, controller: PlayerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle(tr("ezkaraoke · 播放器"))
        self.resize(1024, 640)
        self._fs_timer = QTimer(self)
        self._fs_timer.setSingleShot(True)
        self._fs_timer.timeout.connect(self._hide_fs_controls)
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

        self._control_bar = control_bar
        layout.addWidget(control_bar)

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

        # Re-translate every label when the language changes.
        on_language_changed(self.retranslate)

        # Defer video output setup until winId is valid
        QTimer.singleShot(0, self._setup_video_output)

        self._update_from_controller()

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
        if self._bridge is not None:
            self._controller.detach_video_callbacks()
        self._controller.shutdown_shift()
        self._controller.stop()
        off_language_changed(self.retranslate)
        super().closeEvent(event)
