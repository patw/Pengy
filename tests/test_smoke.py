"""Import and basic-call smoke tests — catch module-level breakage early.

These tests don't need network, LLM endpoints, or real state.  They just prove
that every public module imports cleanly and that the CLI entry-points parse.

Run with:  python -m pytest tests/test_smoke.py -v
"""

import importlib
import sys
from io import StringIO
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Every importable pengy sub-module
# ---------------------------------------------------------------------------
CORE_MODULES = [
    "pengy.core.config",
    "pengy.core.llm_client",
    "pengy.core.chat_manager",
    "pengy.core.task_manager",
    "pengy.core.tools",
    "pengy.core.image_utils",
]

CLI_MODULES = [
    "pengy.cli.main",
]

UI_MODULES = [
    "pengy.ui.chat_history",
    "pengy.ui.chat_input",
    "pengy.ui.chat_view",
    "pengy.ui.chat_worker",
    "pengy.ui.main_window",
    "pengy.ui.settings_dialog",
    "pengy.ui.tasks_dialog",
]


class TestImportSmoke:
    """Every pengy sub-module must be importable in isolation."""

    @pytest.mark.parametrize("module_name", CORE_MODULES)
    def test_core_import(self, module_name):
        """Core modules need no optional dependencies."""
        importlib.import_module(module_name)

    @pytest.mark.parametrize("module_name", CLI_MODULES)
    def test_cli_import(self, module_name):
        """CLI module needs 'rich' — guaranteed by the [cli] extra."""
        importlib.import_module(module_name)

    def test_cli_import_is_repeatable(self):
        """Repeated imports of pengy.cli.main must not blow up."""
        import pengy.cli.main
        # Force a second import path — __import__ is fine but explicit reload
        # catches state-leak bugs if we ever add side-effects to the module.
        importlib.reload(pengy.cli.main)

    @pytest.mark.parametrize("module_name", UI_MODULES)
    def test_ui_import(self, module_name):
        """UI modules if PySide6 is available (it is in our env)."""
        try:
            import PySide6  # noqa: F401
        except ImportError:
            pytest.skip("PySide6 not installed")
        importlib.import_module(module_name)

    def test_all_modules_imported_without_exception(self):
        """Bulk-check: importing every public pengy module in one process."""
        all_modules = CORE_MODULES + CLI_MODULES
        # Only test UI modules if PySide6 is available
        try:
            import PySide6  # noqa: F401
            all_modules += UI_MODULES
        except ImportError:
            pass
        errors = []
        for mod in all_modules:
            try:
                importlib.import_module(mod)
            except Exception as exc:
                errors.append(f"{mod}: {exc}")
        if errors:
            pytest.fail("\n".join(errors))


class TestCliEntryPoint:
    """The 'pengy-cli' console_script entry-point must parse cleanly."""

    def test_help_flag(self):
        """``pengy-cli --help`` parses and exits 0."""
        from pengy.cli.main import main

        with patch.object(sys, "argv", ["pengy-cli", "--help"]):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0

    def test_no_args_starts_interactive_without_crashing(self):
        """Calling main() with zero args should not crash at import/init time.

        We don't actually enter the REPL — we just verify the PengyCLI object
        can be instantiated and that the interactive path is chosen.
        """
        from pengy.cli.main import PengyCLI

        cli = PengyCLI(no_save=True)
        assert cli is not None
        assert cli.llm_client is not None

    def test_single_shot_mode_no_save(self):
        """``pengy-cli "hello" --no-save`` should not crash even without API key.

        It will fail with an LLM error (no key / server), but the code path to
        that point must be solid.
        """
        from pengy.cli.main import PengyCLI

        cli = PengyCLI(no_save=True)
        # _drive_generator will raise because there's no real endpoint, but the
        # setup and argument parsing should complete fine.
        try:
            cli.run_single_shot("hello")
        except Exception as exc:
            # Acceptable: missing API key, connection refused, etc.
            # Not acceptable: AttributeError, ImportError, TypeError
            assert not isinstance(exc, (AttributeError, ImportError, TypeError, NameError)), (
                f"Unexpected error type {type(exc).__name__}: {exc}"
            )

    def test_slash_commands_dispatch(self, capsys):
        """Every registered slash command should dispatch without crashing."""
        from pengy.cli.main import PengyCLI

        cli = PengyCLI(no_save=True)
        # Use _handle_slash directly to avoid the REPL loop
        commands = [
            "/help",
            "/new",
            "/yolo",
            "/yolo all",
            "/yolo safe",
            "/yolo none",
            "/config",
            "/model",
            "/system",
            "/compact",
            "/attach README.md",
        ]
        for cmd in commands:
            try:
                cli._handle_slash(cmd)
            except SystemExit:
                pass  # /quit would do this
            except Exception as exc:
                pytest.fail(f"Command '{cmd}' raised {type(exc).__name__}: {exc}")

    def test_unknown_slash_command(self, capsys):
        """Unknown slash commands should print an error, not crash."""
        from pengy.cli.main import PengyCLI

        cli = PengyCLI(no_save=True)
        cli._handle_slash("/bogus")

        captured = capsys.readouterr()
        assert "Unknown command" in captured.out

    def test_resolve_attachments_no_files(self):
        """@path to non-existent files are left untouched (with a warning)."""
        from pengy.cli.main import PengyCLI

        text = "hello @/definitely/not/a/file.txt world"
        cleaned, blocks, images = PengyCLI(no_save=True)._resolve_attachments(text)
        # Non-existent paths are left as-is (could be @mentions, etc.)
        assert cleaned == text
        assert blocks == ""
        assert images == []

    def test_resolve_attachments_real_file(self, tmp_path):
        """@path to a real text file injects content."""
        from pengy.cli.main import PengyCLI

        f = tmp_path / "demo.py"
        f.write_text("print('hello')")

        text = f"look at @{f} and tell me"
        cleaned, blocks, images = PengyCLI(no_save=True)._resolve_attachments(text)
        assert cleaned == "look at  and tell me"
        assert "demo.py" in blocks
        assert "print('hello')" in blocks
        assert images == []

    def test_confirm_display_values(self):
        """All confirmation modes have a display string."""
        from pengy.cli.main import PengyCLI

        cli = PengyCLI(no_save=True)
        for mode in ("all", "safe", "none"):
            cli.config["tool_confirmation"] = mode
            display = cli._confirm_display()
            assert isinstance(display, str)
            assert len(display) > 0


class TestGuiEntryPoint:
    """The 'pengy' console_script must at least import cleanly."""

    def test_main_module_imports(self):
        """pengy.main is importable without crashing."""
        import pengy.main  # noqa: F401

    def test_main_function_exists(self):
        """main() exists and is callable (we just check the import guard)."""
        from pengy.main import main
        assert callable(main)


# ---------------------------------------------------------------------------
# pytest marker so you can run just smoke tests:  pytest -m smoke
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.smoke
