from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QRect, QSize
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
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ezkaraoke.avatar import AvatarWorker, strip_png_iccp
from ezkaraoke.config import Config, save_config
from ezkaraoke.database import SongDatabase
from ezkaraoke.letters import pinyin_key
from ezkaraoke.library import Song
from ezkaraoke.player import PlayerController
from ezkaraoke.pitchshift import remove_cached_for
from ezkaraoke.scanner import ScanWorker


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


class _SongItem(QTableWidgetItem):
    """Song table cell with a pinyin-aware sort key (Chinese order)."""

    def __init__(self, text: str, key: list[str]) -> None:
        super().__init__(text)
        self._sort_key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, _SongItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)


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
        if data[:4] == b"\x89PNG":
            data = strip_png_iccp(data)  # malformed iCCP triggers qt.gui.icc warnings
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
        self._scan_worker: ScanWorker | None = None
        self._avatar_worker: AvatarWorker | None = None
        self._mode: str = "artist"
        self._current_artist: str | None = None
        self._current_letter: str | None = None
        self._search_text: str = ""
        self._avatar_cache: dict[str, QPixmap] = {}

        self.setWindowTitle("ezkaraoke · 点歌台")
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

        self._btn_mode_artist = QPushButton("歌手", self)
        self._btn_mode_artist.setObjectName("ModeButton")
        self._btn_mode_artist.setCheckable(True)
        self._btn_mode_artist.setChecked(True)
        self._btn_mode_artist.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._btn_mode_artist.clicked.connect(lambda: self._set_mode("artist"))
        mode_layout.addWidget(self._btn_mode_artist)

        self._btn_mode_letter = QPushButton("首字母", self)
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

        self._btn_choose = QPushButton("选择文件夹…", self)
        self._btn_choose.setObjectName("ToolButton")
        self._btn_choose.clicked.connect(self._choose_folder)
        toolbar_layout.addWidget(self._btn_choose)

        self._btn_rescan = QPushButton("重新扫描", self)
        self._btn_rescan.setObjectName("ToolButton")
        self._btn_rescan.clicked.connect(self._start_scan)
        toolbar_layout.addWidget(self._btn_rescan)

        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText("搜索歌手或歌名…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumWidth(200)
        self._search_edit.textChanged.connect(self._on_search_changed)
        toolbar_layout.addWidget(self._search_edit)

        center_layout.addWidget(toolbar)

        # Song table (click 歌手/歌名 headers to sort; pinyin order for CJK).
        # #SongTable gets the large display font via theme.qss.
        self._song_table = QTableWidget(self)
        self._song_table.setObjectName("SongTable")
        self._song_table.setColumnCount(2)
        self._song_table.setHorizontalHeaderLabels(["歌手", "歌名"])
        self._song_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self._song_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._song_table.setColumnWidth(0, 260)
        # Comfortable breathing room around the 28px display font
        self._song_table.verticalHeader().setDefaultSectionSize(52)
        # Manual pinyin-aware sorting: Qt's built-in sort compares raw text
        # (and PySide ignores QTableWidgetItem.__lt__), so we sort in Python
        # by pinyin key and re-fill the table.
        self._sort_column: int | None = None
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._song_table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._song_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._song_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._song_table.setAlternatingRowColors(True)
        self._song_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._song_table.doubleClicked.connect(self._on_song_double_clicked)
        # Re-enable the action buttons as soon as the user (de)selects rows;
        # without this they stay disabled until the next table refresh.
        self._song_table.itemSelectionChanged.connect(self._update_button_states)
        self._song_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._song_table.customContextMenuRequested.connect(self._on_song_context_menu)
        center_layout.addWidget(self._song_table, stretch=1)

        # Action buttons
        action_bar = QWidget(self)
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(10)

        self._btn_append = QPushButton("点歌", self)
        self._btn_append.setObjectName("AccentButton")
        self._btn_append.clicked.connect(self._append_selected)
        action_layout.addWidget(self._btn_append)

        self._btn_insert = QPushButton("插入播放", self)
        self._btn_insert.setObjectName("ToolButton")
        self._btn_insert.clicked.connect(self._insert_selected)
        action_layout.addWidget(self._btn_insert)

        self._btn_play_now = QPushButton("立即播放", self)
        self._btn_play_now.setObjectName("ToolButton")
        self._btn_play_now.clicked.connect(self._play_now_selected)
        action_layout.addWidget(self._btn_play_now)

        self._btn_play = QPushButton("播放", self)
        self._btn_play.setObjectName("ToolButton")
        self._btn_play.clicked.connect(self._controller.toggle_pause)
        action_layout.addWidget(self._btn_play)

        self._btn_track = QPushButton("原唱/伴奏", self)
        self._btn_track.setObjectName("ToolButton")
        self._btn_track.setCheckable(True)
        self._btn_track.setEnabled(False)
        self._btn_track.setToolTip("切换当前歌曲的音轨（原唱/伴奏）")
        self._btn_track.clicked.connect(self._controller.toggle_audio_track)
        action_layout.addWidget(self._btn_track)

        action_layout.addStretch()
        center_layout.addWidget(action_bar)

        splitter.addWidget(center_widget)

        # ===== Right: Play queue =====
        right_widget = QWidget(self)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(4, 8, 8, 8)
        right_layout.setSpacing(10)

        self._queue_table = QTableWidget(self)
        self._queue_table.setColumnCount(3)
        self._queue_table.setHorizontalHeaderLabels(["序号", "歌手", "歌名"])
        self._queue_table.horizontalHeader().setStretchLastSection(True)
        self._queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._queue_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._queue_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._queue_table.setAlternatingRowColors(True)
        self._queue_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._queue_table.doubleClicked.connect(self._on_queue_double_clicked)
        right_layout.addWidget(self._queue_table, stretch=1)

        queue_bar = QWidget(self)
        queue_bar_layout = QHBoxLayout(queue_bar)
        queue_bar_layout.setContentsMargins(0, 0, 0, 0)
        queue_bar_layout.setSpacing(8)

        self._btn_up = QPushButton("上移", self)
        self._btn_up.setObjectName("ToolButton")
        self._btn_up.clicked.connect(self._move_up)
        queue_bar_layout.addWidget(self._btn_up)

        self._btn_down = QPushButton("下移", self)
        self._btn_down.setObjectName("ToolButton")
        self._btn_down.clicked.connect(self._move_down)
        queue_bar_layout.addWidget(self._btn_down)

        self._btn_remove = QPushButton("删除", self)
        self._btn_remove.setObjectName("ToolButton")
        self._btn_remove.clicked.connect(self._remove_selected)
        queue_bar_layout.addWidget(self._btn_remove)

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
            self._status_left.setText(f"共 {self._db.song_count()} 首（本地数据库）")
            self._start_avatar_worker()
        elif self._config.music_folder:
            folder = Path(self._config.music_folder)
            if folder.exists() and folder.is_dir():
                self._start_scan()
            else:
                self._status_left.setText("文件夹不存在")
        else:
            self._status_left.setText("请设置音乐文件夹")

    # ===== Folder / Scan =====

    def _update_folder_label(self) -> None:
        folder = self._config.music_folder
        if folder:
            text = str(folder)
            # Simple elide by truncation with tooltip
            self._folder_label.setText(text)
            self._folder_label.setToolTip(text)
        else:
            self._folder_label.setText("未设置文件夹")
            self._folder_label.setToolTip("")

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择音乐文件夹", self._config.music_folder or "")
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
        folder = self._config.music_folder
        if not folder:
            self._status_left.setText("请先设置音乐文件夹")
            return
        path = Path(folder)
        if not path.exists() or not path.is_dir():
            self._status_left.setText("文件夹无效")
            return

        self._set_scanning(True)
        self._set_progress_busy("正在扫描音乐文件夹…")
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
        self._status_left.setText(f"共 {self._db.song_count()} 首")
        self._set_scanning(False)
        self._scan_worker = None
        self._start_avatar_worker()

    def _on_scan_error(self, message: str) -> None:
        self._status_left.setText(f"扫描错误: {message}")
        self._set_scanning(False)
        self._set_progress_idle()
        self._scan_worker = None

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

    def _avatar_pixmap(self, name: str) -> QPixmap:
        """Cached avatar pixmap (decoded once per avatar, not per refresh)."""
        pixmap = self._avatar_cache.get(name)
        if pixmap is None:
            pixmap = avatar_pixmap(name, self._db.get_avatar(name), size=112)
            self._avatar_cache[name] = pixmap
        return pixmap

    def _refresh_artist_list(self) -> None:
        self._artist_list.blockSignals(True)
        self._artist_list.clear()
        all_item = QListWidgetItem(f"全部 ({self._db.song_count()})")
        all_item.setData(Qt.UserRole, None)
        all_item.setIcon(QIcon(self._avatar_pixmap("全")))
        self._artist_list.addItem(all_item)
        ordered = sorted(
            self._db.artist_counts(),
            key=lambda p: [s.lower() for s in pinyin_key(p[0])],
        )
        for name, count in ordered:
            item = QListWidgetItem(f"{name} ({count})")
            item.setData(Qt.UserRole, name)
            item.setIcon(QIcon(self._avatar_pixmap(name)))
            self._artist_list.addItem(item)
        self._artist_list.blockSignals(False)
        self._artist_list.setCurrentRow(0)

    def _refresh_letter_list(self) -> None:
        self._letter_list.blockSignals(True)
        self._letter_list.clear()
        all_item = QListWidgetItem(f"全部 ({self._db.song_count()})")
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
        songs = self._get_displayed_songs()
        table = self._song_table
        table.setRowCount(len(songs))
        for row, song in enumerate(songs):
            artist_item = _SongItem(song.artist, pinyin_key(song.artist) + [song.artist])
            artist_item.setData(Qt.UserRole, song.path)
            table.setItem(row, 0, artist_item)
            table.setItem(row, 1, _SongItem(song.title, pinyin_key(song.title) + [song.title]))
        if self._sort_column is not None:
            self._sort_table()
        self._update_button_states()

    def _on_header_clicked(self, column: int) -> None:
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
        table = self._song_table
        rows = []
        for r in range(table.rowCount()):
            artist_item = table.item(r, 0)
            title_item = table.item(r, 1)
            if artist_item is None or title_item is None:
                continue
            rows.append(
                (
                    pinyin_key(artist_item.text()),
                    pinyin_key(title_item.text()),
                    artist_item.text(),
                    title_item.text(),
                    artist_item.data(Qt.UserRole),
                )
            )
        primary, secondary = (0, 1) if self._sort_column == 0 else (1, 0)
        rows.sort(key=lambda r: (r[primary], r[secondary]), reverse=self._sort_order == Qt.SortOrder.DescendingOrder)
        table.setRowCount(0)
        table.setRowCount(len(rows))
        for r, (akey, tkey, artist, title, path) in enumerate(rows):
            artist_item = _SongItem(artist, akey + [artist])
            artist_item.setData(Qt.UserRole, path)
            table.setItem(r, 0, artist_item)
            table.setItem(r, 1, _SongItem(title, tkey + [title]))
        header = table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(self._sort_column, self._sort_order)

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._refresh_song_table()

    def _selected_songs(self) -> list[Song]:
        # Read from the (possibly user-sorted) rows, not the query order.
        songs = []
        rows = sorted(set(idx.row() for idx in self._song_table.selectionModel().selectedRows()))
        for row in rows:
            artist_item = self._song_table.item(row, 0)
            title_item = self._song_table.item(row, 1)
            if artist_item is None or title_item is None:
                continue
            songs.append(
                Song(artist_item.text(), title_item.text(), artist_item.data(Qt.UserRole))
            )
        return songs

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
        row = self._song_table.rowAt(pos.y())
        if row < 0:
            return
        artist_item = self._song_table.item(row, 0)
        title_item = self._song_table.item(row, 1)
        path = artist_item.data(Qt.UserRole) if artist_item is not None else None
        if artist_item is None or title_item is None or not path:
            return
        self._song_table.selectRow(row)
        song = Song(artist_item.text(), title_item.text(), path)
        menu = QMenu(self)
        delete_action = menu.addAction("永久删除")
        delete_action.setToolTip(f"从磁盘删除视频文件：{song.path}")
        if menu.exec(self._song_table.mapToGlobal(pos)) is delete_action:
            self._confirm_delete_song(song)

    def _confirm_delete_song(self, song: Song) -> None:
        reply = QMessageBox.question(
            self,
            "永久删除",
            f"确定要永久删除这首歌曲吗？\n\n{song.display}\n\n"
            f"将删除视频文件：\n{song.path}\n\n此操作不可撤销。",
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
                self._status_bar.showMessage(f"删除文件失败：{exc}", 5000)
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
        self._refresh_song_table()
        self._refresh_queue()
        self._update_button_states()
        self._status_left.setText(f"共 {self._db.song_count()} 首（本地数据库）")
        self._status_bar.showMessage(f"已永久删除《{song.title}》", 5000)

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
        self._set_progress_count("正在获取歌手头像", 0, len(names))
        self._avatar_worker.start()

    def _on_avatar_fetched(self, name: str, data) -> None:
        if data:
            self._db.set_avatar(name, data)
        else:
            self._db.mark_avatar_tried(name)
        self._avatar_cache.pop(name, None)
        self._update_artist_icon(name)

    def _on_avatar_progress(self, done: int, total: int) -> None:
        self._set_progress_count("正在获取歌手头像", done, total)

    def _update_artist_icon(self, name: str) -> None:
        for i in range(self._artist_list.count()):
            item = self._artist_list.item(i)
            if item.data(Qt.UserRole) == name:
                item.setIcon(QIcon(self._avatar_pixmap(name)))
                return

    def _on_avatar_worker_done(self) -> None:
        self._avatar_worker = None
        self._set_progress_idle()
        self._status_left.setText(f"共 {self._db.song_count()} 首歌曲")

    # ===== Queue table =====

    def _refresh_queue(self) -> None:
        # Preserve selection by song path
        old_selection = set()
        for idx in self._queue_table.selectionModel().selectedRows():
            item = self._queue_table.item(idx.row(), 2)
            if item:
                old_selection.add(item.text())

        queue = self._controller.queue
        self._queue_table.setRowCount(len(queue))
        for row, song in enumerate(queue):
            prefix = ""
            if row == self._controller.current_index:
                prefix = "▶ "
            num_item = QTableWidgetItem(f"{prefix}{row + 1}")
            self._queue_table.setItem(row, 0, num_item)
            self._queue_table.setItem(row, 1, QTableWidgetItem(song.artist))
            self._queue_table.setItem(row, 2, QTableWidgetItem(song.title))

        self._highlight_current_queue()

        # Restore selection where possible
        if old_selection:
            for row in range(self._queue_table.rowCount()):
                item = self._queue_table.item(row, 2)
                if item and item.text() in old_selection:
                    self._queue_table.selectRow(row)

    def _highlight_current_queue(self) -> None:
        current = self._controller.current_index
        for row in range(self._queue_table.rowCount()):
            num_item = self._queue_table.item(row, 0)
            if num_item is None:
                continue
            text = num_item.text().lstrip("▶ ").strip()
            if row == current:
                num_item.setText(f"▶ {text}")
            else:
                num_item.setText(text)

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
        self._btn_play.setText("暂停" if state == "playing" else "播放")

    def _on_audio_track(self, index: int) -> None:
        multi = self._controller.has_multi_audio_track()
        playing = self._controller.current_index >= 0
        self._btn_track.setEnabled(multi and playing)
        self._btn_track.setChecked(index == 1)
        self._btn_track.setToolTip(
            f"当前音轨：{'伴奏' if index else '原唱'}" if multi
            else "切换当前歌曲的音轨（原唱/伴奏）"
        )

    def _on_queue_double_clicked(self) -> None:
        row = self._queue_table.currentRow()
        if 0 <= row < len(self._controller.queue):
            self._controller.play_at(row)

    def _move_up(self) -> None:
        rows = sorted(set(idx.row() for idx in self._queue_table.selectionModel().selectedRows()))
        # Cumulative delta approach: move each selected row up by 1
        # Process top-to-bottom, but since moving up shifts indices, we need to account
        # Actually move top-to-bottom; if consecutive, moving the top one first is fine.
        for row in rows:
            if row > 0:
                self._controller.move(row, -1)

    def _move_down(self) -> None:
        rows = sorted(set(idx.row() for idx in self._queue_table.selectionModel().selectedRows()), reverse=True)
        for row in rows:
            if row < len(self._controller.queue) - 1:
                self._controller.move(row, 1)

    def _remove_selected(self) -> None:
        rows = sorted(set(idx.row() for idx in self._queue_table.selectionModel().selectedRows()), reverse=True)
        for row in rows:
            self._controller.remove_at(row)

    def _on_status_message(self, message: str) -> None:
        self._status_bar.showMessage(message, 5000)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Worker threads are daemons: ask them to stop, but never block the
        # close on an in-flight network call (old behavior: close stalled for
        # minutes behind a slow avatar fetch, and closing mid-fetch could
        # abort process finalization).
        for worker in (self._scan_worker, self._avatar_worker):
            if worker is not None:
                worker.stop()
        super().closeEvent(event)

    def _update_button_states(self) -> None:
        has_selection = bool(self._song_table.selectionModel().selectedRows())
        self._btn_append.setEnabled(has_selection)
        self._btn_insert.setEnabled(has_selection)
        self._btn_play_now.setEnabled(has_selection)
