"""Pengy - LLM Chat Desktop Application."""
import os
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QIcon

from pengy.core.config import load_config
from pengy.ui.main_window import MainWindow

_ICON_PATH = Path(__file__).parent / "assets" / "icon.svg"


def main():
    """Main entry point."""
    scale = load_config().get("ui_scale", 100)
    if scale != 100:
        os.environ["QT_SCALE_FACTOR"] = str(scale / 100)

    app = QApplication(sys.argv)
    app.setApplicationName("Pengy")
    app.setOrganizationName("Pengy")
    if _ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(_ICON_PATH)))

    # Set default font
    font = QFont("Sans Serif", 10)
    app.setFont(font)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
