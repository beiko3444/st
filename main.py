from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from inventory_app.config import load_config
from inventory_app.ui.main_window import MainWindow


def _apply_light_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(245, 245, 245))
    palette.setColor(QPalette.WindowText, QColor(20, 20, 20))
    palette.setColor(QPalette.Base, QColor(255, 255, 255))
    palette.setColor(QPalette.AlternateBase, QColor(248, 248, 248))
    palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 220))
    palette.setColor(QPalette.ToolTipText, QColor(20, 20, 20))
    palette.setColor(QPalette.Text, QColor(20, 20, 20))
    palette.setColor(QPalette.Button, QColor(240, 240, 240))
    palette.setColor(QPalette.ButtonText, QColor(20, 20, 20))
    palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.Highlight, QColor(30, 115, 190))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)


def main() -> int:
    app = QApplication(sys.argv)
    _apply_light_theme(app)

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, "설정 로드 실패", str(exc))
        return 1

    window = MainWindow(config)
    window.show()
    QTimer.singleShot(0, window.sync_now)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
