from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStatusBar,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ezkaraoke import i18n
from ezkaraoke.avatar import AvatarRenderer, AvatarWorker, clean_image_data
from ezkaraoke.config import Config, save_config
from ezkaraoke.database import SongDatabase
from ezkaraoke.i18n import on_language_changed, off_language_changed, tr
from ezkaraoke.letters import pinyin_key
from ezkaraoke.library import Song
from ezkaraoke.loudness import LoudnessWorker
from ezkaraoke.mic_mixer import list_input_devices
from ezkaraoke.player import PlayerController
from ezkaraoke.pitchshift import remove_cached_for
from ezkaraoke.scanner import ScanWorker, SizeBackfillWorker, parse_version
from ezkaraoke.web_server import WebServer, qr_pixmap


def placeholder_label(text: str) -> str:
    """Placeholder icon text for *text*.

    CJK names get "中D-丁" (pinyin initial letter made explicit so the
    sort order is visible at a glance); other names keep the first char.
    The letter comes from the whole name, not the first char alone, so
    pypinyin's phrase disambiguation applies (长春 -> ch, not zh).
    """
    stripped = text.strip()
    ch = stripped[:1]
    if ch and not ch.isascii():
        key = pinyin_key(stripped)
        if key and key[0]:
            letter = key[0][0].upper()
            if letter.isascii() and letter.isalpha():
                return f"中{letter}-{ch}"
    return ch or "·"


def make_placeholder_pixmap(text: str, size: int = 40) -> QPixmap:
    """Rounded square with the placeholder label of *text* centered."""
    label = placeholder_label(text)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2a2a3e"))
    painter.drawRoundedRect(0, 0, size, size, 6, 6)
    painter.setPen(QColor("#a0a0b0"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(max(10, size // (3 if len(label) <= 1 else 5)))
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, label)
    painter.end()
    return pixmap

class _SongTableModel(QAbstractTableModel):
    """Song table rows: the currently displayed songs in display order.

    Sorting happens in Python (pinyin keys), because Qt's built-in text
    comparison does not match Chinese order. The trailing invisible
    spacer column keeps the 文件尺寸 resize handle off the table's right
    edge (see the construction site).
    """

    COLUMNS = 5  # 歌手 / 歌名 / 版本 / 文件尺寸 / spacer

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._songs: list[Song] = []

    # ------------------------------------------------------------- data
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._songs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self.COLUMNS

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        song = self._songs[index.row()]
        col = index.column()
        if col == self.COLUMNS - 1:
            return None  # trailing spacer
        if role == Qt.DisplayRole:
            if col == 0:
                return song.artist
            if col == 1:
                return song.title
            if col == 2:
                return parse_version(song.path)
            return format_size(song.size)
        if role == Qt.TextAlignmentRole and col == 3:
            return int(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
        if role == Qt.UserRole and col == 0:
            return song.path
        return None

    def headerData(self, section: int, orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return (tr("歌手"), tr("歌名"), tr("版本"), tr("文件尺寸"), "")[section]
        return None

    def retranslate(self) -> None:
        """Re-emit the horizontal header labels in the active language."""
        self.headerDataChanged.emit(
            Qt.Horizontal, 0, self.COLUMNS - 1
        )

    # -------------------------------------------------------- mutations
    def set_songs(self, songs: list[Song]) -> None:
        self.beginResetModel()
        self._songs = list(songs)
        self.endResetModel()

    def sort(self, column: int, order) -> None:
        """Reorder rows in place (display-role keys, pinyin-aware)."""
        if not self._songs:
            return
        if column == 3:
            # Size (bytes) primary, missing sizes last; artist as tie-break.
            key = lambda s: (  # noqa: E731
                (s.size if s.size is not None else -1),
                pinyin_key(s.artist) + [s.artist],
                s.artist,
            )
        elif column == 2:
            # Version label; artist/title as tie-break.
            key = lambda s: (  # noqa: E731
                parse_version(s.path),
                pinyin_key(s.artist) + [s.artist],
                s.artist,
            )
        elif column == 1:
            key = lambda s: (  # noqa: E731
                pinyin_key(s.title) + [s.title],
                pinyin_key(s.artist) + [s.artist],
            )
        else:
            key = lambda s: (  # noqa: E731
                pinyin_key(s.artist) + [s.artist],
                pinyin_key(s.title) + [s.title],
            )
        self.beginResetModel()
        self._songs.sort(key=key, reverse=(order == Qt.SortOrder.DescendingOrder))
        self.endResetModel()

    def song_at(self, row: int) -> Song | None:
        if 0 <= row < len(self._songs):
            return self._songs[row]
        return None


# Raw title of the queue row; kept on the title cell so the "▶ " marker
# stays display-only (the cell text is rewritten on every refresh).
QUEUE_ROLE_TITLE = Qt.UserRole + 2


def format_size(size: int | None) -> str:
    """Human-readable size: MB by default, GB once >= 1 GiB."""
    if size is None:
        return "—"
    mb = size / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    if mb >= 100:
        return f"{mb:.0f} MB"
    return f"{mb:.1f} MB"


def avatar_pixmap(name: str, data: bytes | None, size: int = 40) -> QPixmap:
    """Avatar with the pinyin letter label always drawn on top.

    With image data the center-cropped photo fills the icon and the
    label ("中D-丁" / "C") is drawn over it with a semi-transparent
    chip for legibility; without data the label sits on the dark
    placeholder. The sort letter stays visible whether or not a photo
    was found.
    """
    label = placeholder_label(name)
    photo = None
    if data:
        data = clean_image_data(data)  # malformed ICC/eXIf trigger qt warnings
        pm = QPixmap()
        if pm.loadFromData(data):
            pm = pm.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (pm.width() - size) // 2
            y = (pm.height() - size) // 2
            photo = pm.copy(x, y, size, size)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setPen(Qt.NoPen)
    clip = QPainterPath()
    clip.addRoundedRect(0, 0, size, size, 6, 6)
    painter.setClipPath(clip)
    if photo is not None:
        painter.drawPixmap(0, 0, photo)
    else:
        painter.setBrush(QColor("#2a2a3e"))
        painter.drawRect(0, 0, size, size)
    font = QFont()
    font.setBold(True)
    font.setPointSize(max(8, size // (3 if len(label) <= 1 else 5)))
    painter.setFont(font)
    fm = QFontMetrics(font)
    text_w = fm.horizontalAdvance(label)
    text_h = fm.height()
    pad = max(2, size // 28)
    chip = QRect(
        (size - text_w) // 2 - pad,
        (size - text_h) // 2 - pad,
        text_w + 2 * pad,
        text_h + 2 * pad,
    )
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(20, 20, 34, 170))
    painter.drawRoundedRect(chip, 4, 4)
    painter.setPen(QColor("#ffffff") if photo is not None else QColor("#a0a0b0"))
    painter.drawText(chip, Qt.AlignCenter, label)
    painter.end()
    return pixmap


class SelectWindow(QMainWindow):
    def __init__(
        self,
        controller: PlayerController,
        db: SongDatabase,
        config: Config,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._db = db
        self._config = config
        # Loudness: the player must not open the DB itself; hand it over and
        # start the playback gain in sync with the persisted config.
        self._controller.attach_database(self._db)
        self._controller.set_loudness_enabled(self._config.loudness_enabled)
        self._controller.set_loudness_target(self._config.loudness_target)
        # Mic: build the mixer from the persisted config. The stream stays
        # closed until playback starts (the player syncs it with the state).
        self._controller.configure_mic(
            enabled=self._config.mic_enabled,
            gain_db=self._config.mic_gain_db,
            echo=self._config.mic_echo,
            bass_db=self._config.mic_bass_db,
            treble_db=self._config.mic_treble_db,
            device=self._config.mic_device,
        )
        self._scan_worker: ScanWorker | None = None
        self._avatar_worker: AvatarWorker | None = None
        self._avatar_renderer: AvatarRenderer | None = None
        self._size_worker: SizeBackfillWorker | None = None
        self._loudness_worker: LoudnessWorker | None = None
        self._loudness_dialog: QDialog | None = None
        # The panel's widgets are closure-local; the window-level
        # finished/error slots refresh it through these callbacks, which are
        # set while the dialog is open and cleared when it closes.
        self._loudness_refresh_status = None
        self._loudness_update_buttons = None
        self._loudness_update_toggle_text = None
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
        # Coalesces save_config writes: a slider drag fires valueChanged
        # many times, so the persistence write is deferred until the drag
        # settles (single shot, ~400 ms).
        self._mic_save_timer = QTimer(self)
        self._mic_save_timer.setSingleShot(True)
        self._mic_save_timer.setInterval(400)
        self._mic_save_timer.timeout.connect(self._save_mic_config)
        # A rescan must not start while a size backfill is writing to the
        # songs table (SQLite lock contention); instead of blocking the GUI
        # thread on the backfill, a short timer starts the scan as soon as
        # the backfill exits. See _start_scan.
        self._scan_defer_timer = QTimer(self)
        self._scan_defer_timer.setInterval(200)
        self._scan_defer_timer.timeout.connect(self._try_begin_scan)
        self._mode: str = "artist"
        self._current_artist: str | None = None
        self._current_letter: str | None = None
        self._search_text: str = ""
        self._avatar_cache: dict[str, QPixmap] = {}
        self._artist_rows: dict[str, int] = {}

        self.setWindowTitle(tr("ezkaraoke · 点歌台"))
        self.resize(1280, 760)

        # Main splitter: left lists | center | right queue
        splitter = QSplitter(Qt.Horizontal, self)
        self.setCentralWidget(splitter)

        # ===== Left: Mode toggle + artist / letter lists =====
        left_widget = QWidget(self)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(8)

        mode_row = QWidget(self)
        mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(6)

        self._btn_mode_artist = QPushButton(tr("歌手"), self)
        self._btn_mode_artist.setObjectName("ModeButton")
        self._btn_mode_artist.setCheckable(True)
        self._btn_mode_artist.setChecked(True)
        self._btn_mode_artist.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._btn_mode_artist.clicked.connect(lambda: self._set_mode("artist"))
        mode_layout.addWidget(self._btn_mode_artist)

        self._btn_mode_letter = QPushButton(tr("首字母"), self)
        self._btn_mode_letter.setObjectName("ModeButton")
        self._btn_mode_letter.setCheckable(True)
        self._btn_mode_letter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._btn_mode_letter.clicked.connect(lambda: self._set_mode("letter"))
        mode_layout.addWidget(self._btn_mode_letter)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self._btn_mode_artist)
        self._mode_group.addButton(self._btn_mode_letter)
        left_layout.addWidget(mode_row)

        # Flexible width (fills the splitter pane, no dead space on wide
        # screens); oversized icons and text for a touch-friendly selection UI.
        self._artist_list = QListWidget(self)
        self._artist_list.setMinimumWidth(440)
        self._artist_list.setIconSize(QSize(112, 112))
        self._artist_list.itemSelectionChanged.connect(self._on_artist_selection_changed)
        left_layout.addWidget(self._artist_list, stretch=1)

        self._letter_list = QListWidget(self)
        self._letter_list.setMinimumWidth(440)
        self._letter_list.setIconSize(QSize(112, 112))
        self._letter_list.itemSelectionChanged.connect(self._on_letter_selection_changed)
        self._letter_list.hide()
        left_layout.addWidget(self._letter_list, stretch=1)

        splitter.addWidget(left_widget)

        # ===== Center: Toolbar + Song table + action buttons =====
        center_widget = QWidget(self)
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(4, 8, 4, 8)
        center_layout.setSpacing(10)

        # Toolbar row
        toolbar = QWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)

        self._folder_label = QLabel(self)
        self._folder_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._update_folder_label()
        toolbar_layout.addWidget(self._folder_label)

        self._btn_choose = QPushButton(tr("选择文件夹…"), self)
        self._btn_choose.setObjectName("ToolButton")
        self._btn_choose.clicked.connect(self._choose_folder)
        toolbar_layout.addWidget(self._btn_choose)

        self._btn_rescan = QPushButton(tr("重新扫描"), self)
        self._btn_rescan.setObjectName("ToolButton")
        self._btn_rescan.clicked.connect(self._start_scan)
        toolbar_layout.addWidget(self._btn_rescan)

        self._btn_loudness = QPushButton(tr("响度对齐"), self)
        self._btn_loudness.setObjectName("ToolButton")
        self._btn_loudness.clicked.connect(self._open_loudness_panel)
        toolbar_layout.addWidget(self._btn_loudness)

        self._btn_mixer = QPushButton(tr("混音台"), self)
        self._btn_mixer.setObjectName("ToolButton")
        self._btn_mixer.clicked.connect(self._open_mixer_panel)
        toolbar_layout.addWidget(self._btn_mixer)

        self._btn_lang = QPushButton(self)
        self._btn_lang.setObjectName("ToolButton")
        self._btn_lang.setToolTip("Switch UI language / 切换界面语言")
        self._btn_lang.clicked.connect(self._toggle_language)
        self._update_lang_button()
        toolbar_layout.addWidget(self._btn_lang)

        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText(tr("搜索歌手或歌名…"))
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumWidth(200)
        self._search_edit.textChanged.connect(self._on_search_changed)
        toolbar_layout.addWidget(self._search_edit)

        center_layout.addWidget(toolbar)

        # Song table (click 歌手/歌名/文件尺寸 headers to sort; pinyin order
        # for CJK, numeric for size). #SongTable gets the large display font
        # via theme.qss.
        #
        # A trailing fixed-width spacer keeps the 文件尺寸 resize handle off
        # the table's right edge; otherwise it sits under the 4px splitter
        # handle and dragging there resizes the splitter, not the column.
        self._song_model = _SongTableModel(self)
        self._song_table = QTableView(self)
        self._song_table.setObjectName("SongTable")
        self._song_table.setModel(self._song_model)
        header = self._song_table.horizontalHeader()
        header.setMinimumSectionSize(20)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        self._song_table.setColumnWidth(0, 160)
        self._song_table.setColumnWidth(2, 120)
        self._song_table.setColumnWidth(3, 115)
        self._song_table.setColumnWidth(4, 20)
        # Comfortable breathing room around the 28px display font
        self._song_table.verticalHeader().setDefaultSectionSize(52)
        # Manual pinyin-aware sorting: Qt's built-in sort compares raw
        # text, so we sort by pinyin keys in Python (see _SongTableModel).
        self._sort_column: int | None = None
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._song_table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._song_table.setSelectionBehavior(
            QTableWidget.SelectRows
        )
        self._song_table.setSelectionMode(
            QTableWidget.ExtendedSelection
        )
        self._song_table.setAlternatingRowColors(True)
        self._song_table.setEditTriggers(
            QTableWidget.NoEditTriggers
        )
        self._song_table.doubleClicked.connect(self._on_song_double_clicked)
        # Re-enable the action buttons as soon as the user (de)selects rows;
        # without this they stay disabled until the next table refresh.
        self._song_table.selectionModel().selectionChanged.connect(
            self._update_button_states
        )
        self._song_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._song_table.customContextMenuRequested.connect(self._on_song_context_menu)
        center_layout.addWidget(self._song_table, stretch=1)

        # Action buttons
        action_bar = QWidget(self)
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(10)

        self._btn_append = QPushButton(tr("点歌"), self)
        self._btn_append.setObjectName("AccentButton")
        self._btn_append.clicked.connect(self._append_selected)
        action_layout.addWidget(self._btn_append)

        self._btn_insert = QPushButton(tr("插入播放"), self)
        self._btn_insert.setObjectName("ToolButton")
        self._btn_insert.clicked.connect(self._insert_selected)
        action_layout.addWidget(self._btn_insert)

        self._btn_play_now = QPushButton(tr("立即播放"), self)
        self._btn_play_now.setObjectName("ToolButton")
        self._btn_play_now.clicked.connect(self._play_now_selected)
        action_layout.addWidget(self._btn_play_now)

        self._btn_play = QPushButton(tr("播放"), self)
        self._btn_play.setObjectName("ToolButton")
        self._btn_play.clicked.connect(self._controller.toggle_pause)
        action_layout.addWidget(self._btn_play)

        self._btn_track = QPushButton(tr("原唱/伴奏"), self)
        self._btn_track.setObjectName("ToolButton")
        self._btn_track.setCheckable(True)
        self._btn_track.setEnabled(False)
        self._btn_track.setToolTip(tr("切换当前歌曲的音轨（原唱/伴奏）"))
        self._btn_track.clicked.connect(self._controller.toggle_audio_track)
        action_layout.addWidget(self._btn_track)

        action_layout.addStretch()
        center_layout.addWidget(action_bar)

        splitter.addWidget(center_widget)

        # ===== Right: Phone-ordering QR + Play queue =====
        right_widget = QWidget(self)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(4, 8, 8, 8)
        right_layout.setSpacing(10)

        # Phone ordering: the QR code decodes to the LAN web service URL.
        self._web = WebServer(
            self._db, self._controller, self._config.web_port, parent=self
        )
        self._qr_row = self._build_qr_row()
        right_layout.addWidget(self._qr_row)
        if not self._web.start():
            self._set_qr_unavailable(
                tr("手机点歌服务启动失败（端口 {port} 被占用）", port=self._config.web_port)
            )
        else:
            self._update_qr()

        # Row numbers stay on the vertical header; a separate 序号 column
        # would repeat them. The current song is marked with "▶ " in the
        # title cell. Column 0 is the clickable favorite heart.
        self._queue_table = QTableWidget(self)
        self._queue_table.setColumnCount(3)
        self._queue_table.setHorizontalHeaderLabels(["", tr("歌手"), tr("歌名")])
        self._queue_table.horizontalHeader().setStretchLastSection(True)
        self._queue_table.setColumnWidth(0, 34)
        self._queue_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._queue_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._queue_table.setAlternatingRowColors(True)
        self._queue_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._queue_table.doubleClicked.connect(self._on_queue_double_clicked)
        self._queue_table.itemClicked.connect(self._on_queue_item_clicked)
        right_layout.addWidget(self._queue_table, stretch=1)

        queue_bar = QWidget(self)
        queue_bar_layout = QHBoxLayout(queue_bar)
        queue_bar_layout.setContentsMargins(0, 0, 0, 0)
        queue_bar_layout.setSpacing(8)

        self._btn_jump = QPushButton(tr("插歌"), self)
        self._btn_jump.setObjectName("ToolButton")
        self._btn_jump.setToolTip(tr("把选中的歌曲移到正在播放歌曲的下一首"))
        self._btn_jump.clicked.connect(self._insert_after_current)
        queue_bar_layout.addWidget(self._btn_jump)

        self._btn_up = QPushButton(tr("上移"), self)
        self._btn_up.setObjectName("ToolButton")
        self._btn_up.clicked.connect(self._move_up)
        queue_bar_layout.addWidget(self._btn_up)

        self._btn_down = QPushButton(tr("下移"), self)
        self._btn_down.setObjectName("ToolButton")
        self._btn_down.clicked.connect(self._move_down)
        queue_bar_layout.addWidget(self._btn_down)

        self._btn_remove = QPushButton(tr("删除"), self)
        self._btn_remove.setObjectName("ToolButton")
        self._btn_remove.clicked.connect(self._remove_selected)
        queue_bar_layout.addWidget(self._btn_remove)

        self._btn_queue_favs = QPushButton(tr("红心入队"), self)
        self._btn_queue_favs.setObjectName("ToolButton")
        self._btn_queue_favs.setToolTip(tr("把所有红心歌曲加入队列"))
        self._btn_queue_favs.clicked.connect(self._queue_all_favorites)
        queue_bar_layout.addWidget(self._btn_queue_favs)

        queue_bar_layout.addStretch()
        right_layout.addWidget(queue_bar)

        splitter.addWidget(right_widget)
        # Left pane sized to the oversized list content (~500px on 1920);
        # the pane can still be dragged wider, the list follows.
        splitter.setSizes([340, 640, 300])

        # Status bar (left: summary, right: progress + current song)
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_left = QLabel(self)
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(True)
        self._progress.setFixedWidth(240)
        self._progress.hide()
        self._status_right = QLabel(self)
        self._status_bar.addWidget(self._status_left)
        self._status_bar.addPermanentWidget(self._progress)
        self._status_bar.addPermanentWidget(self._status_right)

        # Connect controller signals
        self._controller.queue_changed.connect(self._refresh_queue)
        self._controller.current_changed.connect(self._highlight_current_queue)
        self._controller.state_changed.connect(self._on_state_changed)
        self._controller.status_message.connect(self._on_status_message)
        self._controller.audio_track_changed.connect(self._on_audio_track)

        # Initial refresh
        self._refresh_artist_list()
        self._refresh_letter_list()
        self._refresh_song_table()
        self._refresh_queue()
        self._update_button_states()

        if self._db.song_count() > 0:
            self._update_status_summary()
            self._start_avatar_worker()
            self._start_avatar_renderer()
            self._start_size_backfill()
        elif self._config.music_folder:
            folder = Path(self._config.music_folder)
            if folder.exists() and folder.is_dir():
                self._start_scan()
            else:
                self._status_left.setText(tr("文件夹不存在"))
        else:
            self._status_left.setText(tr("请设置音乐文件夹"))

        # Re-translate every label when the language changes.
        on_language_changed(self.retranslate)

    # ===== Language =====

    def _update_lang_button(self) -> None:
        # The button always offers the *other* language.
        self._btn_lang.setText("EN" if i18n.current_language() == "zh" else "中文")

    def _toggle_language(self) -> None:
        new = "en" if i18n.current_language() == "zh" else "zh"
        self._config.language = new
        save_config(self._config)
        i18n.set_language(new)  # notifies both windows -> retranslate()

    def retranslate(self) -> None:
        """Re-apply the active language to every label, header and tooltip."""
        self.setWindowTitle(tr("ezkaraoke · 点歌台"))
        self._btn_mode_artist.setText(tr("歌手"))
        self._btn_mode_letter.setText(tr("首字母"))
        self._btn_choose.setText(tr("选择文件夹…"))
        self._btn_rescan.setText(tr("重新扫描"))
        self._btn_loudness.setText(tr("响度对齐"))
        self._btn_mixer.setText(tr("混音台"))
        if self._mixer_dialog is not None:
            self._mixer_dialog.setWindowTitle(tr("混音台"))
        self._btn_lang.setText("EN" if i18n.current_language() == "zh" else "中文")
        self._search_edit.setPlaceholderText(tr("搜索歌手或歌名…"))
        self._song_model.retranslate()
        self._btn_append.setText(tr("点歌"))
        self._btn_insert.setText(tr("插入播放"))
        self._btn_play_now.setText(tr("立即播放"))
        self._btn_play.setText(tr("暂停") if self._controller.is_playing else tr("播放"))
        self._btn_track.setText(tr("原唱/伴奏"))
        self._queue_table.setHorizontalHeaderLabels(["", tr("歌手"), tr("歌名")])
        self._btn_jump.setText(tr("插歌"))
        self._btn_jump.setToolTip(tr("把选中的歌曲移到正在播放歌曲的下一首"))
        self._btn_up.setText(tr("上移"))
        self._btn_down.setText(tr("下移"))
        self._btn_remove.setText(tr("删除"))
        self._btn_queue_favs.setText(tr("红心入队"))
        self._btn_queue_favs.setToolTip(tr("把所有红心歌曲加入队列"))
        self._qr_title.setText(tr("手机扫码点歌"))
        self._update_folder_label()
        self._on_audio_track(self._controller.audio_track_index)
        self._update_status_summary()
        self._highlight_current_queue()
        for lst in (self._artist_list, self._letter_list):
            if lst.count() > 0:
                lst.item(0).setText(tr("全部 ({count})", count=self._db.song_count()))

    def _update_status_summary(self) -> None:
        if self._db.song_count() > 0:
            self._status_left.setText(
                tr("共 {count} 首（本地数据库）", count=self._db.song_count())
            )
        elif self._config.music_folder and Path(self._config.music_folder).is_dir():
            self._status_left.setText("")
        elif self._config.music_folder:
            self._status_left.setText(tr("文件夹不存在"))
        else:
            self._status_left.setText(tr("请设置音乐文件夹"))

    # ===== Folder / Scan =====

    def _update_folder_label(self) -> None:
        folder = self._config.music_folder
        if folder:
            text = str(folder)
            # Simple elide by truncation with tooltip
            self._folder_label.setText(text)
            self._folder_label.setToolTip(text)
        else:
            self._folder_label.setText(tr("未设置文件夹"))
            self._folder_label.setToolTip("")

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("选择音乐文件夹"), self._config.music_folder or "")
        if folder:
            self._config.music_folder = folder
            save_config(self._config)
            self._update_folder_label()
            self._start_scan()

    def _set_scanning(self, scanning: bool) -> None:
        self._btn_choose.setEnabled(not scanning)
        self._btn_rescan.setEnabled(not scanning)

    def _set_progress_busy(self, text: str) -> None:
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setFormat(text)
        self._progress.show()

    def _set_progress_count(self, label: str, done: int, total: int) -> None:
        self._progress.setRange(0, max(total, 1))
        self._progress.setValue(done)
        self._progress.setFormat(f"{label} {done}/{total}")
        self._progress.show()

    def _set_progress_idle(self) -> None:
        self._progress.hide()

    def _start_scan(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        # A rescan rebuilds the songs table; it must not start while the
        # one-shot size backfill is still writing (SQLite lock contention).
        # Defer with a short timer instead of blocking the GUI thread on
        # worker.wait() — the backfill terminates on its own (finite rows),
        # so the timer picks the scan up within ~200 ms of it finishing.
        if self._size_worker is not None and self._size_worker.isRunning():
            if not self._scan_defer_timer.isActive():
                self._scan_defer_timer.start()
            return
        self._begin_scan()

    def _try_begin_scan(self) -> None:
        if self._size_worker is not None and self._size_worker.isRunning():
            return
        self._scan_defer_timer.stop()
        self._begin_scan()

    def _begin_scan(self) -> None:
        folder = self._config.music_folder
        if not folder:
            self._status_left.setText(tr("请先设置音乐文件夹"))
            return
        path = Path(folder)
        if not path.exists() or not path.is_dir():
            self._status_left.setText(tr("文件夹无效"))
            return

        self._set_scanning(True)
        self._set_progress_busy(tr("正在扫描音乐文件夹…"))
        self._scan_worker = ScanWorker(folder)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.start()

    def _on_scan_finished(self, songs: list) -> None:
        self._db.rebuild(songs)
        self._avatar_cache.clear()
        self._refresh_artist_list()
        self._refresh_letter_list()
        self._refresh_song_table()
        self._status_left.setText(tr("共 {count} 首", count=self._db.song_count()))
        self._set_scanning(False)
        self._scan_worker = None
        self._start_avatar_worker()
        self._start_avatar_renderer()

    def _on_scan_error(self, message: str) -> None:
        self._status_left.setText(tr("扫描错误: {message}", message=message))
        self._set_scanning(False)
        self._set_progress_idle()
        self._scan_worker = None

    def _start_size_backfill(self) -> None:
        """Fill in file sizes for rows written before the size column existed.

        One-shot on launch: a no-op when every row already has a size.
        """
        if self._size_worker is not None and self._size_worker.isRunning():
            return
        if self._db.count_missing_sizes() == 0:
            return
        self._size_worker = SizeBackfillWorker(str(self._db.path))
        self._size_worker.done.connect(self._on_size_backfill_done)
        self._size_worker.start()

    def _on_size_backfill_done(self) -> None:
        self._size_worker = None
        self._refresh_song_table()

    # ===== Loudness =====

    def _loudness_status_text(self) -> str:
        stats = self._db.loudness_stats()
        return "\n".join(
            (
                tr(
                    "已测 {ok} / {total}（失败 {failed}）",
                    ok=stats["ok"],
                    total=stats["count"],
                    failed=stats["failed"],
                ),
                tr("目标 {target} LUFS", target=f"{self._config.loudness_target:g}"),
            )
        )

    def _open_loudness_panel(self) -> None:
        # Re-open the existing panel instead of stacking another dialog
        # on top of it.
        if self._loudness_dialog is not None:
            self._loudness_dialog.raise_()
            self._loudness_dialog.activateWindow()
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("响度对齐"))
        layout = QVBoxLayout(dlg)
        layout.setSpacing(8)

        status = QLabel(dlg)
        status.setText(self._loudness_status_text())
        status.setWordWrap(True)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel(tr("目标响度"), dlg))
        target_spin = QDoubleSpinBox(dlg)
        target_spin.setRange(-30, -5)
        target_spin.setSingleStep(0.25)
        target_spin.setValue(self._config.loudness_target)
        target_row.addWidget(target_spin)
        target_row.addStretch()

        start_btn = QPushButton(tr("开始测量"), dlg)
        start_btn.setObjectName("AccentButton")
        stop_btn = QPushButton(tr("停止测量"), dlg)
        stop_btn.setObjectName("ToolButton")
        remeasure_btn = QPushButton(tr("重新测量全部"), dlg)
        remeasure_btn.setObjectName("ToolButton")
        toggle_btn = QPushButton(dlg)
        toggle_btn.setObjectName("ToolButton")

        btn_row = QHBoxLayout()
        for btn in (start_btn, stop_btn, remeasure_btn, toggle_btn):
            btn_row.addWidget(btn)
        btn_row.addStretch()

        layout.addWidget(status)
        layout.addLayout(target_row)
        layout.addLayout(btn_row)

        def _refresh_status() -> None:
            status.setText(self._loudness_status_text())

        def _update_toggle_text() -> None:
            toggle_btn.setText(
                tr("停用") if self._controller.loudness_enabled else tr("启用")
            )

        def _update_buttons() -> None:
            running = self._loudness_worker is not None
            start_btn.setEnabled(not running)
            remeasure_btn.setEnabled(not running)
            stop_btn.setEnabled(running)

        def _on_progress(done: int, total: int) -> None:
            if total > 0:
                self._set_progress_count(tr("正在测量响度"), done, total)
            else:
                self._set_progress_busy(tr("正在测量响度"))

        def _start_worker() -> None:
            if (
                self._loudness_worker is not None
                and self._loudness_worker.isRunning()
            ):
                return
            worker = LoudnessWorker(
                str(self._db.path),
                workers=self._config.loudness_workers,
                target=self._config.loudness_target,
            )
            self._loudness_worker = worker
            # Bind the worker identity into the slots: a stale worker's
            # queued finished/error must not clear a newer worker's
            # reference (that would leave the new one untracked and
            # unstoppable on close).
            worker.progress.connect(_on_progress)
            worker.finished.connect(
                lambda w=worker: self._on_loudness_finished(w)
            )
            worker.error.connect(
                lambda message, w=worker: self._on_loudness_error(w, message)
            )
            worker.start()
            self._set_progress_busy(tr("正在测量响度"))
            _update_buttons()

        def _stop_worker() -> None:
            if self._loudness_worker is not None:
                self._loudness_worker.stop()

        def _remeasure_all() -> None:
            reply = QMessageBox.question(
                self,
                tr("重新测量全部"),
                tr("确定重新测量全部歌曲的响度吗？"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._db.clear_loudness()
                _refresh_status()
                _start_worker()

        def _toggle_enabled() -> None:
            enabled = not self._controller.loudness_enabled
            self._controller.set_loudness_enabled(enabled)
            self._config.loudness_enabled = enabled
            save_config(self._config)
            _update_toggle_text()

        def _target_changed(value: float) -> None:
            self._controller.set_loudness_target(value)
            self._config.loudness_target = float(value)
            save_config(self._config)
            _refresh_status()

        start_btn.clicked.connect(_start_worker)
        stop_btn.clicked.connect(_stop_worker)
        remeasure_btn.clicked.connect(_remeasure_all)
        toggle_btn.clicked.connect(_toggle_enabled)
        target_spin.valueChanged.connect(_target_changed)

        _update_toggle_text()
        _update_buttons()

        self._loudness_refresh_status = _refresh_status
        self._loudness_update_buttons = _update_buttons
        self._loudness_update_toggle_text = _update_toggle_text

        self._loudness_dialog = dlg
        dlg.setAttribute(Qt.WA_DeleteOnClose)

        def _on_dialog_finished(_result: int) -> None:
            self._loudness_dialog = None
            self._loudness_refresh_status = None
            self._loudness_update_buttons = None
            self._loudness_update_toggle_text = None

        dlg.finished.connect(_on_dialog_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_loudness_finished(self, worker) -> None:
        """A loudness run finished; ignore signals from stale workers."""
        if self._loudness_worker is not worker:
            return
        self._loudness_worker = None
        self._set_progress_idle()
        if self._loudness_dialog is not None:
            self._loudness_refresh_status()
            self._loudness_update_buttons()
            self._loudness_update_toggle_text()

    def _on_loudness_error(self, worker, message: str) -> None:
        """A loudness run failed; only the tracked worker resets the UI."""
        self._on_status_message(message)
        if self._loudness_worker is worker and not worker.isRunning():
            self._loudness_worker = None
            self._set_progress_idle()
            if self._loudness_dialog is not None:
                self._loudness_update_buttons()
                self._loudness_update_toggle_text()

    # ===== Mic mixer (混音台) =====

    def _open_mixer_panel(self) -> None:
        """Build (or re-raise) the mic mixer dialog.

        Mirrors the loudness panel: one reusable ``QDialog`` per window;
        the reference is cleared on ``finished`` so the next click builds
        a fresh one.
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

    # ===== Mode / lists =====

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._current_artist = None
        self._current_letter = None
        if mode == "letter":
            self._artist_list.blockSignals(True)
            self._artist_list.clearSelection()
            self._artist_list.blockSignals(False)
            self._artist_list.hide()
            self._letter_list.show()
        else:
            self._letter_list.blockSignals(True)
            self._letter_list.clearSelection()
            self._letter_list.blockSignals(False)
            self._letter_list.hide()
            self._artist_list.show()
        self._refresh_song_table()

    def _avatar_pixmap(self, name: str, eager: bool = False) -> QPixmap:
        """Avatar pixmap for *name*.

        Cached real photos are returned instantly. Without a cache hit,
        *eager* decodes the stored image on the spot; otherwise a
        placeholder is returned (the background renderer replaces it as
        soon as it is ready).
        """
        pixmap = self._avatar_cache.get(name)
        if pixmap is None:
            data = self._db.get_avatar(name) if eager else None
            pixmap = avatar_pixmap(name, data, size=112)
            if data:
                self._avatar_cache[name] = pixmap
        return pixmap

    def _refresh_artist_list(self) -> None:
        self._artist_list.blockSignals(True)
        self._artist_list.clear()
        self._artist_rows.clear()
        all_item = QListWidgetItem(tr("全部 ({count})", count=self._db.song_count()))
        all_item.setData(Qt.UserRole, None)
        all_item.setIcon(QIcon(self._avatar_pixmap("全")))
        self._artist_list.addItem(all_item)
        ordered = sorted(
            self._db.artist_counts(),
            key=lambda p: [s.lower() for s in pinyin_key(p[0])],
        )
        row = 1
        for name, count in ordered:
            item = QListWidgetItem(f"{name} ({count})")
            item.setData(Qt.UserRole, name)
            item.setIcon(QIcon(self._avatar_pixmap(name)))
            self._artist_list.addItem(item)
            self._artist_rows[name] = row
            row += 1
        self._artist_list.blockSignals(False)
        self._artist_list.setCurrentRow(0)

    def _refresh_letter_list(self) -> None:
        self._letter_list.blockSignals(True)
        self._letter_list.clear()
        all_item = QListWidgetItem(tr("全部 ({count})", count=self._db.song_count()))
        all_item.setData(Qt.UserRole, None)
        self._letter_list.addItem(all_item)
        for letter, count in self._db.letters():
            item = QListWidgetItem(f"{letter} ({count})")
            item.setData(Qt.UserRole, letter)
            if count == 0:
                # QListWidgetItem has no setEnabled; clearing ItemIsEnabled
                # also makes the item unselectable
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self._letter_list.addItem(item)
        self._letter_list.blockSignals(False)
        self._letter_list.setCurrentRow(0)

    def _on_artist_selection_changed(self) -> None:
        item = self._artist_list.currentItem()
        if item is None:
            return
        data = item.data(Qt.UserRole)
        self._current_artist = data if isinstance(data, str) else None
        self._refresh_song_table()

    def _on_letter_selection_changed(self) -> None:
        item = self._letter_list.currentItem()
        if item is None:
            return
        data = item.data(Qt.UserRole)
        self._current_letter = data if isinstance(data, str) else None
        self._refresh_song_table()

    # ===== Song table =====

    def _get_displayed_songs(self) -> list[Song]:
        if self._search_text:
            return self._db.search(self._search_text)
        if self._mode == "letter":
            if self._current_letter:
                return self._db.songs_by_letter(self._current_letter)
            return self._db.all_songs()
        if self._current_artist:
            return self._db.songs_by_artist(self._current_artist)
        return self._db.all_songs()

    def _refresh_song_table(self) -> None:
        self._song_model.set_songs(self._get_displayed_songs())
        if self._sort_column is not None:
            self._sort_table()
        self._update_button_states()

    def _on_header_clicked(self, column: int) -> None:
        if column >= self._song_table.model().columnCount() - 1:
            return  # trailing spacer: not a sortable column
        if self._sort_column == column:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_column = column
            self._sort_order = Qt.SortOrder.AscendingOrder
        self._sort_table()

    def _sort_table(self) -> None:
        self._song_model.sort(self._sort_column, self._sort_order)
        header = self._song_table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(self._sort_column, self._sort_order)

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._refresh_song_table()

    def _selected_songs(self) -> list[Song]:
        # Read from the (possibly user-sorted) rows, not the query order.
        rows = sorted(
            {idx.row() for idx in self._song_table.selectionModel().selectedRows()}
        )
        songs = [self._song_model.song_at(row) for row in rows]
        return [s for s in songs if s is not None]

    def _append_selected(self) -> None:
        was_empty = not self._controller.queue
        for song in self._selected_songs():
            self._controller.append(song)
        self._start_if_first_song(was_empty)

    def _insert_selected(self) -> None:
        was_empty = not self._controller.queue
        for song in self._selected_songs():
            self._controller.insert_next(song)
        self._start_if_first_song(was_empty)

    def _play_now_selected(self) -> None:
        songs = self._selected_songs()
        if songs:
            self._controller.play_now(songs[0])

    def _on_song_double_clicked(self) -> None:
        songs = self._selected_songs()
        if songs:
            was_empty = not self._controller.queue
            self._controller.append(songs[0])
            self._start_if_first_song(was_empty)

    def _start_if_first_song(self, was_empty: bool) -> None:
        """An empty queue starts playing the first song that is added."""
        if was_empty and self._controller.queue:
            self._controller.play_at(0)

    # ===== Right-click: permanent delete =====

    def _on_song_context_menu(self, pos: QPoint) -> None:
        index = self._song_table.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        song = self._song_model.song_at(row)
        if song is None or not song.path:
            return
        self._song_table.selectRow(row)
        menu = QMenu(self)
        delete_action = menu.addAction(tr("永久删除"))
        delete_action.setToolTip(tr("从磁盘删除视频文件：{path}", path=song.path))
        if menu.exec(self._song_table.mapToGlobal(pos)) is delete_action:
            self._confirm_delete_song(song)

    def _confirm_delete_song(self, song: Song) -> None:
        reply = QMessageBox.question(
            self,
            tr("永久删除"),
            tr(
                "确定要永久删除这首歌曲吗？\n\n{display}\n\n"
                "将删除视频文件：\n{path}\n\n此操作不可撤销。",
                display=song.display,
                path=song.path,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._delete_song(song)

    def _delete_song(self, song: Song) -> None:
        path = Path(song.path)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                self._status_bar.showMessage(tr("删除文件失败：{error}", error=str(exc)), 5000)
                return
        # Stop playback and drop every queued copy before the file is gone.
        current = self._controller.current_song
        if current is not None and current.path == song.path:
            self._controller.stop()
        queue = self._controller.queue
        for i in range(len(queue) - 1, -1, -1):
            if queue[i].path == song.path:
                self._controller.remove_at(i)
        self._db.delete_song(song.path)
        remove_cached_for(song.path)
        self._refresh_artist_list()
        self._refresh_letter_list()
        self._start_avatar_renderer()
        self._refresh_song_table()
        self._refresh_queue()
        self._update_button_states()
        self._update_status_summary()
        self._status_bar.showMessage(tr("已永久删除《{title}》", title=song.title), 5000)

    # ===== Avatar fetching =====

    def _start_avatar_worker(self) -> None:
        if self._avatar_worker is not None and self._avatar_worker.isRunning():
            return
        names = [n for n in self._db.artists_without_avatar() if n != "未知歌手"]
        if not names:
            self._set_progress_idle()
            return
        self._avatar_worker = AvatarWorker(names)
        self._avatar_worker.fetched.connect(self._on_avatar_fetched)
        self._avatar_worker.progress.connect(self._on_avatar_progress)
        self._avatar_worker.finished_all.connect(self._on_avatar_worker_done)
        self._set_progress_count(tr("正在获取歌手头像"), 0, len(names))
        self._avatar_worker.start()

    def _on_avatar_fetched(self, name: str, data) -> None:
        if data:
            self._db.set_avatar(name, data)
        else:
            self._db.mark_avatar_tried(name)
        self._avatar_cache.pop(name, None)
        self._update_artist_icon(name)

    def _on_avatar_progress(self, done: int, total: int) -> None:
        self._set_progress_count(tr("正在获取歌手头像"), done, total)

    def _update_artist_icon(self, name: str) -> None:
        row = self._artist_rows.get(name)
        if row is None:
            return
        item = self._artist_list.item(row)
        if item is not None:
            item.setIcon(QIcon(self._avatar_pixmap(name, eager=True)))

    def _start_avatar_renderer(self) -> None:
        if self._avatar_renderer is not None and self._avatar_renderer.isRunning():
            return
        names = [name for name, _ in self._db.artist_counts() if name != "未知歌手"]
        if not names:
            return
        self._avatar_renderer = AvatarRenderer(names, self._db.path)
        self._avatar_renderer.rendered.connect(self._on_avatar_rendered)
        self._avatar_renderer.finished_all.connect(self._on_avatar_renderer_done)
        self._avatar_renderer.start()

    def _on_avatar_rendered(self, batch) -> None:
        # (artist, clean bytes) decoded off the GUI thread. Shrunken copies
        # are already persisted by the renderer itself, so this slot only
        # paints — no bulk DB I/O on the GUI thread.
        for name, data in batch:
            if not data:
                continue
            self._avatar_cache.pop(name, None)
            self._update_artist_icon(name)

    def _on_avatar_renderer_done(self) -> None:
        self._avatar_renderer = None

    def _on_avatar_worker_done(self) -> None:
        self._avatar_worker = None
        self._set_progress_idle()
        self._status_left.setText(tr("共 {count} 首歌曲", count=self._db.song_count()))

    # ===== Queue table =====

    def _refresh_queue(self) -> None:
        # Preserve selection by song path (titles are not unique in a
        # queue: covers and live versions share titles).
        old_paths = set()
        for idx in self._queue_table.selectionModel().selectedRows():
            item = self._queue_table.item(idx.row(), 2)
            if item is not None:
                path = item.data(Qt.UserRole)
                if path is not None:
                    old_paths.add(path)

        queue = self._controller.queue
        self._queue_table.setRowCount(len(queue))
        for row, song in enumerate(queue):
            self._set_queue_heart(row, song.path)
            self._queue_table.setItem(row, 1, QTableWidgetItem(song.artist))
            title_item = QTableWidgetItem(
                f"▶ {song.title}" if row == self._controller.current_index else song.title
            )
            title_item.setData(Qt.UserRole, song.path)
            title_item.setData(QUEUE_ROLE_TITLE, song.title)
            self._queue_table.setItem(row, 2, title_item)

        self._highlight_current_queue()

        # Restore selection where possible
        if old_paths:
            for row in range(self._queue_table.rowCount()):
                item = self._queue_table.item(row, 2)
                if item is not None and item.data(Qt.UserRole) in old_paths:
                    self._queue_table.selectRow(row)

    def _set_queue_heart(self, row: int, path: str) -> None:
        heart = QTableWidgetItem()
        fav = self._db.is_favorite(path)
        heart.setText("♥" if fav else "♡")
        heart.setForeground(QColor("#e5484d") if fav else QColor("#8a8f98"))
        heart.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._queue_table.setItem(row, 0, heart)

    def _on_queue_item_clicked(self, item: QTableWidgetItem) -> None:
        """Clicking the heart cell toggles the song's favorite state."""
        if item.column() != 0:
            return
        row = item.row()
        queue = self._controller.queue
        if not 0 <= row < len(queue):
            return
        fav = self._db.toggle_favorite(queue[row].path)
        heart = self._queue_table.item(row, 0)
        if heart is not None:
            heart.setText("♥" if fav else "♡")
            heart.setForeground(QColor("#e5484d") if fav else QColor("#8a8f98"))

    def _highlight_current_queue(self) -> None:
        current = self._controller.current_index
        for row in range(self._queue_table.rowCount()):
            title_item = self._queue_table.item(row, 2)
            if title_item is None:
                continue
            # The raw title lives in item data (QUEUE_ROLE_TITLE); the "▶ "
            # marker is display-only, so a title that itself starts with
            # "▶ " (or a space) is never mangled by re-stripping.
            title = title_item.data(QUEUE_ROLE_TITLE)
            if title is None:
                title = title_item.text().lstrip("▶ ").strip()
            title_item.setText(f"▶ {title}" if row == current else title)

        # Update status bar right
        song = self._controller.current_song
        if song:
            self._status_right.setText(f"▶ {song.display}")
        else:
            self._status_right.setText("")

        self._btn_track.setEnabled(
            self._controller.current_index >= 0 and self._controller.has_multi_audio_track()
        )

    def _on_state_changed(self, state: str) -> None:
        self._btn_play.setText(tr("暂停") if state == "playing" else tr("播放"))

    def _on_audio_track(self, index: int) -> None:
        multi = self._controller.has_multi_audio_track()
        playing = self._controller.current_index >= 0
        self._btn_track.setEnabled(multi and playing)
        self._btn_track.setChecked(index == 1)
        self._btn_track.setToolTip(
            tr("当前音轨：伴奏") if (multi and index) else tr("当前音轨：原唱")
            if multi
            else tr("切换当前歌曲的音轨（原唱/伴奏）")
        )

    def _on_queue_double_clicked(self, index: QModelIndex) -> None:
        # The heart cell is a toggle target, not a play target.
        if index.column() == 0:
            return
        row = index.row()
        if 0 <= row < len(self._controller.queue):
            self._controller.play_at(row)

    def _queue_all_favorites(self) -> None:
        songs = self._db.favorite_songs()
        if not songs:
            self._status_bar.showMessage(tr("没有红心歌曲"), 3000)
            return
        in_queue = {s.path for s in self._controller.queue}
        to_add = [s for s in songs if s.path not in in_queue]
        if not to_add:
            self._status_bar.showMessage(tr("红心歌曲都已在队列中"), 3000)
            return
        was_empty = not self._controller.queue
        for song in to_add:
            self._controller.append(song)
        self._start_if_first_song(was_empty)
        self._status_bar.showMessage(tr("已加入 {count} 首红心歌曲", count=len(to_add)), 3000)

    def _insert_after_current(self) -> None:
        rows = [idx.row() for idx in self._queue_table.selectionModel().selectedRows()]
        self._controller.jump_after_current(rows)

    def _move_up(self) -> None:
        rows = [idx.row() for idx in self._queue_table.selectionModel().selectedRows()]
        self._controller.move_rows(rows, -1)

    def _move_down(self) -> None:
        rows = [idx.row() for idx in self._queue_table.selectionModel().selectedRows()]
        self._controller.move_rows(rows, 1)

    def _remove_selected(self) -> None:
        rows = [idx.row() for idx in self._queue_table.selectionModel().selectedRows()]
        self._controller.remove_rows(rows)

    def _on_status_message(self, message: str) -> None:
        self._status_bar.showMessage(tr(message), 5000)

    # ===== Phone ordering (QR + web server) =====

    def _build_qr_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._qr_label = QLabel(row)
        self._qr_label.setObjectName("QrCode")
        self._qr_label.setFixedSize(100, 100)
        self._qr_label.setStyleSheet(
            "background: #ffffff; border: 1px solid #2a2a3e; border-radius: 6px;"
        )
        self._qr_label.setScaledContents(True)
        layout.addWidget(self._qr_label)

        box = QWidget(row)
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(4)
        self._qr_title = QLabel(tr("手机扫码点歌"), box)
        self._qr_title.setObjectName("QrTitle")
        self._qr_url = QLabel(box)
        self._qr_url.setObjectName("QrUrl")
        self._qr_url.setWordWrap(True)
        vbox.addWidget(self._qr_title)
        vbox.addWidget(self._qr_url)
        vbox.addStretch()
        layout.addWidget(box, stretch=1)
        return row

    def _update_qr(self) -> None:
        self._qr_label.setPixmap(qr_pixmap(self._web.url))
        self._qr_url.setText(self._web.url)

    def _set_qr_unavailable(self, text: str) -> None:
        self._qr_label.clear()
        self._qr_url.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Worker threads are daemons: ask them to stop, but never block the
        # close on an in-flight network call (old behavior: close stalled for
        # minutes behind a slow avatar fetch, and closing mid-fetch could
        # abort process finalization).
        for worker in (
            self._scan_worker,
            self._avatar_worker,
            self._avatar_renderer,
            self._size_worker,
        ):
            if worker is not None:
                worker.stop()
        # The loudness worker spawns ffmpeg children: stop it and wait for
        # the thread to exit so no ffmpeg survives the window close.
        if self._loudness_worker is not None:
            self._loudness_worker.stop()
            self._loudness_worker.wait(5000)
        self._scan_defer_timer.stop()
        # The mic mixer dialog is a child of this window: close it (the
        # finished slot clears the widget refs) and stop any pending
        # debounced save write so no save_config fires after close.
        self._mic_save_timer.stop()
        if self._mixer_dialog is not None:
            self._mixer_dialog.close()
            self._mixer_dialog = None
        self._web.stop()
        off_language_changed(self.retranslate)
        super().closeEvent(event)

    def _update_button_states(self) -> None:
        has_selection = bool(self._song_table.selectionModel().selectedRows())
        self._btn_append.setEnabled(has_selection)
        self._btn_insert.setEnabled(has_selection)
        self._btn_play_now.setEnabled(has_selection)
