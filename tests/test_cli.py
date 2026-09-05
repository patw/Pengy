"""Tests for CLI behaviour beyond the import/dispatch smoke checks.

Covers slash-command tab completion, the command registry staying in sync with
the dispatcher, the tool-confirmation display labels, and the confirmation
guard on the destructive /delete command.

Run with:  python -m pytest tests/test_cli.py -v
"""
from __future__ import annotations

import io
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.markup import escape

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


# ────────────────────────────────────────────────────────────────────
# /redact
# ────────────────────────────────────────────────────────────────────

class TestRedact:
    def _cli_with_chat(self, messages):
        from pengy.core.chat_manager import create_chat, save_chat

        cli = PengyCLI(no_save=True)
        chat = create_chat()
        chat["messages"] = messages
        save_chat(chat)
        cli.current_chat = chat
        return cli

    def test_redact_default_removes_one_message(self, tmp_cfg):
        from pengy.core.chat_manager import get_chat

        cli = self._cli_with_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        cli._cmd_redact([])

        assert cli.current_chat["messages"] == [{"role": "user", "content": "hi"}]
        assert get_chat(cli.current_chat["id"])["messages"] == cli.current_chat["messages"]

    def test_redact_n_removes_n_messages(self, tmp_cfg):
        cli = self._cli_with_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "two"},
        ])
        cli._cmd_redact(["3"])
        assert cli.current_chat["messages"] == [{"role": "user", "content": "hi"}]

    def test_redact_more_than_available_empties_without_erroring(self, tmp_cfg):
        cli = self._cli_with_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        cli._cmd_redact(["50"])
        assert cli.current_chat["messages"] == []

    def test_redact_repeatable_to_empty(self, tmp_cfg):
        cli = self._cli_with_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        cli._cmd_redact([])
        cli._cmd_redact([])
        assert cli.current_chat["messages"] == []
        # A third redact on an already-empty chat must not raise.
        cli._cmd_redact([])
        assert cli.current_chat["messages"] == []

    def test_redact_no_active_chat(self, tmp_cfg):
        cli = PengyCLI(no_save=True)
        cli.current_chat = None
        cli._cmd_redact([])  # must not raise

    @pytest.mark.parametrize("args", [["0"], ["-1"], ["abc"]])
    def test_redact_invalid_n_rejected(self, tmp_cfg, args):
        cli = self._cli_with_chat([{"role": "user", "content": "hi"}])
        cli._cmd_redact(args)
        # Nothing removed on bad input.
        assert cli.current_chat["messages"] == [{"role": "user", "content": "hi"}]


# ────────────────────────────────────────────────────────────────────
# /tasks and /task
# ────────────────────────────────────────────────────────────────────

class TestTasks:
    def _cli(self):
        from pengy.core.chat_manager import create_chat

        cli = PengyCLI(no_save=True)
        cli.current_chat = create_chat()
        # _send_text drives a real LLM generator — stub it out so these tests
        # only exercise task lookup/placeholder-filling/rendering.
        cli._drive_generator = lambda messages, chat: None
        return cli

    def test_tasks_empty_shows_hint(self, tmp_cfg, capsys):
        cli = self._cli()
        cli._cmd_tasks()
        assert "No tasks defined" in capsys.readouterr().out

    def test_tasks_lists_saved_templates(self, tmp_cfg, capsys):
        from pengy.core.task_manager import create_task

        create_task("Greet", "Say hello to %name%")
        cli = self._cli()
        cli._cmd_tasks()
        out = capsys.readouterr().out
        assert "Greet" in out
        assert "%name%" in out

    def test_task_no_args_lists_tasks(self, tmp_cfg, capsys):
        from pengy.core.task_manager import create_task

        create_task("Greet", "Say hello to %name%")
        cli = self._cli()
        cli._cmd_task([])
        assert "Greet" in capsys.readouterr().out

    def test_task_fills_placeholders_and_sends(self, tmp_cfg):
        from pengy.core.task_manager import create_task

        create_task("Greet", "Say hello to %name% in %language%")
        cli = self._cli()

        with patch.object(cli_main, "Prompt") as prompt:
            prompt.ask.side_effect = ["Ada", "French"]
            cli._cmd_task(["1"])

        assert cli.current_chat["messages"][-1] == {
            "role": "user", "content": "Say hello to Ada in French",
        }

    def test_task_with_no_placeholders_never_prompts(self, tmp_cfg):
        from pengy.core.task_manager import create_task

        create_task("Static", "Summarize the last file I read.")
        cli = self._cli()

        with patch.object(cli_main, "Prompt") as prompt:
            cli._cmd_task(["1"])

        assert not prompt.ask.called
        assert cli.current_chat["messages"][-1]["content"] == "Summarize the last file I read."

    def test_task_empty_render_is_not_sent(self, tmp_cfg):
        from pengy.core.task_manager import create_task

        create_task("Blank", "   ")
        cli = self._cli()
        before = len(cli.current_chat["messages"])
        cli._cmd_task(["1"])
        assert len(cli.current_chat["messages"]) == before

    @pytest.mark.parametrize("args", [["0"], ["99"], ["abc"]])
    def test_task_invalid_index(self, tmp_cfg, args, capsys):
        from pengy.core.task_manager import create_task

        create_task("Greet", "hi %name%")
        cli = self._cli()
        before = len(cli.current_chat["messages"])
        cli._cmd_task(args)
        assert len(cli.current_chat["messages"]) == before
        assert "Usage" in capsys.readouterr().out

    def test_task_no_active_chat(self, tmp_cfg):
        cli = PengyCLI(no_save=True)
        cli.current_chat = None
        cli._cmd_task(["1"])  # must not raise


# ────────────────────────────────────────────────────────────────────
# Cumulative token usage
# ────────────────────────────────────────────────────────────────────

class TestCumulativeUsage:
    def _cli(self, mode="pretty"):
        cli = PengyCLI(no_save=True)
        cli._output_mode = mode
        return cli

    def test_render_final_accumulates_across_turns(self, capsys):
        cli = self._cli()
        chat = {"id": "c1", "messages": []}

        cli._render_final({
            "content": "hi", "message": {"role": "assistant", "content": "hi"},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }, chat)
        assert chat["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        out = capsys.readouterr().out
        assert "total this turn" in out
        assert "15 total this chat" in out

        cli._render_final({
            "content": "hi again", "message": {"role": "assistant", "content": "hi again"},
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        }, chat)
        assert chat["usage"] == {"prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43}
        out = capsys.readouterr().out
        assert "43 total this chat" in out

    def test_render_final_no_usage_creates_zeroed_total(self):
        cli = self._cli()
        chat = {"id": "c1", "messages": []}
        cli._render_final({"content": "hi", "message": {"role": "assistant", "content": "hi"}}, chat)
        assert chat["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ────────────────────────────────────────────────────────────────────
# Mid-turn assistant narration
# ────────────────────────────────────────────────────────────────────

class TestAssistantPreamble:
    """Text the model writes alongside its tool calls must render live.

    It is persisted with the turn, so a run that only showed the tool cards
    looked like the model went straight to the tools with nothing to say —
    the narration then appeared out of nowhere on a later /show.
    """

    @staticmethod
    def _cli(mode="pretty"):
        cli = PengyCLI(no_save=True)
        cli._output_mode = mode
        return cli

    def _capture(self, cli, message):
        printed = []
        with patch.object(cli.console, "print", side_effect=lambda *a, **k: printed.append(a)):
            with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a)):
                cli._render_assistant_preamble(message)
        return printed

    def test_pretty_mode_renders_narration(self):
        cli = self._cli()
        printed = self._capture(cli, {"role": "assistant", "content": "Let me check that."})
        assert printed, "narration was dropped instead of rendered"

    def test_raw_mode_prints_plain_text(self):
        cli = self._cli("raw")
        printed = self._capture(cli, {"role": "assistant", "content": "Let me check."})
        assert printed == [("Let me check.",)]

    @pytest.mark.parametrize("content", ["", "   ", None])
    def test_empty_narration_prints_nothing(self, content):
        cli = self._cli()
        assert self._capture(cli, {"role": "assistant", "content": content}) == []

    @pytest.mark.parametrize("mode", ["json", "silent"])
    def test_machine_modes_stay_quiet(self, mode):
        """json builds one object from the final response; stray text breaks it."""
        cli = self._cli(mode)
        assert self._capture(cli, {"role": "assistant", "content": "chatter"}) == []


class TestAttachments:
    """@path attachment resolution, including the silent-failure warning."""

    @staticmethod
    def _cli():
        return PengyCLI(no_save=True)

    def test_real_file_attaches_silently(self, tmp_path):
        cli = self._cli()
        real = tmp_path / "note.txt"
        real.write_text("hello from note")
        with patch.object(cli.console, "print") as mock_print:
            cleaned, blocks, images = cli._resolve_attachments(f"read @{real}")
        assert blocks and "hello from note" in blocks
        assert images == []
        assert not mock_print.called, "a real attachment must not warn"

    def test_missing_path_warns_and_stays_literal(self):
        cli = self._cli()
        with patch.object(cli.console, "print") as mock_print:
            cleaned, blocks, images = cli._resolve_attachments("read @missing.txt")
        assert blocks == ""
        assert images == []
        assert "@missing.txt" in cleaned  # left as literal text
        assert mock_print.called, "a path-looking token that fails to resolve must warn"

    def test_plain_at_mention_does_not_warn(self):
        cli = self._cli()
        with patch.object(cli.console, "print") as mock_print:
            cleaned, blocks, images = cli._resolve_attachments("mention @someone here")
        assert blocks == ""
        assert images == []
        assert "@someone" in cleaned
        assert not mock_print.called, "an @mention with no path shape is not an attachment"


# ────────────────────────────────────────────────────────────────
# Markup safety: the CLI must never crash or silently drop content
# when rendering untrusted text containing rich markup characters.
# Regression: a bracketed path (e.g. tests/test_mediainfo.cpp(125)) in
# tool/compiler output used to crash console.print with a MarkupError and
# strip '[bracketed]' text from tool-result Panels.
# ────────────────────────────────────────────────────────────────

class TestMarkupSafety:
    """Render methods must not parse untrusted text as rich markup."""

    @staticmethod
    def _cli():
        cli = PengyCLI(no_save=True)
        cli.console = Console(file=io.StringIO(), force_terminal=False, width=160)
        return cli

    @staticmethod
    def _out(cli):
        return cli.console.file.getvalue()

    def test_tool_result_preserves_bracketed_text(self):
        cli = self._cli()
        cli._render_tool_result({"content": "FAIL: [tests/test_mediainfo.cpp(125)] boom"})
        out = self._out(cli)
        # Bracketed text must be shown literally, not consumed as markup.
        assert "[tests/test_mediainfo.cpp(125)]" in out

    def test_tool_request_bracketed_argv_does_not_raise(self):
        cli = self._cli()
        cli._render_tool_request({"name": "run_bash", "args": {"command": "echo [abc] done"}})
        out = self._out(cli)
        assert "[abc]" in out

    def test_error_message_with_markup_does_not_raise(self):
        # The exception handler prints error text; a message carrying a
        # closing-tag-like '[...]' must render as literal text, not throw.
        cli = self._cli()
        cli.console.print("\n[red]Error:[/red] " + escape("ValueError: [/home/x/file(3)] boom"))
        out = self._out(cli)
        assert "/home/x/file(3)" in out

    def test_closing_tag_style_content_does_not_crash_panel(self):
        cli = self._cli()
        cli._render_tool_result({"content": "[/usr/bin/ffprobe] could not start"})
        out = self._out(cli)
        assert "/usr/bin/ffprobe" in out

    def test_tool_result_strips_ansi_and_control(self):
        # ANSI color + ESC title + a C0 control byte must not reach the terminal.
        cli = self._cli()
        cli._render_tool_result({"content": "\x1b[31mred\x1b[0m \x1b]0;evil\x07[end"})
        out = self._out(cli)
        assert "red" in out            # text survives
        assert "\x1b" not in out       # escapes stripped

    def test_tool_request_strips_ansi(self):
        cli = self._cli()
        cli._render_tool_request({"name": "run_bash", "args": {"command": "echo \x1b[32mgreen\x1b[0m"}})
        out = self._out(cli)
        assert "\x1b" not in out
        assert "green" in out

    def test_sanitize_display_unit(self):
        from pengy.cli.main import _sanitize_display
        assert _sanitize_display("\x1b[31mred\x1b[0m") == "red"
        assert _sanitize_display("a\x00b\x07c\x1fd") == "abcd"
        assert _sanitize_display("line1\nline2\tend") == "line1\nline2\tend"
        assert _sanitize_display("FAIL: [tests/test.cpp(9)]") == "FAIL: [tests/test.cpp(9)]"
        assert _sanitize_display("a\x1b]0;evil\x07b") == "ab"
