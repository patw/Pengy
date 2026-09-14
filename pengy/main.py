"""Pengy - LLM Chat Desktop Application."""
import sys


def _get_version() -> str:
    """Return the Pengy version string."""
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
    print("  --install-launcher    Add Pengy to your application menu (Linux,")
    print("                        user-level only — nothing system-wide is touched).")
    print("  --install-launcher --force  Replace an existing entry not written by Pengy")
    print("                        (e.g. a launcher for a native AppImage build).")
    print("  --uninstall-launcher  Remove that menu entry again.")
    print()
    print("The desktop GUI launches a PySide6 window (pip install \"pengy[gui]\").")
    print()
    print("Without it, use the CLI and Web UI, which the default install includes:")
    print("  pengy-cli    chat in the terminal")
    print("  pengy-web    browser UI (http://127.0.0.1:5000)")
    print()
    print("Native desktop builds, no Python required:")
    print("  https://github.com/patw/PengyR/releases")
    print()
    print("Windows: point shortcuts at `pengy-gui`, not `pengy` — it uses a")
    print("console-less launcher, so no black window appears behind the GUI.")
    sys.exit(exit_code)


def _handle_launcher(flag: str) -> None:
    """Handle --install-launcher / --uninstall-launcher, then exit.

    Explicit invocation only: Pengy never writes outside its own config
    directory unless the user asks for it.
    """
    from pengy.core.launcher import LauncherError, install_launcher, uninstall_launcher

    if flag == "--install-launcher":
        from pengy.core.nudge import compact_desktop_hint, gui_available

        if not gui_available():
            # A menu entry that cannot open a window is worse than no entry.
            print(
                "❌ Install the desktop GUI first — a launcher entry would point at\n"
                "   a program that cannot open a window yet.",
                file=sys.stderr,
            )
            print(compact_desktop_hint(), file=sys.stderr)
            sys.exit(1)
        force = "--force" in sys.argv[1:]
        try:
            print(install_launcher(force=force))
        except LauncherError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    try:
        print(uninstall_launcher())
    except LauncherError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def main():
    """Main entry point for the Pengy desktop GUI."""
    # Handle flags before doing anything else
    for arg in sys.argv[1:]:
        if arg in ("-v", "--version"):
            print(f"Pengy v{_get_version()}")
            sys.exit(0)
        if arg in ("-h", "--help"):
            _show_help(0)

    # Launcher integration — explicit flags only, handled *before* the Qt guard
    # so `pengy --uninstall-launcher` still works if the GUI was uninstalled.
    for flag in ("--install-launcher", "--uninstall-launcher"):
        if flag in sys.argv[1:]:
            _handle_launcher(flag)

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

    # ``pengy`` is the *desktop* entry point, so a missing Qt is never silently
    # swallowed: say what is wrong, then show the two ways to get a window
    # (add the [gui] extra, or use a native build with no Python at all).
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print(
            "❌ Pengy Desktop needs PySide6 (the Qt GUI), and it is not\n"
            "   installed in this environment.",
            file=sys.stderr,
        )
        from pengy.core.nudge import show_always

        show_always()

        # A Windows shortcut can launch this through pythonw, where there is no
        # console at all and printing is invisible — fall back to a real dialog
        # so a double-clicked icon explains itself instead of doing nothing.
        from pengy.core.nudge import notify_without_console

        notify_without_console(
            "Pengy Desktop needs PySide6 (the Qt GUI).\n\n"
            'Add it to this install:  pip install "pengy[gui]"\n'
            "Native builds (no Python): https://github.com/patw/PengyR/releases"
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
