"""Pengy - LLM Chat Desktop Application."""
import sys


def _get_version() -> str:
    """Return the Pengy version string (actual build version)."""
    try:
        from importlib.metadata import version as _v
        return _v("pengy")
    except Exception:
        from pengy import __version__
        return __version__


def _show_help(exit_code: int = 0):
    """Print usage information for the desktop GUI."""
    print(f"Pengy v{_get_version()} — Local-first AI agent with tools (GUI)")
    print()
    print("Usage: pengy [OPTIONS]")
    print()
    print("Options:")
    print("  -h, --help     Show this help message and exit.")
    print("  -v, --version  Show version information and exit.")
    print("  --config-dir PATH  Use a custom config directory.")
    print()
    print("The desktop GUI launches a PySide6 window. No additional")
    print("command-line options are supported.")
    sys.exit(exit_code)


def main():
    """Main entry point for the Pengy desktop GUI."""
    # Handle flags before doing anything else
    for arg in sys.argv[1:]:
        if arg in ("-v", "--version"):
            print(f"Pengy v{_get_version()}")
            sys.exit(0)
        if arg in ("-h", "--help"):
            _show_help(0)

    # Check for --config-dir
    config_dir = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config-dir":
            # The value is the *next* argv entry, so it must exist.
            if i + 1 >= len(sys.argv):
                print("error: option '--config-dir' requires a value",
                      file=sys.stderr)
                sys.exit(2)
            config_dir = sys.argv[i + 1]
            # Remove the flag and its value from sys.argv so QApplication doesn't choke
            sys.argv.remove("--config-dir")
            sys.argv.remove(config_dir)
            break

    if config_dir:
        from pengy.core.config import set_config_dir
        set_config_dir(config_dir)

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

    from pathlib import Path
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon

    from pengy.core.config import load_config
    from pengy.ui.main_window import MainWindow
    from pengy.ui.theme import get_theme, qt_app_stylesheet, ui_font

    _ICON_PATH = Path(__file__).parent / "assets" / "icon.png"

    config = load_config()

    # Qt/OS own physical display and per-monitor DPI scaling. Pengy's UI scale
    # is an independent preference applied explicitly to fonts and custom
    # metrics, rather than injected into Qt's platform scaling pipeline.
    app = QApplication(sys.argv)
    app.setApplicationName("Pengy")
    app.setOrganizationName("Pengy")
    if _ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(_ICON_PATH)))

    app.setFont(ui_font(config))
    app.setStyleSheet(qt_app_stylesheet(get_theme(config)))

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
