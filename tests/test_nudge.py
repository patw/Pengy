"""Regression tests for the first-run desktop notice.

The notice exists because pip cannot print anything at install time, so it must
behave predictably in the cases that matter:

* silent when the GUI is available (nothing to advertise),
* **once** per machine for the CLI/Web entry points, and only on a TTY, so cron
  jobs and systemd journals never collect it,
* never written to stdout (``pengy-cli --output json`` must stay parseable),
* always shown by the ``pengy`` desktop entry point, since that user is actively
  looking for a window, and
* silenceable with ``PENGY_NO_NUDGE=1``.

The notice functions take an injectable ``stream`` argument on purpose: patching
``sys.stderr`` from a pytest fixture does *not* work reliably, because pytest
re-installs its capture object at the call phase (after fixtures run), which
silently replaces the stub and makes "assert nothing printed" pass vacuously.
"""

from __future__ import annotations

import pytest

from pengy.core import nudge


_real_import = __import__


def _no_pyside6_import(name, *args, **kwargs):
    """Import fake: make PySide6 look absent without touching the real toolkit."""
    if name == "PySide6" or name.startswith("PySide6."):
        raise ImportError("simulated: PySide6 not installed")
    return _real_import(name, *args, **kwargs)


class _TtyStub:
    """Minimal stderr stand-in with a controllable isatty()."""

    def __init__(self, isatty: bool = True):
        self._isatty = isatty
        self.data = ""

    def isatty(self) -> bool:
        return self._isatty

    def write(self, text: str) -> int:
        self.data += text
        return len(text)

    def flush(self) -> None:
        pass


@pytest.fixture
def isolated_cfg(tmp_path, monkeypatch):
    """Point the config dir (and therefore the marker) at a temp directory."""
    monkeypatch.setenv("PENGY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("PENGY_NO_NUDGE", raising=False)
    return tmp_path


@pytest.fixture
def stream():
    """A fresh TTY stub to pass into the notice functions."""
    return _TtyStub(isatty=True)


def _force_no_gui(monkeypatch):
    monkeypatch.setattr(nudge, "gui_available", lambda: False)


def _force_gui(monkeypatch):
    monkeypatch.setattr(nudge, "gui_available", lambda: True)


class TestNoticeText:
    def test_mentions_both_desktop_paths(self):
        text = nudge.build_notice()
        assert 'pip install "pengy[gui]"' in text
        assert nudge.RELEASES_URL in text

    def test_mentions_the_silencing_env_var(self):
        assert "PENGY_NO_NUDGE=1" in nudge.build_notice()

    def test_mentions_cli_and_web_commands(self):
        text = nudge.build_notice()
        assert "pengy-cli" in text
        assert "pengy-web" in text

    def test_compact_notice_still_actionable(self):
        text = nudge.build_compact_notice()
        assert "pengy[gui]" in text
        assert nudge.RELEASES_URL in text


class TestShowOnce:
    def test_shows_on_first_run_and_writes_marker(self, isolated_cfg, stream, monkeypatch):
        _force_no_gui(monkeypatch)
        nudge.show_once(stream=stream)
        assert "pengy[gui]" in stream.data
        assert (isolated_cfg / nudge.MARKER_NAME).exists()

    def test_stays_silent_on_second_run(self, isolated_cfg, stream, monkeypatch):
        _force_no_gui(monkeypatch)
        nudge.show_once(stream=stream)
        assert "pengy[gui]" in stream.data  # sanity: it did print the first time
        stream.data = ""
        nudge.show_once(stream=stream)
        assert stream.data == ""

    def test_silent_when_gui_is_available(self, isolated_cfg, stream, monkeypatch):
        _force_gui(monkeypatch)
        nudge.show_once(stream=stream)
        assert stream.data == ""
        # and the marker is not burned, so a later GUI-less venv still gets it
        assert not (isolated_cfg / nudge.MARKER_NAME).exists()

    def test_silent_when_stream_is_not_a_tty(self, isolated_cfg, monkeypatch):
        _force_no_gui(monkeypatch)
        non_tty = _TtyStub(isatty=False)
        nudge.show_once(stream=non_tty)
        assert non_tty.data == ""
        # marker must NOT be written: the user should still see it next time
        # they run Pengy by hand.
        assert not (isolated_cfg / nudge.MARKER_NAME).exists()

    def test_env_var_silences_it(self, isolated_cfg, stream, monkeypatch):
        _force_no_gui(monkeypatch)
        monkeypatch.setenv("PENGY_NO_NUDGE", "1")
        nudge.show_once(stream=stream)
        assert stream.data == ""

    def test_unwritable_config_dir_does_not_raise(self, isolated_cfg, monkeypatch, stream):
        """A read-only home must never break the command that asked for a notice.

        ``isolated_cfg`` is requested so CI's ``PENGY_NO_NUDGE=1`` is cleared —
        otherwise this test silently asserts nothing.
        """
        _force_no_gui(monkeypatch)
        monkeypatch.setenv("PENGY_CONFIG_DIR", "/proc/definitely/not/writable")
        nudge.show_once(stream=stream)  # must not raise
        assert "pengy[gui]" in stream.data


class TestShowAlways:
    def test_ignores_the_marker(self, isolated_cfg, stream, monkeypatch):
        _force_no_gui(monkeypatch)
        nudge.show_once(stream=stream)
        stream.data = ""
        nudge.show_always(stream=stream)
        assert "pengy[gui]" in stream.data

    def test_env_var_falls_back_to_compact_notice(self, isolated_cfg, stream, monkeypatch):
        monkeypatch.setenv("PENGY_NO_NUDGE", "1")
        nudge.show_always(stream=stream)
        # still actionable — the user asked for quiet, not for no explanation
        assert "pengy[gui]" in stream.data
        assert nudge.RELEASES_URL in stream.data
        assert "Pengy v" not in stream.data  # the full header is suppressed


class TestWindowsDialog:
    """`notify_without_console` covers the pythonw case we cannot run here.

    A shortcut pointed at `pengy-gui` runs under pythonw, where sys.stdout is
    None and anything printed is invisible — so a double-clicked icon that lacks
    PySide6 would appear to do nothing at all.
    """

    def test_noop_on_posix(self, monkeypatch):
        monkeypatch.setattr(nudge.os, "name", "posix")
        assert nudge.notify_without_console("hello") is False

    def test_noop_when_a_console_is_present(self, monkeypatch):
        monkeypatch.setattr(nudge.os, "name", "nt")
        monkeypatch.setattr("sys.stdout", object())  # console exists
        assert nudge.notify_without_console("hello") is False

    def test_uses_a_message_box_when_there_is_no_console(self, monkeypatch):
        import ctypes

        calls = []

        class _User32:
            def MessageBoxW(self, hwnd, text, title, flags):
                calls.append((hwnd, text, title, flags))

        class _Windll:
            user32 = _User32()

        monkeypatch.setattr(nudge.os, "name", "nt")
        monkeypatch.setattr("sys.stdout", None)
        monkeypatch.setattr(ctypes, "windll", _Windll(), raising=False)

        assert nudge.notify_without_console("needs PySide6", title="Pengy") is True
        assert len(calls) == 1
        hwnd, text, title, flags = calls[0]
        assert hwnd is None and text == "needs PySide6" and title == "Pengy"
        assert flags == 0x10  # MB_ICONERROR

    def test_a_failing_dialog_never_raises(self, monkeypatch):
        monkeypatch.setattr(nudge.os, "name", "nt")
        monkeypatch.setattr("sys.stdout", None)
        # no ctypes.windll at all on this platform -> must be swallowed
        assert nudge.notify_without_console("hello") is False

    def test_gui_entry_attempts_the_dialog_when_qt_is_missing(
        self, isolated_cfg, monkeypatch, capsys
    ):
        """The no-Qt path must reach out to the user on a console-less launch."""
        import sys as _sys

        recorded = []
        monkeypatch.setattr(
            "pengy.core.nudge.notify_without_console",
            lambda message, title="Pengy": recorded.append(message) or True,
        )
        monkeypatch.setattr("builtins.__import__", _no_pyside6_import)

        from pengy.main import main

        argv = _sys.argv
        _sys.argv = ["pengy"]
        try:
            with pytest.raises(SystemExit):
                main()
        finally:
            _sys.argv = argv

        capsys.readouterr()
        assert len(recorded) == 1
        assert "pengy[gui]" in recorded[0]


class TestEntryPoints:
    """The notice must never contaminate stdout (JSON output, CI greps)."""

    def test_cli_version_stdout_is_clean(self, isolated_cfg, capsys):
        import sys as _sys

        from pengy.cli.main import main

        argv = _sys.argv
        _sys.argv = ["pengy-cli", "--version"]
        try:
            try:
                main()
            except SystemExit as exc:  # acceptable: either return or exit 0
                assert exc.code in (0, None)
        finally:
            _sys.argv = argv

        captured = capsys.readouterr()
        assert captured.out.strip().startswith("Pengy v")
        assert "pengy[gui]" not in captured.out

    def test_gui_entry_exits_1_with_instructions_when_qt_missing(
        self, isolated_cfg, monkeypatch, capsys
    ):
        """`pengy` without Qt must fail loudly and point at both desktop paths."""
        import builtins
        import sys as _sys

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ImportError("simulated: PySide6 not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        from pengy.main import main

        argv = _sys.argv
        _sys.argv = ["pengy"]
        try:
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1
        finally:
            _sys.argv = argv

        err = capsys.readouterr().err
        assert "PySide6" in err
        assert "pengy[gui]" in err
        assert nudge.RELEASES_URL in err

    def test_gui_entry_help_mentions_desktop_options(self, isolated_cfg, capsys):
        import sys as _sys

        from pengy.main import main

        argv = _sys.argv
        _sys.argv = ["pengy", "--help"]
        try:
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
        finally:
            _sys.argv = argv

        out = capsys.readouterr().out
        assert "pengy[gui]" in out
        assert nudge.RELEASES_URL in out
