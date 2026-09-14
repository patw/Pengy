"""Tests for the user-level desktop launcher integration.

The launcher exists because pip only ever creates console scripts — it cannot
drop a ``.desktop`` file or an icon — so the installed program has to offer it.
What must hold:

* the entry launches **this** environment's interpreter (never whatever
  ``pengy`` happens to be first on ``PATH`` — the Pengy editions share command
  names, so the Rust build could be picked up by mistake),
* paths with spaces are quoted per the Desktop Entry spec,
* install is idempotent, uninstall cleans up both files,
* nothing is written outside ``$XDG_DATA_HOME``,
* a system-wide entry is reported (the user entry shadows it),
* non-Linux platforms get a clear message pointing at the native builds,
* and installing a launcher for a GUI that cannot open a window is refused.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pengy.core import launcher


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Redirect XDG_DATA_HOME at a temp dir and hide any system entries."""
    data = tmp_path / "share"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "empty-sys"))
    return data


class TestDesktopEntry:
    def test_launches_this_interpreter_not_path_pengy(self, xdg, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/opt/venvs/pengy/bin/python")
        entry = launcher.build_desktop_entry()
        assert "Exec=/opt/venvs/pengy/bin/python -m pengy.main" in entry
        # a bare `pengy` would resolve through PATH to whichever edition is first
        assert "Exec=pengy" not in entry

    def test_quotes_paths_with_spaces(self, xdg, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/home/some one/my venv/bin/python")
        entry = launcher.build_desktop_entry()
        assert 'Exec="/home/some one/my venv/bin/python" -m pengy.main' in entry

    def test_required_keys_present(self, xdg):
        entry = launcher.build_desktop_entry()
        for key in (
            "Type=Application",
            "Name=Pengy",
            "Icon=",
            "Terminal=false",
            "Categories=Network;Chat;",
            # stops a second, ungrouped taskbar/dock icon
            "StartupWMClass=Pengy",
        ):
            assert key in entry, key

    def test_exactly_one_main_category_with_chat_satisfied(self, xdg):
        """`Chat` is an additional category; it needs its related main (Network)."""
        entry = launcher.build_desktop_entry()
        cats = next(l for l in entry.splitlines() if l.startswith("Categories="))
        values = cats.split("=", 1)[1].strip(";").split(";")
        main_categories = {
            "AudioVideo", "Audio", "Video", "Development", "Education", "Game",
            "Graphics", "Network", "Office", "Science", "Settings", "System",
            "Utility",
        }
        mains = [c for c in values if c in main_categories]
        assert mains == ["Network"], mains
        assert "Chat" in values and "Network" in values

    @pytest.mark.skipif(
        subprocess.run(["which", "desktop-file-validate"], capture_output=True).returncode
        != 0,
        reason="desktop-file-validate not installed",
    )
    def test_generated_entry_passes_the_real_validator(self, xdg, tmp_path):
        path = tmp_path / "pengy.desktop"
        path.write_text(launcher.build_desktop_entry(), encoding="utf-8")
        result = subprocess.run(
            ["desktop-file-validate", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert result.stdout == "" and result.stderr == ""


class TestInstallUninstall:
    def test_install_writes_entry_and_icon(self, xdg):
        report = launcher.install_launcher()
        assert launcher.desktop_file_path().exists()
        assert launcher.icon_file_path().exists()
        assert str(launcher.desktop_file_path()) in report

    def test_installed_icon_is_a_256px_png(self, xdg):
        """The icon must match what the hicolor/256x256 directory claims.

        The packaged source is 1176x1176, so a verbatim copy would be a lie to
        the icon theme (and 714 KB instead of a few KB).
        """
        pytest.importorskip("PIL", reason="Pillow is a base dependency")
        from PIL import Image

        launcher.install_launcher()
        with Image.open(launcher.icon_file_path()) as img:
            assert img.format == "PNG"
            assert max(img.size) <= 256
        # and it is smaller than the source, i.e. actually re-encoded
        assert (
            launcher.icon_file_path().stat().st_size
            < launcher._source_icon().stat().st_size
        )

    def test_install_is_idempotent(self, xdg):
        launcher.install_launcher()
        first = launcher.desktop_file_path().read_text()
        launcher.install_launcher()
        assert launcher.desktop_file_path().read_text() == first
        assert len(list(launcher.desktop_file_path().parent.glob("pengy.desktop"))) == 1

    def test_uninstall_removes_both_files(self, xdg):
        launcher.install_launcher()
        report = launcher.uninstall_launcher()
        assert not launcher.desktop_file_path().exists()
        assert not launcher.icon_file_path().exists()
        assert "Removed" in report

    def test_uninstall_when_nothing_installed_is_not_an_error(self, xdg):
        assert "Nothing to remove" in launcher.uninstall_launcher()

    def test_nothing_written_outside_xdg_data_home(self, xdg, tmp_path):
        """The only writes allowed are inside $XDG_DATA_HOME.

        (update-desktop-database legitimately adds a mimeinfo.cache there, so we
        assert *where* things were written rather than *how many* files.)
        """
        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        launcher.install_launcher()
        after = {p for p in tmp_path.rglob("*") if p.is_file()}
        new = after - before
        outside = [p for p in new if xdg not in p.parents]
        assert outside == [], outside
        assert launcher.desktop_file_path() in new
        assert launcher.icon_file_path() in new

    def test_reports_shadowed_system_entry(self, xdg, tmp_path):
        sys_dir = tmp_path / "sysshare" / "applications"
        sys_dir.mkdir(parents=True)
        (sys_dir / "pengy.desktop").write_text("[Desktop Entry]\n")
        import os

        # XDG_DATA_DIRS was pinned by the fixture; extend it with the fake system dir
        os.environ["XDG_DATA_DIRS"] = f"{tmp_path / 'sysshare'}:/usr/share"
        report = launcher.install_launcher()
        assert "system-wide entry" in report
        assert "takes precedence" in report

    def test_refuses_to_replace_a_foreign_entry(self, xdg):
        """A launcher for a *native* build must not be silently replaced.

        This is the beholaptop 2026-09-14 incident in test form: the machine had
        a pengy.desktop starting the Rust AppImage, and a plain
        `pengy --install-launcher` overwrote it.
        """
        entry = launcher.desktop_file_path()
        entry.parent.mkdir(parents=True, exist_ok=True)
        foreign = (
            "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Pengy\n"
            "Comment=AI chat assistant\n"
            "Exec=/home/someone/Personal/PengyR-x86_64.AppImage\n"
            "Icon=/home/someone/.local/share/icons/pengy.png\n"
            "Terminal=false\nCategories=Utility;\n"
        )
        entry.write_text(foreign)

        with pytest.raises(launcher.LauncherError) as exc:
            launcher.install_launcher()
        message = str(exc.value)
        # the user is shown what they would have lost
        assert "PengyR-x86_64.AppImage" in message
        assert "--force" in message
        # and the foreign entry is still there, byte for byte
        assert entry.read_text() == foreign

    def test_force_replaces_a_foreign_entry(self, xdg):
        entry = launcher.desktop_file_path()
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("[Desktop Entry]\nName=Pengy\nExec=/somewhere/AppImage\n")
        launcher.install_launcher(force=True)
        assert "-m pengy.main" in entry.read_text()

    def test_rerunning_our_own_entry_is_not_treated_as_foreign(self, xdg):
        """Idempotence must survive the foreign-entry guard."""
        launcher.install_launcher()
        launcher.install_launcher()  # no force needed
        assert "-m pengy.main" in launcher.desktop_file_path().read_text()

    def test_refuses_on_non_linux(self, xdg, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        with pytest.raises(launcher.LauncherError) as exc:
            launcher.install_launcher()
        assert "Linux-only" in str(exc.value)
        assert "PengyR/releases" in str(exc.value)  # points at the dmg path


class TestEntryPointWiring:
    """`pengy --install-launcher` must be gated on a usable GUI."""

    def _run(self, monkeypatch, argv, gui_available):
        from pengy.core import nudge
        from pengy.main import main

        monkeypatch.setattr(nudge, "gui_available", lambda: gui_available())
        old = sys.argv
        sys.argv = argv
        try:
            with pytest.raises(SystemExit) as exc:
                main()
            return exc.value.code
        finally:
            sys.argv = old

    def test_install_without_qt_exits_1_with_hint(self, xdg, monkeypatch, capsys):
        monkeypatch.setattr("builtins.__import__", _fake_import_without_pyside6)
        code = self._run(
            monkeypatch,
            ["pengy", "--install-launcher"],
            gui_available=lambda: False,
        )
        assert code == 1
        err = capsys.readouterr().err
        assert "Install the desktop GUI first" in err
        assert 'pengy[gui]' in err
        # and nothing was created
        assert not launcher.desktop_file_path().exists()

    def test_install_with_qt_creates_entry(self, xdg, monkeypatch, capsys):
        monkeypatch.setattr("builtins.__import__", _fake_import_with_pyside6)
        code = self._run(
            monkeypatch,
            ["pengy", "--install-launcher"],
            gui_available=lambda: True,
        )
        assert code == 0
        assert launcher.desktop_file_path().exists()
        assert "Installed launcher entry" in capsys.readouterr().out

    def test_help_lists_the_launcher_flags(self, xdg, capsys):
        from pengy.main import main

        old = sys.argv
        sys.argv = ["pengy", "--help"]
        try:
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
        finally:
            sys.argv = old
        out = capsys.readouterr().out
        assert "--install-launcher" in out
        assert "--uninstall-launcher" in out
        assert "pengy-gui" in out  # Windows shortcut guidance


# ---------------------------------------------------------------------------
# import fakes: simulate a PySide6-less environment without touching the real one
# ---------------------------------------------------------------------------

_real_import = __import__

# NOTE for anyone exercising the launcher by hand: the session conftest redirects
# XDG_DATA_HOME and aborts the run if it still points at ~/.local/share.  Do the
# same for a manual `pengy --install-launcher` on a working machine, or you will
# replace that machine's live application-menu entry (it did on 2026-09-14).


def _fake_import_without_pyside6(name, *args, **kwargs):
    if name == "PySide6" or name.startswith("PySide6."):
        raise ImportError("simulated: PySide6 not installed")
    return _real_import(name, *args, **kwargs)


def _fake_import_with_pyside6(name, *args, **kwargs):
    """Pretend PySide6 exists, but never actually import the real Qt toolkit."""
    if name == "PySide6" or name.startswith("PySide6."):
        import types

        return types.ModuleType(name)
    return _real_import(name, *args, **kwargs)
