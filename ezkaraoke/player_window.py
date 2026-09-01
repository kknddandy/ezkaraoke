from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer
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

from ezkaraoke.frame_bridge import FrameBridge, video_output_mode
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
            if window.isFullScreen():
                window.showNormal()
            else:
                window.showFullScreen()
        super().mouseDoubleClickEvent(event)


class PlayerWindow(QMainWindow):
    def __init__(self, controller: PlayerController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("ezkaraoke · 播放器")
        self.resize(1024, 640)

        # Central widget with vertical layout: video + controls
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

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

        self._btn_prev = QPushButton("上一首", self)
        self._btn_prev.setObjectName("ToolButton")
        self._btn_prev.clicked.connect(controller.prev)
        control_layout.addWidget(self._btn_prev)

        self._btn_play = QPushButton("播放", self)
        self._btn_play.setObjectName("AccentButton")
        self._btn_play.clicked.connect(controller.toggle_pause)
        control_layout.addWidget(self._btn_play)

        self._btn_next = QPushButton("下一首", self)
        self._btn_next.setObjectName("ToolButton")
        self._btn_next.clicked.connect(controller.next)
        control_layout.addWidget(self._btn_next)

        self._btn_pitch_down = QPushButton("降调", self)
        self._btn_pitch_down.setObjectName("ToolButton")
        self._btn_pitch_down.clicked.connect(lambda: controller.change_pitch(-1))
        control_layout.addWidget(self._btn_pitch_down)

        self._btn_pitch_up = QPushButton("升调", self)
        self._btn_pitch_up.setObjectName("ToolButton")
        self._btn_pitch_up.clicked.connect(lambda: controller.change_pitch(1))
        control_layout.addWidget(self._btn_pitch_up)

        self._pitch_label = QLabel("原调", self)
        self._pitch_label.setObjectName("PitchLabel")
        self._pitch_label.setMinimumWidth(56)
        control_layout.addWidget(self._pitch_label)

        self._btn_track = QPushButton("原唱/伴奏", self)
        self._btn_track.setObjectName("ToolButton")
        self._btn_track.setCheckable(True)
        self._btn_track.setEnabled(False)
        self._btn_track.setToolTip("该歌曲没有可切换的音轨")
        self._btn_track.clicked.connect(controller.toggle_audio_track)
        control_layout.addWidget(self._btn_track)

        self._title_label = QLabel("未在播放", self)
        self._title_label.setObjectName("CurrentSongLabel")
        self._title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        control_layout.addWidget(self._title_label, stretch=1)

        self._position_label = QLabel("- / -", self)
        self._position_label.setObjectName("PositionLabel")
        control_layout.addWidget(self._position_label)

        self._state_label = QLabel("停止", self)
        self._state_label.setObjectName("StateLabel")
        control_layout.addWidget(self._state_label)

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
        self._pitch_base = "原调"
        self._shifting = False
        self._shifting_frac = 0.0

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
            self._banner.setText("警告：无法设置视频输出（请确认 VLC 已安装）")
            self._banner.show()

    def _on_frame(self, img: QImage) -> None:
        self._frame_label.setPixmap(QPixmap.fromImage(img))

    def _on_current_changed(self, index: int) -> None:
        if index == -1:
            self._frame_label.clear()
        self._update_from_controller()
        self._btn_track.setEnabled(self._controller.is_playing and self._controller.has_multi_audio_track())

    def _on_state_changed(self, state: str) -> None:
        if state == "playing":
            self._btn_play.setText("暂停")
            self._state_label.setText("播放中")
        elif state == "paused":
            self._btn_play.setText("播放")
            self._state_label.setText("已暂停")
        else:
            self._btn_play.setText("播放")
            self._state_label.setText("停止")
            self._frame_label.clear()
        self._btn_track.setEnabled(self._controller.is_playing and self._controller.has_multi_audio_track())

    def _on_audio_track(self, index: int) -> None:
        multi = self._controller.has_multi_audio_track()
        playing = self._controller.is_playing
        self._btn_track.setEnabled(multi and playing)
        self._btn_track.setChecked(index == 1)
        self._btn_track.setToolTip(
            f"当前音轨：{'伴奏' if index else '原唱'}" if multi else "该歌曲没有可切换的音轨"
        )

    def _on_pitch(self, semis: int) -> None:
        if semis == 0:
            self._pitch_base = "原调"
        elif semis > 0:
            self._pitch_base = f"升{semis}"
        else:
            self._pitch_base = f"降{-semis}"
        self._pitch_label.setToolTip(
            "纯变调（rubberband，首次使用需后台生成，约 1 分钟）"
            if self._controller.pitch_is_pure
            else "缺少 ffmpeg：变调同时改变速度"
        )
        self._update_pitch_label()

    def _on_pitch_status(self, status: str) -> None:
        self._shifting = status == "shifting"
        self._update_pitch_label()

    def _on_pitch_progress(self, frac: float) -> None:
        self._shifting_frac = frac
        self._update_pitch_label()

    def _update_pitch_label(self) -> None:
        text = self._pitch_base
        if self._shifting:
            text += f" · 生成{int(self._shifting_frac * 100)}%"
        self._pitch_label.setText(text)

    def _on_status_message(self, message: str) -> None:
        if message:
            self._banner.setText(message)
            self._banner.show()
        else:
            self._banner.hide()

    def _update_from_controller(self) -> None:
        song = self._controller.current_song
        if song is not None:
            self._title_label.setText(song.display)
            total = len(self._controller.queue)
            idx = self._controller.current_index + 1
            self._position_label.setText(f"{idx}/{total}")
        else:
            self._title_label.setText("未在播放")
            self._position_label.setText("- / -")

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
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
        super().closeEvent(event)
