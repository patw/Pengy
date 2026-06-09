"""Pengy - LLM Chat Desktop Application."""
import sys


def main():
    """Main entry point for the Pengy desktop GUI."""
    # Friendly import guard so ``pip install pengy`` (without [gui]) gives a
    # clear message instead of an ugly traceback.
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print(
            "❌ Pengy GUI requires PySide6.\n"
            "   Install it with:  pip install pengy[gui]\n"
            "   Or install everything:  pip install pengy[all]\n"
            "   For the CLI-only version:  pip install pengy[cli]",
            file=sys.stderr,
        )
        sys.exit(1)

    import os
    from pathlib import Path
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase, QIcon

    from pengy.core.config import load_config
    from pengy.ui.main_window import MainWindow

    _ICON_PATH = Path(__file__).parent / "assets" / "icon.svg"

    scale = load_config().get("ui_scale", 100)
    if scale != 100:
        os.environ["QT_SCALE_FACTOR"] = str(scale / 100)

    app = QApplication(sys.argv)
    app.setApplicationName("Pengy")
    app.setOrganizationName("Pengy")
    if _ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(_ICON_PATH)))

    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
