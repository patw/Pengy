"""Desktop launcher integration for pip/uv-tool installs (Linux).

Why this lives in the *program* rather than the installer: pip only ever creates
console scripts. It cannot drop a ``.desktop`` file, an icon, a Start-Menu entry
or a ``.app`` bundle — and it cannot run code at install time either (a wheel
install executes none of our code; see :mod:`pengy.core.nudge` for the receipts).
So an integrated launcher has to be something the user asks the installed
program to do, which is what ``pengy --install-launcher`` is for.

Linux only, deliberately:

* **Linux** — a freedesktop entry in ``~/.local/share/applications`` plus an
  icon in ``~/.local/share/icons/hicolor``. No sudo, PEP 668-safe, works inside
  a venv/uv-tool/pipx environment.
* **Windows / macOS** — not implemented here. Windows shortcuts should target
  the ``pengy-gui`` entry point (a ``gui_scripts`` console-less launcher);
  macOS is served properly by the ``.dmg``, which drags into /Applications.

The ``Exec`` line is built from :data:`sys.executable`, never from
``shutil.which("pengy")``: the Pengy editions share command names, so a Rust
``pengy`` earlier in ``PATH`` would otherwise be launched instead of this one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP_FILE_NAME = "pengy.desktop"
ICON_NAME = "pengy.png"

# Where the launcher lives, and what it invokes.  Keeping the icon next to the
# entry means uninstall is a two-file cleanup with no leftovers.
ICON_REL = Path("icons/hicolor/256x256/apps") / ICON_NAME


class LauncherError(RuntimeError):
    """Raised when a launcher action cannot be carried out."""


# ---------------------------------------------------------------------------
# XDG plumbing
# ---------------------------------------------------------------------------


def _data_home() -> Path:
    """``$XDG_DATA_HOME`` (default ``~/.local/share``)."""
    env = os.environ.get("XDG_DATA_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share"


def desktop_file_path() -> Path:
    return _data_home() / "applications" / DESKTOP_FILE_NAME


def icon_file_path() -> Path:
    return _data_home() / ICON_REL


def _source_icon() -> Path:
    """The icon shipped inside the package (verified present in the wheel)."""
    return Path(__file__).resolve().parent.parent / "assets" / "icon.png"


def system_entries() -> list[Path]:
    """Existing system-wide pengy entries.

    macOS/XDG install roots to check.  A user-level entry in ``$XDG_DATA_HOME``
    takes precedence over these, so finding one is a warning, not a problem —
    but it is worth telling the user, since the two can drift apart (e.g. a
    system ``.deb`` install plus a user launcher pointing at a venv).
    """
    roots = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    found = []
    for root in roots.split(":"):
        if not root:
            continue
        candidate = Path(root) / "applications" / DESKTOP_FILE_NAME
        if candidate.exists():
            found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# Desktop Entry generation
# ---------------------------------------------------------------------------

# Characters that force quoting in an Exec value (freedesktop Desktop Entry
# spec: reserved characters must be quoted, and \, ", $ and ` escaped).
_SAFE_EXEC_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-+:=@%,"
)


def _exec_quote(value: str) -> str:
    """Quote one Exec argument per the Desktop Entry spec if needed."""
    if value and all(ch in _SAFE_EXEC_CHARS for ch in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for ch in ("$", "`"):
        escaped = escaped.replace(ch, "\\" + ch)
    return f'"{escaped}"'


def build_desktop_entry(python: str | None = None) -> str:
    """Return the contents of the user-level ``pengy.desktop`` file."""
    python = python or sys.executable
    return "\n".join(
        [
            "[Desktop Entry]",
            "Version=1.0",
            "Type=Application",
            "Name=Pengy",
            "GenericName=AI Agent",
            "Comment=Local-first AI agent with tools",
            f"Exec={_exec_quote(python)} -m pengy.main",
            f"Icon={icon_file_path()}",
            "Terminal=false",
            # Exactly one main category (Network) plus the related additional one
            # (Chat). Listing several main categories makes the app appear more
            # than once in the menu, and `Chat` without `Network` is flagged by
            # desktop-file-validate as incomplete — probed all four
            # combinations; `Network;Chat;` is the only hint-free one.
            "Categories=Network;Chat;",
            "Keywords=ai;llm;chat;agent;openai;ollama;",
            "StartupNotify=true",
            # Qt derives WM_CLASS from setApplicationName("Pengy"); telling the
            # shell about it stops a second, ungrouped taskbar icon appearing.
            "StartupWMClass=Pengy",
            "",
        ]
    )


def _refresh_caches() -> list[str]:
    """Update the desktop database / icon cache when the tools are available."""
    done: list[str] = []
    apps_dir = desktop_file_path().parent.parent
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(apps_dir)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        done.append("update-desktop-database")

    icons_dir = icon_file_path().parent.parent.parent  # …/icons
    if shutil.which("gtk-update-icon-cache") and (icons_dir / "index.theme").exists():
        # Only meaningful for a themed directory that already has an index;
        # running it on a bare tree just prints noise.
        subprocess.run(
            ["gtk-update-icon-cache", "-q", "-t", str(icons_dir)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        done.append("gtk-update-icon-cache")
    return done


def _install_icon(destination: Path) -> None:
    """Install the packaged icon at the conventional 256x256 size.

    The shipped ``assets/icon.png`` is 1176x1176, so copying it verbatim into a
    ``hicolor/256x256`` directory would misdescribe the file.  Pillow is already
    a base dependency, so resize it to what the directory claims; if Pillow is
    somehow unavailable, copy the file as-is rather than failing the install.
    """
    source = _source_icon()
    try:
        from PIL import Image

        with Image.open(source) as img:
            square = img.convert("RGBA")
            square.thumbnail((256, 256), Image.LANCZOS)
            square.save(destination, format="PNG", optimize=True)
    except Exception:  # pragma: no cover - defensive: never block the launcher
        shutil.copyfile(source, destination)


# ---------------------------------------------------------------------------
# Public actions
# ---------------------------------------------------------------------------


def install_launcher(force: bool = False) -> str:
    """Create the user-level launcher entry.  Returns a human-readable report.

    Refuses to replace an existing entry that Pengy did not write (``force=True``
    overrides).  This matters: someone who already has a launcher for a *native*
    build (AppImage, or a system ``.deb`` entry) would otherwise lose it silently
    — the exact way the beholaptop AppImage launcher was clobbered on 2026-09-14.
    """
    if sys.platform != "linux":
        raise LauncherError(
            "the launcher entry is Linux-only. "
            "On macOS use the .dmg from https://github.com/patw/PengyR/releases; "
            "on Windows create a shortcut to `pengy-gui`."
        )

    icon_src = _source_icon()
    if not icon_src.exists():  # pragma: no cover - packaging accident
        raise LauncherError(f"package icon missing at {icon_src}")

    entry = desktop_file_path()
    icon = icon_file_path()
    content = build_desktop_entry()

    if entry.exists() and not force:
        existing = entry.read_text(encoding="utf-8")
        if existing != content:
            raise LauncherError(
                f"{entry} already exists and was not written by this command:\n"
                + "\n".join(
                    f"      {line}"
                    for line in existing.splitlines()
                    if line.startswith(("Exec=", "Icon=", "Name="))
                )
                + "\n   Refusing to replace it. Re-run with --force to overwrite "
                "(e.g. if that entry launches a native PengyR/AppImage build, "
                "you probably want to keep it)."
            )

    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)

    _install_icon(icon)
    entry.write_text(content, encoding="utf-8")
    # 0644: the entry is data, not a program.
    entry.chmod(0o644)
    icon.chmod(0o644)

    refreshed = _refresh_caches()

    lines = [
        f"✅ Installed launcher entry: {entry}",
        f"   icon: {icon}",
        f"   launches: {sys.executable} -m pengy.main",
    ]
    if refreshed:
        lines.append(f"   refreshed: {', '.join(refreshed)}")
    shadowed = system_entries()
    if shadowed:
        lines.append(
            "   note: a system-wide entry also exists "
            f"({', '.join(str(p) for p in shadowed)}) — the user entry above takes "
            "precedence for your user account."
        )
    if not Path(sys.executable).exists():  # pragma: no cover - pathological
        lines.append(
            "   warning: the interpreter path above does not currently exist; "
            "re-run this command from the environment you want to launch."
        )
    lines.append(
        "   Pengy should now appear in your application menu "
        "(it may take a few seconds, or a re-login on some desktops)."
    )
    return "\n".join(lines)


def uninstall_launcher() -> str:
    """Remove the user-level launcher entry.  Returns a human-readable report."""
    entry = desktop_file_path()
    icon = icon_file_path()

    removed = []
    for path in (entry, icon):
        try:
            if path.exists():
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            raise LauncherError(f"could not remove {path}: {exc}") from exc

    if not removed:
        return "Nothing to remove — no user-level launcher entry was installed."

    refreshed = _refresh_caches()
    lines = ["✅ Removed:"]
    lines += [f"   {p}" for p in removed]
    if refreshed:
        lines.append(f"   refreshed: {', '.join(refreshed)}")
    return "\n".join(lines)
