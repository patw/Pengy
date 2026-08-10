"""Tests for CLI behaviour beyond the import/dispatch smoke checks.

Covers slash-command tab completion, the command registry staying in sync with
the dispatcher, the tool-confirmation display labels, and the confirmation
guard on the destructive /delete command.

Run with:  python -m pytest tests/test_cli.py -v
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pengy.cli import main as cli_main
from pengy.cli.main import PengyCLI, SLASH_COMMANDS, _complete_slash, _setup_readline
from pengy.cli.main import _last_message_lines

readline = pytest.importorskip("readline", reason="readline is Unix-only")


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_cfg():
    """Isolated config dir so /delete tests can't touch real chats."""
    from pengy.core.config import set_config_dir, get_config_dir

    with tempfile.TemporaryDirectory(prefix="pengy-clitest-") as cfg_dir:
        set_config_dir(cfg_dir)
        assert get_config_dir() != Path.home() / ".config" / "pengy"
        yield Path(cfg_dir)
    set_config_dir(None)


@pytest.fixture
def line_buffer(monkeypatch):
    """Drive the completer by faking readline's current line buffer.

    Returns a callable: ``complete(line, text) -> list[str]`` that exhausts the
    completer's state protocol the way readline itself does.
    """
    def complete(line: str, text: str) -> list[str]:
        monkeypatch.setattr(readline, "get_line_buffer", lambda: line)
        matches, state = [], 0
        while True:
            match = _complete_slash(text, state)
            if match is None:
                return matches
            matches.append(match)
            state += 1
            assert state < 200, "completer did not terminate"

    return complete


# ────────────────────────────────────────────────────────────────────
# Tab completion
# ────────────────────────────────────────────────────────────────────

class TestSlashCompletion:
    def test_bare_slash_offers_every_command(self, line_buffer):
        assert line_buffer("/", "/") == list(SLASH_COMMANDS)

    def test_prefix_narrows_to_shared_stem(self, line_buffer):
        assert line_buffer("/mo", "/mo") == ["/model", "/models"]

    def test_exit_and_export_share_a_prefix(self, line_buffer):
        assert sorted(line_buffer("/ex", "/ex")) == ["/exit", "/export"]

    @pytest.mark.parametrize("line,expected", [
        ("/context-", "/context-keep"),
        ("/llm", "/llm-timeout"),
    ])
    def test_hyphenated_commands_complete(self, line_buffer, line, expected):
        """Hyphens must not be treated as word delimiters."""
        assert line_buffer(line, line) == [expected]

    def test_leading_whitespace_tolerated(self, line_buffer):
        assert line_buffer("   /hel", "/hel") == ["/help"]

    def test_yolo_arguments_complete_after_space(self, line_buffer):
        assert line_buffer("/yolo ", "") == ["all", "safe", "none"]

    def test_yolo_argument_prefix_narrows(self, line_buffer):
        assert line_buffer("/yolo s", "s") == ["safe"]

    def test_command_without_known_args_offers_nothing(self, line_buffer):
        """/rename takes free text — don't suggest command names as arguments."""
        assert line_buffer("/rename ", "") == []

    def test_empty_line_is_not_completed(self, line_buffer):
        """Tab on an empty prompt must not dump all 26 commands at the user.

        This is what the ``startswith("/")`` guard actually buys: in argument
        position the unknown-command lookup already returns nothing, so an
        empty buffer is the one case where dropping the guard changes results.
        """
        assert line_buffer("", "") == []
        assert line_buffer("   ", "") == []

    def test_plain_prose_is_not_completed(self, line_buffer):
        """The completer must stay out of the way of ordinary messages."""
        assert line_buffer("write me a hai", "hai") == []
        assert line_buffer("hello", "hello") == []

    def test_slash_mid_sentence_is_not_completed(self, line_buffer):
        assert line_buffer("what is 3/4 of", "3/4") == []
        assert line_buffer("use /tmp/x for", "/tmp/x") == []

    def test_returns_none_when_state_exhausted(self, monkeypatch):
        """readline's protocol requires None (not '') past the last match."""
        monkeypatch.setattr(readline, "get_line_buffer", lambda: "/help")
        assert _complete_slash("/help", 0) == "/help"
        assert _complete_slash("/help", 1) is None

    def test_unknown_command_prefix_yields_no_matches(self, line_buffer):
        assert line_buffer("/zzz", "/zzz") == []


class TestReadlineSetup:
    def test_setup_registers_completer_and_delims(self):
        _setup_readline()
        assert readline.get_completer() is _complete_slash
        delims = readline.get_completer_delims()
        # "/" and "-" are part of command names and must not split words
        assert "/" not in delims
        assert "-" not in delims

    def test_setup_is_idempotent(self):
        _setup_readline()
        _setup_readline()
        assert readline.get_completer() is _complete_slash


# ────────────────────────────────────────────────────────────────────
# Registry / dispatcher sync
# ────────────────────────────────────────────────────────────────────

class TestSlashCommandRegistry:
    @staticmethod
    def _dispatched_commands() -> set[str]:
        """Scrape the command literals out of PengyCLI._handle_slash."""
        src = Path(cli_main.__file__).read_text()
        body = src[src.index("def _handle_slash"):src.index("def _cmd_help")]
        return set(re.findall(r'"(/[a-z-]+)"', body))

    def test_every_dispatched_command_is_completable(self):
        missing = self._dispatched_commands() - set(SLASH_COMMANDS)
        assert not missing, f"dispatched but not tab-completable: {sorted(missing)}"

    def test_every_completable_command_is_dispatched(self):
        extra = set(SLASH_COMMANDS) - self._dispatched_commands()
        assert not extra, f"tab-completable but not dispatched: {sorted(extra)}"

    def test_no_duplicates(self):
        assert len(SLASH_COMMANDS) == len(set(SLASH_COMMANDS))

    def test_all_commands_start_with_slash(self):
        assert all(c.startswith("/") for c in SLASH_COMMANDS)

    def test_help_documents_every_command(self, capsys):
        """Anything completable should be findable in /help."""
        cli = PengyCLI(no_save=True)
        cli._cmd_help()
        out = capsys.readouterr().out
        # /help renders in a rich table that may wrap; strip whitespace to match
        flat = re.sub(r"\s+", "", out)
        undocumented = [c for c in SLASH_COMMANDS if c.replace(" ", "") not in flat]
        assert not undocumented, f"missing from /help: {undocumented}"


# ────────────────────────────────────────────────────────────────────
# Tool-confirmation labels
# ────────────────────────────────────────────────────────────────────

class TestLastMessagePreview:
    def test_wraps_long_url_or_line_across_multiple_lines(self):
        lines = _last_message_lines("Check out " + "https://example.com/" + "x" * 180)
        assert len(lines) == 3
        assert all(len(line) <= 101 for line in lines)
        assert "https://example.com/" in lines[0]

    def test_accepts_terminal_specific_width(self):
        lines = _last_message_lines("abcdefghijklmnopqrstuvwxyz " * 4, width=20)
        assert all(len(line) <= 20 for line in lines)
        assert len(lines) == 6
        assert not lines[-1].endswith("…")

    def test_preserves_recent_multiline_content_up_to_ten_lines(self):
        lines = _last_message_lines("\n".join(f"line {i}" for i in range(20)))
        assert lines[:9] == [f"line {i}" for i in range(9)]
        assert lines[9] == "line 9…"

    def test_flattens_image_content_parts(self):
        lines = _last_message_lines([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}},
            {"type": "text", "text": "Describe this"},
        ])
        assert lines == ["[image] Describe this"]


class TestConfirmDisplay:
    @pytest.mark.parametrize("mode,expected", [
        ("all", "YOLO"),
        ("safe", "Safe"),
        ("none", "Confirm All"),
    ])
    def test_labels(self, mode, expected):
        cli = PengyCLI(no_save=True)
        cli.config["tool_confirmation"] = mode
        assert cli._confirm_display() == expected

    def test_safest_mode_is_not_labelled_none(self):
        """"none" means *confirm everything*; showing "None" reads as the opposite."""
        cli = PengyCLI(no_save=True)
        cli.config["tool_confirmation"] = "none"
        assert cli._confirm_display() != "None"

    def test_unknown_mode_falls_back_to_safest_label(self):
        cli = PengyCLI(no_save=True)
        cli.config["tool_confirmation"] = "nonsense"
        assert cli._confirm_display() == "Confirm All"


    @pytest.mark.parametrize("mode,expected", [
        ("all", "YOLO"),
        ("safe", "Safe"),
        ("none", "Confirm All"),
    ])
    def test_labels(self, mode, expected):
        cli = PengyCLI(no_save=True)
        cli.config["tool_confirmation"] = mode
        assert cli._confirm_display() == expected

    def test_safest_mode_is_not_labelled_none(self):
        """"none" means *confirm everything*; showing "None" reads as the opposite."""
        cli = PengyCLI(no_save=True)
        cli.config["tool_confirmation"] = "none"
        assert cli._confirm_display() != "None"

    def test_unknown_mode_falls_back_to_safest_label(self):
        cli = PengyCLI(no_save=True)
        cli.config["tool_confirmation"] = "nonsense"
        assert cli._confirm_display() == "Confirm All"


# ────────────────────────────────────────────────────────────────────
# /delete confirmation guard
# ────────────────────────────────────────────────────────────────────

class TestDeleteConfirmation:
    @staticmethod
    def _seed(titles):
        from pengy.core.chat_manager import create_chat, save_chat, load_chats

        for title in titles:
            chat = create_chat()
            chat["title"] = title
            save_chat(chat)
        return [c["title"] for c in load_chats()]

    def _cli(self):
        cli = PengyCLI(no_save=True)
        cli.current_chat = None
        return cli

    def test_declining_keeps_the_chat(self, tmp_cfg, capsys):
        from pengy.core.chat_manager import load_chats

        before = self._seed(["Alpha", "Beta"])
        cli = self._cli()

        with patch.object(cli_main, "Confirm") as confirm:
            confirm.ask.return_value = False
            cli._cmd_delete(["1"])

        assert confirm.ask.called, "/delete must ask before destroying a chat"
        assert [c["title"] for c in load_chats()] == before
        assert "Cancelled" in capsys.readouterr().out

    def test_accepting_deletes_the_chat(self, tmp_cfg):
        from pengy.core.chat_manager import load_chats

        before = self._seed(["Alpha", "Beta"])
        cli = self._cli()

        with patch.object(cli_main, "Confirm") as confirm:
            confirm.ask.return_value = True
            cli._cmd_delete(["1"])

        remaining = [c["title"] for c in load_chats()]
        assert remaining == before[1:]
        assert len(remaining) == len(before) - 1

    def test_confirm_defaults_to_no(self, tmp_cfg):
        """A bare Enter at the prompt must not delete anything."""
        self._seed(["Alpha"])
        cli = self._cli()

        with patch.object(cli_main, "Confirm") as confirm:
            confirm.ask.return_value = False
            cli._cmd_delete(["1"])

        assert confirm.ask.call_args.kwargs.get("default") is False

    def test_prompt_names_the_chat_being_deleted(self, tmp_cfg):
        self._seed(["Alpha", "DeleteMe"])
        cli = self._cli()

        with patch.object(cli_main, "Confirm") as confirm:
            confirm.ask.return_value = False
            cli._cmd_delete(["1"])  # newest first -> "DeleteMe"

        assert "DeleteMe" in confirm.ask.call_args.args[0]

    @pytest.mark.parametrize("args", [[], ["0"], ["99"], ["abc"]])
    def test_invalid_index_never_prompts(self, tmp_cfg, args):
        """Bad input should be rejected before the user is asked anything."""
        from pengy.core.chat_manager import load_chats

        before = self._seed(["Alpha"])
        cli = self._cli()

        with patch.object(cli_main, "Confirm") as confirm:
            cli._cmd_delete(args)

        assert not confirm.ask.called
        assert [c["title"] for c in load_chats()] == before
