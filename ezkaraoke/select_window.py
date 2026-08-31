from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ezkaraoke.config import Config, save_config
from ezkaraoke.library import Library, Song
from ezkaraoke.player import PlayerController
from ezkaraoke.scanner import ScanWorker


class SelectWindow(QMainWindow):
    def __init__(
        self,
        controller: PlayerController,
        library: Library,
        config: Config,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._library = library
        self._config = config
        self._scan_worker: ScanWorker | None = None
        self._current_artist: str | None = None
        self._search_text: str = ""

        self.setWindowTitle("ezkaraoke · 点歌台")
        self.resize(1280, 760)

        # Main splitter: left tree | center | right queue
        splitter = QSplitter(Qt.Horizontal, self)
        self.setCentralWidget(splitter)

        # ===== Left: Artist tree =====
        left_widget = QWidget(self)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(8)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(1)
        self._tree.setFixedWidth(240)
        self._tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        left_layout.addWidget(self._tree)

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

        btn_choose = QPushButton("选择文件夹…", self)
        btn_choose.setObjectName("ToolButton")
        btn_choose.clicked.connect(self._choose_folder)
        toolbar_layout.addWidget(btn_choose)

        btn_rescan = QPushButton("重新扫描", self)
        btn_rescan.setObjectName("ToolButton")
        btn_rescan.clicked.connect(self._start_scan)
        toolbar_layout.addWidget(btn_rescan)

        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText("搜索歌手或歌名…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumWidth(200)
        self._search_edit.textChanged.connect(self._on_search_changed)
        toolbar_layout.addWidget(self._search_edit)

        center_layout.addWidget(toolbar)

        # Song table
        self._song_table = QTableWidget(self)
        self._song_table.setColumnCount(3)
        self._song_table.setHorizontalHeaderLabels(["歌手", "歌名", "文件名"])
        self._song_table.horizontalHeader().setStretchLastSection(True)
        self._song_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._song_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._song_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._song_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._song_table.setAlternatingRowColors(True)
        self._song_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._song_table.doubleClicked.connect(self._on_song_double_clicked)
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
        splitter.setSizes([240, 700, 340])

        # Status bar
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_left = QLabel(self)
        self._status_right = QLabel(self)
        self._status_bar.addWidget(self._status_left)
        self._status_bar.addPermanentWidget(self._status_right)

        # Connect controller signals
        self._controller.queue_changed.connect(self._refresh_queue)
        self._controller.current_changed.connect(self._highlight_current_queue)
        self._controller.status_message.connect(self._on_status_message)
        self._controller.audio_track_changed.connect(self._on_audio_track)

        # Initial refresh
        self._refresh_tree()
        self._refresh_song_table()
        self._refresh_queue()
        self._update_button_states()

        # Auto-scan on init if folder set
        if self._config.music_folder:
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

        self._status_left.setText("正在扫描…")
        self._scan_worker = ScanWorker(folder, self)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.start()

    def _on_scan_finished(self, songs: list) -> None:
        self._library.load(songs)
        self._refresh_tree()
        self._refresh_song_table()
        self._status_left.setText(f"共 {len(self._library)} 首歌曲")
        self._scan_worker = None

    def _on_scan_error(self, message: str) -> None:
        self._status_left.setText(f"扫描错误: {message}")
        self._scan_worker = None

    # ===== Tree =====

    def _refresh_tree(self) -> None:
        self._tree.clear()
        all_item = QTreeWidgetItem(self._tree, [f"全部歌曲 ({len(self._library)})"])
        all_item.setData(0, Qt.UserRole, None)
        for artist in self._library.artists:
            count = len(self._library.songs_by_artist(artist))
            child = QTreeWidgetItem(all_item, [f"{artist} ({count})"])
            child.setData(0, Qt.UserRole, artist)
        self._tree.expandItem(all_item)
        self._tree.setCurrentItem(all_item)

    def _on_tree_selection_changed(self) -> None:
        item = self._tree.currentItem()
        if item is None:
            return
        artist = item.data(0, Qt.UserRole)
        self._current_artist = artist if isinstance(artist, str) else None
        self._refresh_song_table()

    def _on_tree_double_clicked(self) -> None:
        item = self._tree.currentItem()
        if item is None:
            return
        artist = item.data(0, Qt.UserRole)
        if isinstance(artist, str):
            self._current_artist = artist
            self._refresh_song_table()

    # ===== Song table =====

    def _get_displayed_songs(self) -> list[Song]:
        if self._search_text:
            return self._library.search(self._search_text)
        if self._current_artist is not None:
            return self._library.songs_by_artist(self._current_artist)
        return list(self._library.songs)

    def _refresh_song_table(self) -> None:
        songs = self._get_displayed_songs()
        self._song_table.setRowCount(len(songs))
        for row, song in enumerate(songs):
            self._song_table.setItem(row, 0, QTableWidgetItem(song.artist))
            self._song_table.setItem(row, 1, QTableWidgetItem(song.title))
            fname = Path(song.path).name
            item = QTableWidgetItem(fname)
            item.setToolTip(song.path)
            self._song_table.setItem(row, 2, item)
        self._update_button_states()

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._refresh_song_table()

    def _selected_songs(self) -> list[Song]:
        songs = self._get_displayed_songs()
        rows = sorted(set(idx.row() for idx in self._song_table.selectionModel().selectedRows()))
        return [songs[r] for r in rows if 0 <= r < len(songs)]

    def _append_selected(self) -> None:
        for song in self._selected_songs():
            self._controller.append(song)

    def _insert_selected(self) -> None:
        for song in self._selected_songs():
            self._controller.insert_next(song)

    def _play_now_selected(self) -> None:
        songs = self._selected_songs()
        if songs:
            self._controller.play_now(songs[0])

    def _on_song_double_clicked(self) -> None:
        songs = self._selected_songs()
        if songs:
            self._controller.append(songs[0])

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

    def _update_button_states(self) -> None:
        has_selection = bool(self._song_table.selectionModel().selectedRows())
        self._btn_append.setEnabled(has_selection)
        self._btn_insert.setEnabled(has_selection)
        self._btn_play_now.setEnabled(has_selection)
