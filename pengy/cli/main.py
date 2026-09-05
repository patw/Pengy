"""Pengy CLI — command-line interface for Pengy.

Interactive mode:  pengy-cli
Single-shot mode:  pengy-cli "What is the capital of France?"
"""

import argparse
import atexit
import json
import os
import re
import shlex
import sys
import textwrap
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from pengy.core.config import load_config, save_config, render_system_message
from pengy.core.llm_client import LLMClient
from pengy.core.chat_manager import (
    load_index, get_chat, create_chat, save_chat, delete_chat,
    clean_dangling_tool_calls, elide_old_tool_results, redact_last_message,
    add_usage,
)
from pengy.core import task_manager
from pengy.core.about import (
    CATBEE_BLURB, CATBEE_URL, DESCRIPTION, GITHUB_URL, LICENSE_NAME, LICENSE_URL,
    WEBSITE_URL, copyright_line, edition_line,
)
from pengy.core.image_utils import preprocess as preprocess_image
from pengy.core.attachments import attachment_label, import_image, resolve_history
from pengy.core import tools


# ANSI escape-sequence / control-character sanitizer for terminal display.
# Untrusted tool/compiler output is rendered literally, but it may carry escape
# sequences (color, cursor moves, OSC title) or C0 control bytes. Strip those
# from what we show the user so they can't move the cursor, clear the screen, or
# confuse the box-width accounting. Display-only: the raw bytes still go to the
# model untouched.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_DCS = re.compile(r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))|(?:\x1b[PX^_][^\x1b]*(?:\x1b\\)?)")
_ANSI_OTHER = re.compile(r"\x1b[@-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_display(s):
    """Strip ANSI escape sequences and non-\n/\t C0 control chars for display."""
    s = _ANSI_OSC_DCS.sub("", s)
    s = _ANSI_CSI.sub("", s)
    s = _ANSI_OTHER.sub("", s)
    return _CTRL.sub("", s)

# ── readline history + completion (Unix only, graceful fallback) ──

_HISTFILE: Path | None = None

# Every slash command the REPL dispatches, for tab completion.
# Keep in sync with PengyCLI._handle_slash and PengyCLI._cmd_help.
SLASH_COMMANDS = (
    "/help", "/new", "/show", "/tail", "/rename", "/clear", "/export",
    "/yolo", "/config", "/model", "/models", "/list", "/load", "/baseurl",
    "/apikey", "/llm-timeout", "/timeout", "/download-max", "/agent", "/context-keep",
    "/system", "/delete", "/attach", "/attachments", "/compact", "/redact",
    "/tasks", "/task", "/about", "/quit", "/exit", "/q",
)

# Sub-arguments worth completing once the command is typed.
_SLASH_ARGS = {
    "/yolo": ("all", "safe", "none"),
}


def _complete_slash(text: str, state: int) -> str | None:
    """readline completer for slash commands and their known arguments.

    Only engages when the line starts with "/" so it never interferes with
    ordinary prompt text.
    """
    import readline

    line = readline.get_line_buffer()
    stripped = line.lstrip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split()
    completing_arg = len(parts) > 1 or (parts and line.endswith(" "))

    if completing_arg:
        options = _SLASH_ARGS.get(parts[0].lower(), ())
    else:
        options = SLASH_COMMANDS

    matches = [o for o in options if o.startswith(text)]
    return matches[state] if state < len(matches) else None


def _setup_readline() -> Path | None:
    """Enable readline line-editing, persistent history, and completion."""
    try:
        import readline
    except ImportError:
        return None

    readline.set_completer(_complete_slash)
    # "/" and "-" are part of command names, so they must not split words.
    readline.set_completer_delims(" \t\n")
    # libedit (macOS) uses a different binding syntax than GNU readline.
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    hist_dir = Path(os.environ.get(
        "XDG_STATE_HOME",
        os.path.join(os.environ.get("HOME", "~"), ".local", "state"),
    )) / "pengy"
    hist_dir.mkdir(parents=True, exist_ok=True)
    hist_file = hist_dir / "cli_history"

    try:
        readline.read_history_file(str(hist_file))
    except (FileNotFoundError, PermissionError, FileExistsError):
        # macOS libedit can report EEXIST while loading a concurrently
        # initialized history file; an empty/current in-memory history is
        # still valid and setup must remain idempotent.
        pass
    readline.set_history_length(1000)

    atexit.register(readline.write_history_file, str(hist_file))
    return hist_file


BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
BLUE = "\033[34m"


# ---------------------------------------------------------------------------
# friendly import guard for the CLI extra
# ---------------------------------------------------------------------------

def _require_rich():
    """Import and return the ``rich`` module, or print a friendly error."""
    try:
        import rich as _r
        return _r
    except ImportError:
        print(
            "❌ Pengy CLI requires the 'rich' library.\n"
            "   Install it with:  pip install pengy[cli]\n"
            "   Or install everything:  pip install pengy[all]",
            file=sys.stderr,
        )
        sys.exit(1)


_rich = _require_rich()
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def _print_version():
    """Print the Pengy version and exit."""
    from pengy import __version__
    print(f"Pengy v{__version__}")



# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = 72) -> str:
    """Truncate *text* to *max_len* characters, adding an ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def _preview(text: str, max_len: int = 72) -> str:
    """Return a single-line preview of possibly multiline/Markdown text."""
    return _truncate(" ".join(str(text).split()), max_len)

def _last_message_lines(content: object, max_lines: int = 10, width: int = 100) -> list[str]:
    """Format the recent user message as a bounded, readable multi-line preview."""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, dict) and part.get("type") == "image_url":
                parts.append("[image]")
        content = " ".join(parts)
    text = str(content or "").strip()
    if not text:
        return []

    lines = []
    for source_line in text.splitlines() or [text]:
        lines.extend(textwrap.wrap(
            source_line,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""])
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _truncate(lines[-1], max(1, width - 1)) + "…"
    return lines


_TEXT_EXTENSIONS = {
    '.txt', '.md', '.markdown', '.rst', '.json', '.xml', '.html', '.htm',
    '.css', '.js', '.ts', '.py', '.rb', '.go', '.rs', '.c', '.cpp', '.h',
    '.java', '.kt', '.swift', '.sh', '.bash', '.zsh', '.fish', '.ps1',
    '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.config',
    '.env', '.csv', '.tsv', '.sql', '.graphql', '.proto', '.tf',
    '.log', '.diff', '.patch',
}

_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}


def _is_text_file(path: Path) -> bool:
    """Return True if *path* looks like a text file."""
    if path.suffix.lower() in _TEXT_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as f:
            f.read(8192).decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def _inject_file_content(path: Path) -> str:
    """Return a fenced code block with the file's content."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return f"[File: {path.name}]\n```\n{content}\n```"
    except Exception as e:
        return f"[File: {path.name} — error reading: {e}]"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class PengyCLI:
    """Command-line interface for Pengy."""

    def __init__(self, no_save: bool = False):
        self.config = load_config()
        self.console = Console()
        self.llm_client: LLMClient | None = None
        self.current_chat: dict | None = None
        self._no_save = no_save
        self._output_mode = "pretty"  # pretty | raw | json | silent
        self._update_llm_client()

        # "yes to all this turn" — resets each time the LLM returns a fresh
        # set of tool calls (i.e. each API round-trip).
        self._yolo_this_turn = False

    # ------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------

    def run_interactive(self):
        """Start the interactive REPL."""
        # Startup banner
        self.console.print()
        self.console.print(
            Panel.fit(
                "[bold]🐧 Pengy CLI[/bold]\n"
                "Type your message and press Enter.  [dim]Try /help for available commands.[/dim]",
                border_style="blue",
            )
        )

        # Load the most recent chat or create a new one. get_chat() can return
        # None if the index outlived its chat file (e.g. killed mid-write), in
        # which case we fall through and start fresh rather than crashing.
        chats = load_index()
        self.current_chat = get_chat(chats[0]["id"]) if chats else None
        if self.current_chat:
            msgs = self.current_chat.get("messages", [])
            msg_count = len(msgs)
            # Find the last user message for context
            last_user = None
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_user = m.get("content", "")
                    break
            # Leave room for the two-space continuation indent while using
            # the actual terminal width instead of a fixed desktop width.
            preview_width = max(20, self.console.width - 2)
            last_preview = _last_message_lines(last_user, width=preview_width)

            self.console.print(
                f"[dim]Resumed:[/dim] [bold]{self.current_chat['title']}[/bold]"
                f" [dim]({msg_count} messages)[/dim]"
            )
            if last_preview:
                self.console.print("[dim]Last:[/dim]")
                for line in last_preview:
                    self.console.print(f"  {line}")
        else:
            self.current_chat = create_chat()
            self.console.print("[dim]New chat created.[/dim]")

        self.console.print(
            f"[dim]Model: {self.config.get('model', '?')}  "
            f"Tool Confirm: {self._confirm_display()}[/dim]"
        )

        tools.set_sudo_password_provider(self._get_sudo_password)
        try:
            self._repl()
        except (KeyboardInterrupt, EOFError):
            self.console.print("\n[dim]Goodbye![/dim]")
        finally:
            tools.set_sudo_password_provider(None)

    def run_single_shot(self, prompt_text: str):
        """Send a single prompt and exit after the response."""
        tools.set_sudo_password_provider(self._get_sudo_password)
        chat = None
        try:
            # Resolve @path attachments
            resolved_text, files_content, image_paths = self._resolve_attachments(prompt_text)
            if files_content:
                prompt_text = files_content + "\n" + resolved_text
            elif resolved_text != prompt_text:
                prompt_text = resolved_text

            display_content = prompt_text
            if image_paths:
                img_placeholders = [f"[Image: {p.name}]" for p in image_paths]
                display_content = "\n".join(img_placeholders + ([prompt_text] if prompt_text else []))

            if self._no_save:
                chat = {
                    "id": str(uuid.uuid4()),
                    "title": _truncate(display_content, 50),
                    "messages": [{"role": "user", "content": display_content}],
                    "created_at": datetime.now().isoformat(),
                }
            else:
                chat = create_chat()
                chat["title"] = _truncate(display_content, 50)
                refs = [import_image(p, p.name, max_dimension=self.config.get("image_max_dimension", 4096), max_mb=self.config.get("image_max_mb", 4.5), quality=self.config.get("image_quality", 85)) for p in image_paths]
                chat["messages"].append({"role": "user", "content": prompt_text, **({"attachments": refs} if refs else {})})
                save_chat(chat)

            messages = self._build_messages(chat, prompt_text,
                                            image_paths=image_paths if image_paths else None)
            self._drive_generator(messages, chat)
        except KeyboardInterrupt:
            self.console.print("\n[dim]Cancelled.[/dim]")
        finally:
            tools.set_sudo_password_provider(None)

    # ------------------------------------------------------------------
    # REPL
    # ------------------------------------------------------------------

    def _repl(self):
        """Read-Eval-Print-Loop for interactive mode."""
        while True:
            try:
                title = self.current_chat.get("title", "?") if self.current_chat else "?"
                prompt_str = f"\n{BLUE}{BOLD}{_truncate(title, 30)} › You{RESET} "
                sys.stdout.write(prompt_str)
                sys.stdout.flush()
                raw = sys.stdin.readline()
                if not raw:
                    raise EOFError
                raw = raw.rstrip("\n")
            except (KeyboardInterrupt, EOFError):
                raise

            text = raw.strip()
            if not text:
                continue

            if text.startswith("/"):
                self._handle_slash(text)
                continue

            # Check for @path syntax (file attachment)
            resolved_text, files_content, image_paths = self._resolve_attachments(text)
            if files_content:
                text = files_content + "\n" + resolved_text
            elif resolved_text != text:
                text = resolved_text

            # Build display version (stored in history)
            display_content = text
            if image_paths:
                img_placeholders = [f"[Image: {p.name}]" for p in image_paths]
                display_content = "\n".join(img_placeholders + ([text] if text else []))

            # Normal message
            self._send_text(display_content, image_paths=image_paths if image_paths else None)

    def _send_text(self, display_content: str, image_paths: list[Path] | None = None):
        """Append a user message to the active chat and drive the generator.

        This is the REPL's normal send path, factored out so /task can feed a
        rendered template through the exact same flow instead of duplicating
        it.
        """
        if not display_content or not self.current_chat:
            return
        refs = []
        if image_paths:
            for path in image_paths:
                try:
                    refs.append(import_image(path, path.name, max_dimension=self.config.get("image_max_dimension", 4096), max_mb=self.config.get("image_max_mb", 4.5), quality=self.config.get("image_quality", 85)))
                except Exception as exc:
                    self.console.print(f"[yellow]Warning: could not import {path.name}: {exc}[/yellow]")
        self.current_chat["messages"].append({"role": "user", "content": display_content, **({"attachments": refs} if refs else {})})
        if self.current_chat["title"] == "New Chat":
            self.current_chat["title"] = _truncate(display_content, 50)

        messages = self._build_messages(self.current_chat, display_content,
                                        image_paths=image_paths)
        self._drive_generator(messages, self.current_chat)

    # ------------------------------------------------------------------
    # slash commands
    # ------------------------------------------------------------------

    def _handle_slash(self, text: str):
        """Parse and dispatch slash commands."""
        parts = shlex.split(text)
        cmd = parts[0].lower() if parts else ""
        args = parts[1:]

        if cmd in ("/quit", "/exit", "/q"):
            raise EOFError()

        elif cmd == "/help":
            self._cmd_help()

        elif cmd == "/new":
            self.current_chat = create_chat()
            self.console.print("[green]✓ New chat created.[/green]")

        elif cmd == "/show":
            self._cmd_show(args)

        elif cmd == "/tail":
            self._cmd_tail(args)

        elif cmd == "/rename":
            self._cmd_rename(args)

        elif cmd == "/clear":
            self._cmd_clear()

        elif cmd == "/export":
            self._cmd_export(args)

        elif cmd == "/yolo":
            self._cmd_yolo(args)

        elif cmd == "/config":
            self._cmd_config()

        elif cmd == "/model":
            self._cmd_model(args)

        elif cmd == "/models":
            self._cmd_models()

        elif cmd == "/list":
            self._cmd_list()

        elif cmd == "/load":
            self._cmd_load(args)

        elif cmd == "/baseurl":
            self._cmd_baseurl(args)

        elif cmd == "/apikey":
            self._cmd_apikey(args)

        elif cmd == "/llm-timeout":
            self._cmd_llm_timeout(args)

        elif cmd == "/timeout":
            self._cmd_timeout(args)

        elif cmd == "/download-max":
            self._cmd_download_max(args)

        elif cmd == "/agent":
            self._cmd_agent(args)

        elif cmd == "/context-keep":
            self._cmd_context_keep(args)

        elif cmd == "/system":
            self._cmd_system(args)

        elif cmd == "/delete":
            self._cmd_delete(args)

        elif cmd == "/attachments":
            self._cmd_attachments()

        elif cmd == "/attach":
            self._cmd_attach(args)

        elif cmd == "/compact":
            self._cmd_compact()

        elif cmd == "/redact":
            self._cmd_redact(args)

        elif cmd == "/tasks":
            self._cmd_tasks()

        elif cmd == "/task":
            self._cmd_task(args)

        elif cmd == "/about":
            self._cmd_about()

        else:
            self.console.print(f"[red]Unknown command:[/red] {cmd}  (try /help)")

    def _cmd_help(self):
        """Show available slash commands."""
        table = Table(title="Slash Commands", border_style="dim")
        table.add_column("Command", style="bold cyan", no_wrap=True)
        table.add_column("Description")

        table.add_row("/help", "Show this help")
        table.add_row("/new", "Start a new chat")
        table.add_row("/show [N]", "Show full conversation (optional: last N messages)")
        table.add_row("/tail [N]", "Show the last N messages (default 5)")
        table.add_row("/rename <title>", "Rename the current chat")
        table.add_row("/clear", "Clear the terminal screen")
        table.add_row("/export [path]", "Export current chat as Markdown")
        table.add_row("/config", "Show current configuration")
        table.add_row("/model <name>", "Change the model (e.g. /model gpt-4o)")
        table.add_row("/models", "Fetch available models from the endpoint")
        table.add_row("/baseurl <url>", "Set the API base URL (e.g. /baseurl http://localhost:11434/v1)")
        table.add_row("/apikey <key>", "Set the API key")
        table.add_row("/llm-timeout <sec>", "Set LLM API request timeout in seconds")
        table.add_row("/timeout <sec>", "Set tool execution timeout in seconds")
        table.add_row("/download-max <mb>", "Set default download size limit in MB (0 = no limit)")
        table.add_row("/agent <string>", "Set the user agent string")
        table.add_row("/context-keep <n>", "Set how many recent turns to keep when compacting")
        table.add_row("/yolo [all|safe|none]", "Set tool confirmation: all (YOLO), safe (read-only), none")
        table.add_row("/system [message...]", "Show or set the system message template")
        table.add_row("/compact", "Compact context by eliding old tool results")
        table.add_row("/redact [N]", "Delete the last N messages (default 1) — repeatable up to the top")
        table.add_row("/tasks", "List saved prompt-template Tasks")
        table.add_row("/task <#>", "Run a Task by its /tasks index, prompting for any %placeholders%")
        table.add_row("/list", "List recent chats")
        table.add_row("/load <index>", "Load a chat by its /list index")
        table.add_row("/delete <index>", "Delete a chat by its /list index")
        table.add_row("/attach", "Show file attachment help")
        table.add_row("/attachments", "Show durable attachment storage usage (read-only)")
        table.add_row("/about", "Show version, repo link, and license info")
        table.add_row("/quit, /exit", "Exit Pengy CLI")

        self.console.print(table)

    def _cmd_show(self, args: list[str]):
        """Show the current conversation with role labels and message numbers."""
        if not self.current_chat:
            self.console.print("[dim]No active chat.[/dim]")
            return
        msgs = self.current_chat.get("messages", [])
        if not msgs:
            self.console.print("[dim]No messages in this chat.[/dim]")
            return

        limit = None
        if args:
            try:
                limit = int(args[0])
            except ValueError:
                self.console.print("[red]Usage: /show [N]  — show last N messages[/red]")
                return

        display = msgs[-limit:] if limit else msgs

        self.console.print()
        self.console.print(
            f"[bold]Conversation:[/bold] {self.current_chat['title']} "
            f"[dim]({len(msgs)} messages total{', showing last ' + str(len(display)) if limit else ''})[/dim]"
        )
        self.console.print("[dim]" + "─" * 60 + "[/dim]")

        for i, msg in enumerate(display, 1 if not limit else len(msgs) - len(display) + 1):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        if p.get("type") == "text":
                            parts.append(p.get("text", ""))
                        elif p.get("type") == "image_url":
                            parts.append("[image]")
                    else:
                        parts.append(str(p))
                content = " ".join(parts)

            content = str(content) if content else ""

            if role == "user":
                self.console.print(f"[bold blue]#{i} You:[/bold blue] {_preview(content, 200)}")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                suffix = ""
                if tool_calls:
                    tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                    suffix = f" [dim](tool calls: {', '.join(tc_names)})[/dim]"
                self.console.print(f"[bold green]#{i} Assistant:[/bold green]{suffix}")
                if content:
                    self.console.print(f"[dim]  {_preview(content, 100)}[/dim]")
            elif role == "tool":
                tc_id = msg.get("tool_call_id", "?")[:8]
                self.console.print(f"[dim]#{i} Tool [{tc_id}]: {_preview(content, 80)}[/dim]")
            elif role == "system":
                self.console.print(f"[dim italic]#{i} System: {_preview(content, 100)}[/dim italic]")

        self.console.print("[dim]" + "─" * 60 + "[/dim]")

    def _cmd_tail(self, args: list[str]):
        """Show the last N messages (default 5)."""
        if not self.current_chat:
            self.console.print("[dim]No active chat.[/dim]")
            return
        try:
            n = int(args[0]) if args else 5
        except ValueError:
            self.console.print("[red]Usage: /tail [N]  — show last N messages (default 5)[/red]")
            return
        self._cmd_show([str(n)])

    def _cmd_rename(self, args: list[str]):
        """Rename the current chat."""
        if not self.current_chat:
            self.console.print("[dim]No active chat.[/dim]")
            return
        if not args:
            self.console.print("[dim]Usage: /rename <new title>[/dim]")
            return
        new_title = " ".join(args)
        old_title = self.current_chat.get("title", "?")
        self.current_chat["title"] = new_title
        save_chat(self.current_chat)
        self.console.print(f"[green]✓ Renamed:[/green] [bold]{old_title}[/bold] → [bold]{new_title}[/bold]")

    def _cmd_clear(self):
        """Clear the terminal screen."""
        self.console.clear()
        self.console.print("[dim]Screen cleared. Use /show to see conversation.[/dim]")

    def _cmd_export(self, args: list[str]):
        """Export the current chat to a Markdown file."""
        if not self.current_chat:
            self.console.print("[dim]No active chat.[/dim]")
            return

        if args:
            out_path = Path(args[0]).expanduser().resolve()
        else:
            safe_title = re.sub(r'[^a-zA-Z0-9 _-]', '', self.current_chat.get("title", "chat"))
            safe_title = safe_title.strip()[:50] or "chat"
            out_path = Path.home() / "Downloads" / f"{safe_title}.md"

        msgs = self.current_chat.get("messages", [])
        lines = []
        lines.append(f"# {self.current_chat.get('title', 'Chat')}")
        lines.append(f"*Exported {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        lines.append("")

        for msg in msgs:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif isinstance(p, dict) and p.get("type") == "image_url":
                        parts.append("[image]")
                content = " ".join(parts)
            content = str(content) if content else ""

            if role == "user":
                lines.append(f"### 🧑 You")
                for ref in msg.get("attachments", []) or []:
                    if isinstance(ref, dict):
                        lines.append(attachment_label(ref))
                lines.append(content)
                lines.append("")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    lines.append(f"### 🤖 Assistant (tool calls)")
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        lines.append(f"- **{fn.get('name', '?')}**")
                        try:
                            args_json = json.loads(fn.get("arguments", "{}"))
                            lines.append(f"  ```json\n  {json.dumps(args_json, indent=2)}\n  ```")
                        except Exception:
                            lines.append(f"  `{fn.get('arguments', '')}`")
                    lines.append("")
                if content:
                    lines.append(f"### 🤖 Assistant")
                    lines.append(content)
                    lines.append("")
            elif role == "tool":
                tc_id = msg.get("tool_call_id", "?")
                lines.append(f"#### 🔧 Tool result (`{tc_id}`)")
                lines.append("```")
                lines.append(content)
                lines.append("```")
                lines.append("")
            elif role == "system":
                lines.append(f"*System: {_truncate(content, 200)}*")
                lines.append("")

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(lines), encoding="utf-8")
            self.console.print(f"[green]✓ Exported to:[/green] [bold]{out_path}[/bold]")
        except Exception as e:
            self.console.print(f"[red]Error exporting:[/red] {e}")

    def _save_config(self):
        """Persist the current in-memory config to disk atomically."""
        save_config(self.config)

    def _cmd_yolo(self, args: list[str]):
        modes = ["all", "safe", "none"]
        current = self.config.get("tool_confirmation", "none")
        if args and args[0].lower() in modes:
            current = args[0].lower()
        else:
            # Cycle to next mode
            idx = modes.index(current) if current in modes else 2
            current = modes[(idx + 1) % len(modes)]

        self.config["tool_confirmation"] = current
        self._save_config()
        self.console.print(
            f"[green]✓ Tool Confirmation:[/green] [bold]{self._confirm_display()}[/bold]"
        )

    def _cmd_config(self):
        table = Table(title="Configuration", border_style="dim")
        table.add_column("Setting", style="bold cyan")
        table.add_column("Value")

        table.add_row("Base URL", self.config.get("base_url", "—"))
        table.add_row("Model", self.config.get("model", "—"))
        table.add_row("API Key", "••••" if self.config.get("api_key") else "(not set)")
        table.add_row("Tool Confirmation", self._confirm_display())
        table.add_row("Context keep turns", str(self.config.get("context_keep_turns", 0)))
        table.add_row("LLM Timeout", f"{self.config.get('llm_timeout', 300)}s")
        table.add_row("Tool Timeout", f"{self.config.get('tool_timeout', 300)}s")
        table.add_row("User Agent", self.config.get("user_agent", "—"))

        self.console.print(table)

    def _cmd_about(self):
        self.console.print(f"[bold]{edition_line('Python')}[/bold]")
        self.console.print(f"[link={GITHUB_URL}]{GITHUB_URL}[/link]")
        self.console.print(f"[link={WEBSITE_URL}]{WEBSITE_URL}[/link]")
        self.console.print()
        self.console.print(DESCRIPTION)
        self.console.print()
        self.console.print(f"{CATBEE_BLURB} [link={CATBEE_URL}]{CATBEE_URL}[/link]")
        self.console.print()
        self.console.print(copyright_line())
        self.console.print(f"[link={LICENSE_URL}]{LICENSE_NAME}[/link]")

    def _cmd_model(self, args: list[str]):
        if not args:
            self.console.print(f"[dim]Current model:[/dim] {self.config.get('model', '?')}")
            self.console.print("[dim]Usage: /model <name>[/dim]")
            return
        new_model = args[0]
        old_model = self.config.get("model", "")
        self.config["model"] = new_model
        self._save_config()
        self._update_llm_client()
        self.console.print(
            f"[green]✓ Model changed:[/green] {old_model} → [bold]{new_model}[/bold]"
        )

    def _cmd_models(self):
        """Fetch available models from the endpoint."""
        base_url = self.config.get("base_url", "").rstrip("/")
        api_key = self.config.get("api_key", "")
        models_url = f"{base_url}/models"

        self.console.print(f"[dim]Fetching models from {models_url}...[/dim]")
        try:
            req = urllib.request.Request(models_url)
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("api-key", api_key)
            req.add_header("User-Agent", self.config.get("user_agent", "PengyAgent/1.0"))
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            model_ids = sorted(
                m.get("id", "") for m in data.get("data", [])
                if m.get("id")
            )
            if not model_ids:
                self.console.print("[yellow]No models returned.[/yellow]")
                return

            current = self.config.get("model", "")
            table = Table(title="Available Models", border_style="dim")
            table.add_column("Model ID", style="bold cyan")
            for mid in model_ids:
                marker = " ← current" if mid == current else ""
                table.add_row(mid + marker)
            self.console.print(table)

        except urllib.error.HTTPError as e:
            self.console.print(
                f"[red]HTTP {e.code} from {models_url}.[/red] "
                f"Check your Base URL and API Key."
            )
        except Exception as e:
            self.console.print(f"[red]Error fetching models:[/red] {e}")

    def _cmd_list(self):
        chats = load_index()
        if not chats:
            self.console.print("[dim]No saved chats.[/dim]")
            return

        table = Table(title="Chat History", border_style="dim")
        table.add_column("#", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Msgs", justify="right")
        table.add_column("Preview")

        for i, chat in enumerate(chats, 1):
            is_current = (
                self.current_chat
                and chat["id"] == self.current_chat["id"]
            )
            prefix = "→ " if is_current else ""
            table.add_row(
                str(i),
                f"{prefix}{chat.get('title', 'Untitled')}",
                str(chat["msg_count"]),
                _truncate(chat["preview"], 50),
            )

        self.console.print(table)

    def _cmd_load(self, args: list[str]):
        if not args:
            self.console.print("[dim]Usage: /load <index>  (use /list to see indices)[/dim]")
            return
        try:
            idx = int(args[0]) - 1
        except ValueError:
            self.console.print("[red]Invalid index. Use /list to see available chats.[/red]")
            return

        chats = load_index()
        if idx < 0 or idx >= len(chats):
            self.console.print("[red]Index out of range.[/red]")
            return

        # Save current chat before switching
        if self.current_chat:
            save_chat(self.current_chat)

        loaded = get_chat(chats[idx]["id"])
        if not loaded:
            self.console.print("[red]Chat could not be loaded.[/red]")
            return
        self.current_chat = loaded
        msgs = self.current_chat.get("messages", [])
        msg_count = len(msgs)
        self.console.print(
            f"[green]✓ Loaded:[/green] [bold]{self.current_chat['title']}[/bold] "
            f"[dim]({msg_count} messages)[/dim]"
        )
        # Show last exchange for context
        self._cmd_show(["3"])

    def _cmd_delete(self, args: list[str]):
        if not args:
            self.console.print("[dim]Usage: /delete <index>  (use /list to see indices)[/dim]")
            return
        try:
            idx = int(args[0]) - 1
        except ValueError:
            self.console.print("[red]Invalid index.[/red]")
            return

        chats = load_index()
        if idx < 0 or idx >= len(chats):
            self.console.print("[red]Index out of range.[/red]")
            return

        target = chats[idx]
        title = target.get("title", "Untitled")

        # Deletion is immediate and unrecoverable; a mistyped index shouldn't
        # silently destroy a chat.
        if not Confirm.ask(
            f'Delete [bold]"{title}"[/bold]? This cannot be undone.', default=False
        ):
            self.console.print("[dim]Cancelled.[/dim]")
            return

        if self.current_chat and target["id"] == self.current_chat["id"]:
            self.current_chat = None

        delete_chat(target["id"])
        self.console.print(f"[green]✓ Deleted:[/green] [bold]{title}[/bold]")

        # If we just deleted the current chat, load the newest one
        if self.current_chat is None:
            remaining = load_index()
            if remaining:
                self.current_chat = get_chat(remaining[0]["id"])
                self.console.print(
                    f"[dim]Loaded:[/dim] [bold]{self.current_chat['title']}[/bold]"
                )
            else:
                self.current_chat = create_chat()
                self.console.print("[dim]New chat created.[/dim]")

    def _cmd_baseurl(self, args: list[str]):
        """Set the API base URL."""
        if not args:
            self.console.print(
                f"[dim]Current base URL:[/dim] {self.config.get('base_url', '?')}"
            )
            self.console.print("[dim]Usage: /baseurl <url> (e.g. /baseurl http://localhost:11434/v1)[/dim]")
            return
        new_url = args[0]
        old_url = self.config.get("base_url", "")
        self.config["base_url"] = new_url
        self._save_config()
        self._update_llm_client()
        self.console.print(
            f"[green]✓ Base URL changed:[/green] {old_url} → [bold]{new_url}[/bold]"
        )

    def _cmd_apikey(self, args: list[str]):
        """Set the API key."""
        if not args:
            current = self.config.get("api_key", "")
            masked = current[:4] + "••••" + current[-4:] if len(current) > 8 else "(not set)"
            self.console.print(f"[dim]Current API key:[/dim] {masked}")
            self.console.print("[dim]Usage: /apikey <key>[/dim]")
            return
        new_key = args[0]
        self.config["api_key"] = new_key
        self._save_config()
        self._update_llm_client()
        self.console.print(f"[green]✓ API key updated.[/green]")

    def _cmd_llm_timeout(self, args: list[str]):
        """Set the LLM API request timeout in seconds."""
        if not args:
            self.console.print(
                f"[dim]Current LLM timeout:[/dim] {self.config.get('llm_timeout', 300)}s"
            )
            self.console.print("[dim]Usage: /llm-timeout <seconds>[/dim]")
            return
        try:
            secs = int(args[0])
        except ValueError:
            self.console.print("[red]Invalid number. Usage: /llm-timeout <seconds>[/red]")
            return
        if secs < 1:
            self.console.print("[red]Timeout must be at least 1 second.[/red]")
            return
        old = self.config.get("llm_timeout", 300)
        self.config["llm_timeout"] = secs
        self._save_config()
        self._update_llm_client()
        self.console.print(
            f"[green]✓ LLM timeout changed:[/green] {old}s → [bold]{secs}s[/bold]"
        )

    def _cmd_timeout(self, args: list[str]):
        """Set the tool execution timeout in seconds."""
        if not args:
            self.console.print(
                f"[dim]Current tool timeout:[/dim] {self.config.get('tool_timeout', 300)}s"
            )
            self.console.print("[dim]Usage: /timeout <seconds>[/dim]")
            return
        try:
            secs = int(args[0])
        except ValueError:
            self.console.print("[red]Invalid number. Usage: /timeout <seconds>[/red]")
            return
        if secs < 1:
            self.console.print("[red]Timeout must be at least 1 second.[/red]")
            return
        old = self.config.get("tool_timeout", 300)
        self.config["tool_timeout"] = secs
        self._save_config()
        tools.set_tool_timeout(secs)
        self.console.print(
            f"[green]✓ Tool timeout changed:[/green] {old}s → [bold]{secs}s[/bold]"
        )

    def _cmd_download_max(self, args: list[str]):
        """Set the default download size limit in MB."""
        if not args:
            self.console.print(
                f"[dim]Current download max:[/dim] {self.config.get('download_max_mb', 100)} MB"
            )
            self.console.print("[dim]Usage: /download-max <mb> (0 = no limit)[/dim]")
            return
        try:
            mb = int(args[0])
        except ValueError:
            self.console.print("[red]Invalid number. Usage: /download-max <mb>[/red]")
            return
        if mb < 0:
            self.console.print("[red]Size must be 0 (no limit) or greater.[/red]")
            return
        old = self.config.get("download_max_mb", 100)
        self.config["download_max_mb"] = mb
        self._save_config()
        tools.set_download_max_mb(mb)
        self.console.print(
            f"[green]✓ Download max changed:[/green] {old} MB → [bold]{mb} MB[/bold]"
        )

    def _cmd_agent(self, args: list[str]):
        """Set the user agent string."""
        if not args:
            self.console.print(
                f"[dim]Current user agent:[/dim] {self.config.get('user_agent', '—')}"
            )
            self.console.print("[dim]Usage: /agent <string>[/dim]")
            return
        new_agent = args[0]
        old_agent = self.config.get("user_agent", "")
        self.config["user_agent"] = new_agent
        self._save_config()
        tools.set_user_agent(new_agent)
        self.console.print(
            f"[green]✓ User agent changed:[/green] {old_agent} → [bold]{new_agent}[/bold]"
        )

    def _cmd_context_keep(self, args: list[str]):
        """Set how many recent turns to keep when compacting context."""
        if not args:
            self.console.print(
                f"[dim]Current context keep turns:[/dim] {self.config.get('context_keep_turns', 0)}"
            )
            self.console.print("[dim]Usage: /context-keep <turns> (0 = keep all)[/dim]")
            return
        try:
            turns = int(args[0])
        except ValueError:
            self.console.print("[red]Invalid number. Usage: /context-keep <turns>[/red]")
            return
        if turns < 0:
            self.console.print("[red]Turns must be 0 or greater.[/red]")
            return
        old = self.config.get("context_keep_turns", 0)
        self.config["context_keep_turns"] = turns
        self._save_config()
        self.console.print(
            f"[green]✓ Context keep turns changed:[/green] {old} → [bold]{turns}[/bold]"
        )

    def _cmd_system(self, args: list[str]):
        """Show or set the system message template."""
        if args:
            # Set a new system message from the arguments
            new_template = " ".join(args)
            self.config["system_message"] = new_template
            self._save_config()
            rendered = render_system_message(new_template)
            self.console.print(
                f"[green]✓ System message updated.[/green]"
            )
            self.console.print(Panel(
                f"[bold]New template:[/bold]\n{new_template}\n\n[bold]Rendered:[/bold]\n{rendered}",
                title="System Message",
                border_style="green",
            ))
            return

        # No arguments — show current system message
        template = self.config.get("system_message", "")
        rendered = render_system_message(template) if template else "(no system message)"

        self.console.print(Panel(
            f"[bold]Template:[/bold]\n{template}\n\n[bold]Rendered (at send time):[/bold]\n{rendered}",
            title="System Message",
            border_style="dim",
        ))

    def _cmd_attachments(self):
        """Show durable attachment storage usage; never deletes objects."""
        from pengy.core.chat_manager import load_index, get_chat
        from pengy.core.attachments import storage_report
        report = storage_report([get_chat(item["id"]) for item in load_index() if get_chat(item["id"])])
        self.console.print_json(data=report)

    def _cmd_attach(self, args: list[str]):
        """Show how to use attachment; the @path syntax is demonstrated."""
        self.console.print(
            "[bold]File attachment:[/bold]\n"
            "  Use [cyan]@path/to/file[/cyan] anywhere in your message to attach a file.\n"
            "  Text files are injected as fenced code blocks.\n"
            "  Image files (.png, .jpg, .gif, .webp) are sent as vision input.\n"
            "  Example: [dim]What's in @screenshot.png?[/dim]\n"
            "  Example: [dim]Look at @src/main.py and fix the bug[/dim]"
        )

    def _cmd_compact(self):
        """Compact the current chat's context by eliding old tool results."""
        if not self.current_chat:
            self.console.print("[dim]No active chat.[/dim]")
            return
        turns = self.config.get("context_keep_turns", 3) or 3
        old_count = len(self.current_chat["messages"])
        self.current_chat["messages"] = elide_old_tool_results(
            self.current_chat["messages"], turns
        )
        new_count = len(self.current_chat["messages"])
        self.console.print(
            f"[green]✓ Compacted:[/green] elided tool results older than "
            f"{turns} turns. ({old_count} → {new_count} messages)"
        )
        save_chat(self.current_chat)

    def _cmd_redact(self, args: list[str]):
        """Delete the last N raw messages (default 1) from the current chat.

        This is the "undo the model's last step" button: repeatable all the
        way to an empty chat. It edits chats.json directly, so it wrecks
        prompt caching on most backends — that's the expected trade-off for
        pruning a wrong path out of context.
        """
        if not self.current_chat:
            self.console.print("[dim]No active chat.[/dim]")
            return
        try:
            n = int(args[0]) if args else 1
        except ValueError:
            self.console.print("[red]Usage: /redact [N]  — delete the last N messages (default 1)[/red]")
            return
        if n < 1:
            self.console.print("[red]N must be at least 1.[/red]")
            return

        messages = self.current_chat["messages"]
        if not messages:
            self.console.print("[dim]Chat is already empty.[/dim]")
            return

        before = len(messages)
        for _ in range(min(n, before)):
            messages = redact_last_message(messages)
        self.current_chat["messages"] = messages
        save_chat(self.current_chat)

        removed = before - len(messages)
        self.console.print(f"[green]✓ Redacted {removed} message(s).[/green] ({before} → {len(messages)})")
        if messages:
            self._cmd_show(["3"])

    def _cmd_tasks(self):
        """List saved prompt-template Tasks (shared with the GUI's Tasks dialog)."""
        tasks = task_manager.load_tasks()
        if not tasks:
            self.console.print(
                "[dim]No tasks defined yet. Create one in the GUI's Tasks dialog "
                "(or add one to tasks.json), then run it here with /task <#>.[/dim]"
            )
            return
        table = Table(title="Tasks", border_style="dim")
        table.add_column("#", style="bold cyan", no_wrap=True)
        table.add_column("Title")
        table.add_column("Template")
        for i, task in enumerate(tasks, 1):
            preview = (task.get("template") or "").replace("\n", " ").strip()
            if len(preview) > 60:
                preview = preview[:57] + "..."
            table.add_row(str(i), task.get("title", "Untitled Task"), preview)
        self.console.print(table)
        self.console.print("[dim]Run one with /task <#>[/dim]")

    def _cmd_task(self, args: list[str]):
        """Run a Task: fill in its %placeholders%, then send it like a normal message."""
        if not self.current_chat:
            self.console.print("[dim]No active chat.[/dim]")
            return
        if not args:
            self._cmd_tasks()
            return

        tasks = task_manager.load_tasks()
        try:
            index = int(args[0])
            if index < 1:
                raise ValueError
            task = tasks[index - 1]
        except (ValueError, IndexError):
            self.console.print("[red]Usage: /task <#>  (use /tasks to see indices)[/red]")
            return

        template = task.get("template", "") or ""
        placeholders = task_manager.extract_placeholders(template)
        values: dict[str, str] = {}
        for name in placeholders:
            values[name] = Prompt.ask(f"  [cyan]{name}[/cyan]")

        prompt = task_manager.render_template(template, values).strip()
        if not prompt:
            self.console.print("[yellow]This task produced an empty prompt.[/yellow]")
            return
        self._send_text(prompt)

    # ------------------------------------------------------------------
    # attachment helper
    # ------------------------------------------------------------------

    def _resolve_attachments(self, text: str) -> tuple[str, str, list[Path]]:
        """Scan *text* for @path references.

        Returns ``(cleaned_text, fenced_blocks, image_paths)``. A token that
        looks like a path (contains ``/`` or a known extension) but doesn't
        resolve prints a warning rather than silently staying literal — the
        user should know the attachment didn't happen.
        """
        resolved = text
        blocks = []
        image_paths = []
        for match in re.finditer(r'@(\S+)', text):
            raw = match.group(1)
            path_str = raw.rstrip(',;:.!?)]}')
            if not path_str:
                continue
            p = Path(path_str).expanduser().resolve()
            if p.exists() and p.is_file():
                if p.suffix.lower() in _IMAGE_EXTENSIONS:
                    image_paths.append(p)
                    resolved = resolved.replace(match.group(0), "", 1)
                elif _is_text_file(p):
                    blocks.append(_inject_file_content(p))
                    resolved = resolved.replace(match.group(0), "", 1)
            elif "/" in path_str or p.suffix.lower() in (_TEXT_EXTENSIONS | _IMAGE_EXTENSIONS):
                self.console.print(
                    f"[yellow]Warning: attachment not found:[/yellow] {match.group(0)}"
                )
        resolved = resolved.strip()
        return resolved, "\n\n".join(blocks), image_paths

    # ------------------------------------------------------------------
    # LLM interaction
    # ------------------------------------------------------------------

    # "none" is the *safest* mode — it confirms every call. Labelling it "None"
    # read as "no confirmations", which is exactly backwards.
    _CONFIRM_DISPLAY = {"all": "YOLO", "safe": "Safe", "none": "Confirm All"}

    def _confirm_display(self) -> str:
        return self._CONFIRM_DISPLAY.get(
            self.config.get("tool_confirmation", "none"), "Confirm All"
        )

    def _update_llm_client(self):
        """Recreate the LLM client from current config."""
        self.llm_client = LLMClient(
            base_url=self.config.get("base_url", "https://api.openai.com/v1"),
            api_key=self.config.get("api_key", ""),
            model=self.config.get("model", "gpt-4o"),
            llm_timeout=self.config.get("llm_timeout", 300),
        )
        tools.set_user_agent(self.config.get("user_agent", "PengyAgent/1.0"))
        tools.set_tool_timeout(self.config.get("tool_timeout", 300))
        tools.set_tool_output_max_chars(self.config.get("tool_output_max_chars", 250000))
        tools.set_download_max_mb(self.config.get("download_max_mb", 100))
        tools.set_image_limits(
            self.config.get("image_max_dimension", 4096),
            self.config.get("image_max_mb", 4.5),
            self.config.get("image_quality", 85),
        )

    def _build_messages(self, chat: dict, _current_text: str,
                         image_paths: list[Path] | None = None) -> list[dict]:
        """Build the message list for an API call from a chat session."""
        config = self.config
        system_msg = config.get("system_message", "")
        messages: list[dict] = []
        if system_msg:
            messages.append({
                "role": "system",
                "content": render_system_message(system_msg),
            })
        # Clean dangling tool calls + elide old tool results
        raw = list(chat.get("messages", []))
        raw = clean_dangling_tool_calls(raw)
        raw = elide_old_tool_results(raw, config.get("context_keep_turns", 0))
        messages.extend(resolve_history(raw, attachment_keep_turns=int(config.get("attachment_context_keep_turns", 4) or 0), max_dimension=config.get("image_max_dimension", 4096), max_mb=config.get("image_max_mb", 4.5), quality=config.get("image_quality", 85)))

        # If the last message is a user message with image attachments,
        # replace its content with the multimodal array.
        if image_paths:
            import base64
            last_user_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    last_user_idx = i
                    break
            if last_user_idx is not None:
                max_dim = config.get("image_max_dimension", 4096)
                max_mb = config.get("image_max_mb", 4.5)
                quality = config.get("image_quality", 85)
                parts = []
                for img_path in image_paths:
                    try:
                        img_bytes, mime = preprocess_image(
                            img_path,
                            max_dimension=max_dim, max_mb=max_mb, quality=quality,
                        )
                        b64 = base64.b64encode(img_bytes).decode()
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        })
                    except Exception:
                        pass
                # Extract the text from the display content
                display = messages[last_user_idx].get("content", "")
                if isinstance(display, str):
                    # Remove [Image: ...] placeholders to get plain text
                    text_part = display
                    for img_path in image_paths:
                        text_part = text_part.replace(f"[Image: {img_path.name}]", "")
                    text_part = text_part.strip()
                    if text_part:
                        parts.append({"type": "text", "text": text_part})
                messages[last_user_idx] = {"role": "user", "content": parts}

        return messages

    def _save_progress(self, chat: dict):
        """Persist mid-turn, so a crash can't take the turn's tool calls with it.

        One small per-chat file write; the whole store is not touched.
        """
        if self._no_save:
            return
        save_chat(chat)

    def _drive_generator(self, messages: list[dict], chat: dict):
        """Drive the LLMClient.chat() generator, handling tool confirmations.

        This is single-threaded — tool confirmation blocks on user input,
        which is fine for a CLI.
        """
        gen = self.llm_client.chat(
            messages,
            tool_confirmation=self.config.get("tool_confirmation", "none"),
            reasoning_effort=self.config.get("reasoning_effort", ""),
            preserve_reasoning=bool(self.config.get("preserve_reasoning", False)),
        )
        self._yolo_this_turn = False
        send_value = None

        expecting_api_call = True

        try:
            while True:
                if expecting_api_call:
                    self._show_thinking()

                response = gen.send(send_value) if send_value is not None else next(gen)
                send_value = None

                if expecting_api_call:
                    self._clear_thinking()

                rtype = response.get("type", "")

                if rtype == "final_response":
                    expecting_api_call = False
                    self._render_final(response, chat)

                elif rtype == "assistant_tool_calls":
                    expecting_api_call = False
                    self._yolo_this_turn = False
                    self._render_assistant_preamble(response["message"])
                    chat["messages"].append(response["message"])
                    self._save_progress(chat)

                elif rtype == "retrying":
                    # 429/529 backoff — surface it instead of hanging silently.
                    expecting_api_call = True
                    self.console.print(
                        "[yellow]Overloaded (HTTP {}) — retrying in {:.1f}s ({}/{})[/yellow]".format(
                            response.get("status_code", "?"),
                            response.get("delay_secs", 0),
                            response.get("attempt", 0),
                            response.get("max_attempts", 0),
                        )
                    )

                elif rtype == "tool_request":
                    expecting_api_call = False
                    self._render_tool_request(response)
                    confirm = self._get_tool_confirmation(response)
                    if confirm and confirm.get("abort_run"):
                        # Send a declined result to the generator and break
                        send_value = {"confirmed": False, "tool_call_id": response["tool_call_id"]}
                        try:
                            gen.send(send_value)
                        except StopIteration:
                            pass
                        break
                    send_value = confirm
                    if confirm and confirm.get("confirmed"):
                        if confirm.get("yolo_turn"):
                            self._yolo_this_turn = True

                elif rtype == "question_request":
                    expecting_api_call = False
                    answers = self._handle_question(response)
                    if answers is not None:
                        send_value = {"answered": True, "tool_call_id": response["tool_call_id"], "answers": answers}
                    else:
                        send_value = None

                elif rtype == "question_result":
                    expecting_api_call = True
                    self._render_tool_result({"content": response.get("content", ""), "declined": False})
                    # The generator already has this on its own message list;
                    # persist it too, or the assistant tool_calls message is
                    # left dangling in chat history.
                    chat["messages"].append({
                        "role": "tool",
                        "tool_call_id": response["tool_call_id"],
                        "content": response.get("content", ""),
                    })
                    self._save_progress(chat)

                elif rtype == "tool_result":
                    expecting_api_call = True
                    self._render_tool_result(response)
                    chat["messages"].append({
                        "role": "tool",
                        "tool_call_id": response["tool_call_id"],
                        "content": response["content"],
                    })
                    self._save_progress(chat)

                if rtype == "final_response":
                    break

        except StopIteration:
            pass
        except Exception as exc:
            self._clear_thinking()
            # escape(): the exception text may contain literal '[...]' (e.g. a
            # bracketed file path in a tool/compiler message) which rich would
            # otherwise parse as markup and crash with a MarkupError.
            self.console.print("\n[red]Error:[/red] " + escape(_sanitize_display(str(exc))))
        finally:
            gen.close()
            if not self._no_save:
                # Ctrl-C or an error leaves the loop mid-turn, where the last
                # assistant message can hold tool_calls with no result behind
                # them (the API 400s on that next request).
                chat["messages"] = clean_dangling_tool_calls(chat["messages"])
                save_chat(chat)

    def _show_thinking(self):
        """Print the 'Thinking…' indicator using raw ANSI.

        Uses \\r (carriage return) + \\033[K (clear-to-end-of-line)
        so the indicator disappears cleanly on the next write, without
        conflicting with readline's own terminal management.
        """
        sys.stdout.write("\r\033[K⏳ Thinking…")
        sys.stdout.flush()

    def _clear_thinking(self):
        """Clear the 'Thinking…' line."""
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # rendering helpers
    # ------------------------------------------------------------------

    def _handle_question(self, response: dict) -> list[str] | None:
        """Present questions to the user and collect answers."""
        questions = response.get("questions", [])
        if not questions:
            return []

        self.console.print()
        self.console.print(
            Panel(
                "[bold]The assistant needs your input:[/bold]",
                border_style="blue",
            )
        )

        answers = []
        for qi, q in enumerate(questions):
            header = q.get("header", f"Question {qi + 1}")
            question_text = q.get("question", "")
            options = q.get("options", [])

            self.console.print(f"\n[bold cyan]{header}[/bold cyan]")
            self.console.print(f"[dim]{question_text}[/dim]")
            self.console.print()

            for oi, opt in enumerate(options, 1):
                label = opt.get("label", "?")
                desc = opt.get("description", "")
                self.console.print(f"  [{oi}] {label}  [dim]— {desc}[/dim]")

            while True:
                try:
                    choice = Prompt.ask(
                        f"  Choose [1-{len(options)}]",
                        default="1",
                    ).strip()
                    idx = int(choice) - 1
                    if 0 <= idx < len(options):
                        answers.append(options[idx]["label"])
                        break
                    self.console.print(f"[red]Please enter a number between 1 and {len(options)}.[/red]")
                except ValueError:
                    self.console.print(f"[red]Please enter a number.[/red]")
                except (KeyboardInterrupt, EOFError):
                    self.console.print("\n[red]Cancelled.[/red]")
                    return None

        self.console.print("[green]\u2713 Answers recorded.[/green]")
        return answers


    def _render_final(self, response: dict, chat: dict):
        """Render the final assistant response and show token usage."""
        content = response.get("content") or ""
        chat["messages"].append(response.get("message") or {"role": "assistant", "content": content})

        # Handle non-pretty output modes
        if self._output_mode == "silent":
            return

        if self._output_mode == "json":
            result = {
                "content": content,
                "usage": response.get("usage", {}),
            }
            self.console.print_json(data=result)
            return

        if self._output_mode == "raw":
            if content.strip():
                print(content)
            return

        # Pretty (default) — rich markdown rendering
        if content.strip():
            self.console.print()
            self.console.print(
                Panel(
                    Markdown(content),
                    title="Assistant 🤖",
                    title_align="left",
                    border_style="green",
                )
            )
        else:
            self.console.print("[dim](empty response)[/dim]")

        # Show token usage: this turn, and the chat's running total
        usage = response.get("usage", {})
        totals = add_usage(chat, usage)
        if usage:
            prompt = usage.get("prompt_tokens", 0)
            completion = usage.get("completion_tokens", 0)
            self.console.print(
                f"[dim]Tokens: {prompt:,} in / {completion:,} out "
                f"({prompt + completion:,} total this turn, "
                f"{totals['total_tokens']:,} total this chat)[/dim]"
            )

    def _render_assistant_preamble(self, message: dict):
        """Show the narration the model wrote alongside its tool calls.

        It is persisted with the turn and shows up on a later ``/show``, so a
        live run that skipped it looked like the model went straight to the
        tools with nothing to say.  ``json`` mode stays silent: its output is a
        single object built from the final response.
        """
        content = (message.get("content") or "").strip()
        if not content or self._output_mode in ("silent", "json"):
            return

        if self._output_mode == "raw":
            print(content)
            return

        self.console.print()
        self.console.print(
            Panel(
                Markdown(content),
                title="Assistant 🤖",
                title_align="left",
                border_style="green",
            )
        )

    def _render_tool_request(self, response: dict):
        """Show the tool call that the model wants to make."""
        name = response.get("name", "?")
        args = response.get("args", {})
        args_preview = ", ".join(f"{k}={v!r}" for k, v in args.items())
        if len(args_preview) > 60:
            args_preview = args_preview[:59] + "…"

        args_json = json.dumps(args, indent=2)
        if len(args_json) > 4000:
            args_json = args_json[:4000] + "\n\n[... truncated ...]"

        self.console.print()
        self.console.print(
            Panel(
                f"[bold]{escape(_sanitize_display(name))}[/bold]\n{escape(_sanitize_display(args_json))}",
                title=f"🔧 Tool: {escape(_sanitize_display(name))} [{escape(_sanitize_display(args_preview))}]",
                title_align="left",
                border_style="yellow",
            )
        )

    def _render_tool_result(self, response: dict):
        """Show the result of a tool execution."""
        content = response.get("content", "")
        declined = response.get("declined", False)

        if declined:
            self.console.print(f"[red]✗ Declined[/red]")
            return

        # Truncate very long results for display
        display = content
        if len(display) > 2000:
            display = display[:2000] + "\n\n[... truncated ...]"

        title = "Tool output"
        self.console.print(
            # Wrap in Text so raw tool output is rendered literally, not
            # parsed as rich markup (which silently strips [bracketed] text).
            Panel(Text(_sanitize_display(display)), title=title, title_align="left", border_style="dim")
        )

    # ------------------------------------------------------------------
    # user interaction
    # ------------------------------------------------------------------

    def _get_tool_confirmation(self, response: dict) -> dict | None:
        """Ask the user to confirm or decline a tool call.

        Returns a dict to send into the generator, or None.
        """
        # Must mirror llm_client.chat()'s skip_confirm logic exactly. When the
        # core auto-approves it ignores the value we send back, so prompting
        # here would be cosmetic — declining would not stop the tool running.
        tc = self.config.get("tool_confirmation")
        if (tc == "all" or self._yolo_this_turn
                or (tc == "safe" and tools.is_readonly_tool(response.get("name", "")))):
            return {"confirmed": True, "tool_call_id": response["tool_call_id"]}

        while True:
            try:
                choice = Prompt.ask(
                    f"  [1] Execute  [2] Yes to all this turn  [3] Decline  [4] Abort run  [bold][1/2/3/4][/bold]",
                    default="1",
                ).strip()
            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[red]Run aborted.[/red]")
                return {"confirmed": False, "tool_call_id": response["tool_call_id"], "abort_run": True}

            if choice == "1":
                return {"confirmed": True, "tool_call_id": response["tool_call_id"]}
            elif choice == "2":
                return {"confirmed": True, "yolo_turn": True, "tool_call_id": response["tool_call_id"]}
            elif choice == "3":
                return None
            elif choice == "4":
                self.console.print("[red]Run aborted by user.[/red]")
                return {"confirmed": False, "tool_call_id": response["tool_call_id"], "abort_run": True}
            else:
                self.console.print("[red]Please enter 1, 2, 3, or 4.[/red]")

    def _get_sudo_password(self) -> str | None:
        """Prompt for sudo password in the terminal."""
        try:
            password = Prompt.ask(
                "[yellow]Enter sudo password[/yellow]",
                password=True,
            )
            return password if password else None
        except (KeyboardInterrupt, EOFError):
            return None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pengy CLI — Chat with LLMs from the command line",
        epilog="Use -- to treat all remaining arguments as prompt text.",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="Optional prompt for single-shot mode. If omitted, starts interactive mode.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Don't persist single-shot chats to history.",
    )
    parser.add_argument(
        "-v", "--version",
        action="store_true",
        help="Show version information and exit.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model to use (overrides config).",
    )
    parser.add_argument(
        "--system",
        type=str,
        default=None,
        help="System message template (overrides config).",
    )
    parser.add_argument(
        "--output",
        type=str,
        choices=["pretty", "raw", "json", "silent"],
        default=None,
        help="Output format for single-shot mode (default: pretty).",
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help="Use a custom config directory instead of ~/.config/pengy.",
    )
    args = parser.parse_args()

    if args.version:
        _print_version()
        return

    # Apply config directory override as early as possible
    if args.config_dir:
        from pengy.core.config import set_config_dir
        set_config_dir(args.config_dir)

    cli = PengyCLI(no_save=args.no_save)

    # In-memory config overrides from CLI flags — never persisted, so a
    # one-shot `--model`/`--system` doesn't rewrite the shared settings.json
    # used by the GUI, web, and the other Pengy editions.
    if args.model:
        cli.config["model"] = args.model
    if args.system:
        cli.config["system_message"] = args.system
    if args.model or args.system:
        cli._update_llm_client()

    if args.prompt:
        prompt_text = " ".join(args.prompt)
        if args.output:
            cli._output_mode = args.output
        cli.run_single_shot(prompt_text)
    else:
        cli.run_interactive()


if __name__ == "__main__":
    main()
