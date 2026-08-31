import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ezkaraoke.config import load_config
from ezkaraoke.database import DEFAULT_DB_PATH, SongDatabase
from ezkaraoke.player import PlayerController
from ezkaraoke.player_window import PlayerWindow
from ezkaraoke.select_window import SelectWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ezkaraoke")

    qss_path = Path(__file__).parent / "theme.qss"
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))

    config = load_config()
    db = SongDatabase(Path(config.db_path) if config.db_path else DEFAULT_DB_PATH)
    controller = PlayerController()

    player_win = PlayerWindow(controller)
    select_win = SelectWindow(controller, db, config)

    player_win.show()
    select_win.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
