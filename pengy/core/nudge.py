"""First-run notice pointing at the desktop options.

**Why this exists at all:** pip has no way to print a message at install time.
A wheel install (what everyone gets from PyPI) runs *none* of our code, and for
a source install pip captures the build backend's output and shows it only
under ``pip install -v`` — verified 2026-09-13: an unconditional banner printed
from the build backend never appears in a normal ``pip install``. ``uv tool
install`` has no message hook either, and no post-install-notice field exists in
the wheel/PEP 621 metadata.

So the notice fires on **first run** instead, which is the first moment the user
is actually reading a terminal:

* :func:`show_once` — used by ``pengy-cli`` and ``pengy-web``. Prints at most
  once per machine (marker file in the config directory) and **only when stderr
  is a TTY**, so cron jobs, systemd journals and piped output stay clean.
* :func:`show_always` — used by ``pengy`` (the desktop entry point) when
  PySide6 is missing, because that user is actively looking for a window. That
  is the one case where the notice must not be silently skipped.

Everything goes to **stderr** so that stdout stays machine-readable
(``pengy-cli --output json``, ``pengy-cli --version`` in CI, etc.).

Silence it permanently with ``PENGY_NO_NUDGE=1``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The native editions (Rust PengyR / C++ PengyCPP) ship the desktop app with no
# Python at all — the right pointer for anyone who does not want the Qt wheel.
RELEASES_URL = "https://github.com/patw/PengyR/releases"

MARKER_NAME = ".desktop-notice-shown"

_RULE = "─" * 72


def _no_nudge() -> bool:
    """True when the user asked us to stay quiet."""
    return bool(os.environ.get("PENGY_NO_NUDGE"))


def gui_available() -> bool:
    """True when PySide6 (the Qt GUI) can be imported."""
    try:
        import PySide6  # noqa: F401

        return True
    except Exception:
        # ImportError normally — but a broken Qt install (missing libGL, a
        # half-removed wheel) should not turn a nudge check into a crash.
        return False


def _marker_path() -> Path:
    from pengy.core.config import get_config_dir

    return get_config_dir() / MARKER_NAME


def build_notice() -> str:
    """The full first-run notice (CLI/Web ready + the two desktop paths)."""
    from pengy import __version__

    return "\n".join(
        [
            "",
            _RULE,
            f"🐧  Pengy v{__version__} is ready — the CLI and Web UI are installed.",
            "",
            "    pengy-cli              chat in this terminal",
            "    pengy-web              browser UI  (http://127.0.0.1:5000)",
            "",
            "    Want a native desktop window?  Two ways:",
            "",
            "    1. Add the Qt GUI to this install   (~80 MB download)",
            '         pip install "pengy[gui]"',
            '         uv tool install --force "pengy[gui]"',
            "       then, on Linux, add a menu entry:",
            "         pengy --install-launcher",
            "",
            "    2. Or use a native build — no Python needed at all",
            f"         {RELEASES_URL}",
            "         PengyR-x86_64.AppImage · pengy_x.y.z_amd64.deb",
            "         Pengy-macOS-arm64.dmg  · PengyR-Windows-x.y.z.zip",
            "",
            "    (hide this notice:  export PENGY_NO_NUDGE=1)",
            _RULE,
            "",
        ]
    )


def build_compact_notice() -> str:
    """Just the actionable lines — used when the full notice is suppressed."""
    return compact_desktop_hint()


def compact_desktop_hint() -> str:
    """The two desktop paths — shared with the launcher command and its errors."""
    return "\n".join(
        [
            '   Desktop GUI for this install:  pip install "pengy[gui]"',
            f"   Native builds (no Python):     {RELEASES_URL}",
        ]
    )


def notify_without_console(message: str, title: str = "Pengy") -> bool:
    """Show a native dialog when there is no console to print to.

    Windows shortcuts created against the ``pengy-gui`` (gui_scripts) entry point
    run under ``pythonw``, where ``sys.stdout``/``sys.stderr`` are ``None`` and
    any printed explanation is invisible.  A double-clicked icon that silently
    does nothing is the worst possible experience, so show a real dialog.

    Returns True when a dialog was attempted; a no-op (False) everywhere else.
    """
    if os.name != "nt":  # pragma: no cover - exercised on Windows only
        return False
    if getattr(sys, "stdout", None) is not None:
        # Launched from a console: printing is enough, do not add a modal dialog.
        return False
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
        return True
    except Exception:
        return False  # never let an error report become the error


def show_once(stream=None) -> None:
    """Show the notice once per machine, interactively only.

    ``stream`` is injectable for tests; it defaults to :data:`sys.stderr`
    (never stdout, so ``--output json`` and CI greps stay clean).

    Deliberately conservative: if the stream is not a TTY (cron, systemd, a
    pipe) nothing is printed and the marker is *not* written, so the user still
    gets the notice the next time they run Pengy by hand.
    """
    if _no_nudge() or gui_available():
        return  # GUI present → nothing to advertise

    stream = sys.stderr if stream is None else stream
    try:
        if not stream.isatty():
            return
    except (AttributeError, ValueError):
        return

    marker = _marker_path()
    try:
        if marker.exists():
            return
    except OSError:
        pass

    print(build_notice(), file=stream)

    try:
        from pengy import __version__

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            "Delete this file to see the Pengy desktop notice again.\n"
            f"(created by pengy {__version__})\n",
            encoding="utf-8",
        )
    except OSError:
        # A read-only home directory must never break the command that asked
        # for the notice.
        pass


def show_always(stream=None) -> None:
    """Show the notice unconditionally (the ``pengy`` desktop entry point).

    ``stream`` is injectable for tests; defaults to :data:`sys.stderr`.
    """
    stream = sys.stderr if stream is None else stream
    if _no_nudge():
        print(build_compact_notice(), file=stream)
        return
    print(build_notice(), file=stream)
