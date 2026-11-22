"""The interactive CLI's input layer (prompt_toolkit), with a headless fallback.

``pengy-cli`` is two products in one binary. Interactive, it is a REPL a person
types into; the rest of the time it is a subagent -- cron, a systemd timer, the Qt
app shelling out, ``--output json`` -- and nothing about that path may change.
So this module is loaded on demand and every entry point checks
:func:`available` first: when stdin/stdout is not a terminal (or prompt_toolkit is
missing) the CLI keeps its old ``sys.stdin.readline()`` behaviour exactly.

What the interactive path gains over the hand-rolled prompt: line editing,
persistent history, slash-command completion, and the queue of messages typed
while a turn ran -- shown in a bottom toolbar with Up/Down recall, so the CLI's
queued input looks like the desktop and web composers rather than a list.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pengy.cli import typing

# Slash commands offered before the first word; the REPL owns the real dispatch
# list, this is only for completion, so a missing entry degrades completion, not
# behaviour.
_COMMANDS = (
    "/help", "/new", "/show", "/tail", "/rename", "/title", "/clear", "/export",
    "/yolo", "/config", "/model", "/models", "/list", "/load", "/search", "/baseurl",
    "/apikey", "/llm-timeout", "/timeout", "/download-max", "/agent", "/context-keep",
    "/auto-compact", "/system", "/delete", "/attach", "/attachments", "/compact",
    "/redact", "/tasks", "/task", "/queue", "/about", "/quit", "/exit", "/q",
)

_HISTORY_PATH = Path(
    os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
) / "pengy" / "cli_history"


def available() -> bool:
    """True when an interactive editor is usable and safe to start."""
    if not (os.isatty(0) and os.isatty(1)):
        return False
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    try:
        import prompt_toolkit  # noqa: F401
    except Exception:
        return False
    return True


def _disable_cpr(session) -> None:
    """Tell the renderer not to ask the terminal for its cursor position.

    prompt_toolkit queries CPR (`ESC [ 6 n`) after the first render and waits up
    to two seconds for the answer, reading stdin while it waits -- which eats
    keystrokes meant for the turn's queue. Every terminal that does not answer
    lands in this state anyway, so asking for it up front costs nothing and makes
    the handoff to the capture thread deterministic.
    """
    try:
        from prompt_toolkit.renderer import CPR_Support

        session.app.renderer.cpr_support = CPR_Support.NOT_SUPPORTED
    except Exception:
        pass


class PromptIO:
    """Owns the REPL's editor session and the plain prompts used mid-turn."""

    def __init__(self) -> None:
        self._session = None
        self._plain = None
        self._enabled = available()

    @property
    def enabled(self) -> bool:
        """Whether the interactive editor is usable (False = legacy prompt)."""
        return self._enabled

    # ── REPL line ─────────────────────────────────────────────────
    def read_command(self, title: str) -> str | None:
        """One REPL line. ``None`` means EOF (Ctrl-D)."""
        if not self._enabled:
            return None
        self._show_queue()
        # Fresh session per line: prompt_toolkit's input object buffers keys and
        # its reader can still be draining the pty as the prompt returns, which
        # steals keystrokes meant for the next turn's queue. A new session per
        # prompt leaves nothing behind, and the history object is shared.
        self._session = None
        session = self._repl_session()
        try:
            return session.prompt(message=lambda: self._message(title))
        except EOFError:
            return None

    # ── Mid-turn prompts ──────────────────────────────────────────
    def ask(self, message: str, *, password: bool = False) -> str:
        """A blocking prompt raised *inside* a turn (confirm/question/sudo).

        Capture is suspended for the duration: a message typed while the turn ran
        must never be read as the answer to this. Falls back to ``input()`` when
        there is no editor, which is what the CLI used before.
        """
        was_active = typing.suspend()
        try:
            if not self._enabled:
                return input(message)
            import prompt_toolkit

            if self._plain is None:
                self._plain = prompt_toolkit.PromptSession()
            return self._plain.prompt(message, is_password=password)
        finally:
            typing.resume(was_active)

    def ask_choice(self, message: str, count: int) -> int | None:
        """A 1..count choice. Blank or out-of-range re-asks; None = cancelled."""
        while True:
            try:
                raw = self.ask(message).strip()
            except (KeyboardInterrupt, EOFError):
                return None
            if not raw:
                # Blank must never mean "option 1": that is how a stray Enter
                # answered a question the model asked.
                continue
            try:
                value = int(raw)
            except ValueError:
                continue
            if 1 <= value <= count:
                return value

    # ── internals ─────────────────────────────────────────────────
    def _message(self, title: str) -> str:
        short = title[:30]
        queue = typing.queue()
        note = ""
        if len(queue):
            note = f" ({len(queue)} queued{', held' if queue.held else ''})"
        return f"\n{short} › You{note}: "

    def _show_queue(self) -> None:
        """Print the queue above the prompt.

        Deliberately not a prompt_toolkit bottom toolbar: the toolbar needs the
        cursor position, which is exactly the CPR query that was eating keystrokes
        (see _disable_cpr), so it does not render at all with CPR disabled. Plain
        scrolling text also survives in the terminal's scrollback; a toolbar does
        not.
        """
        queue = typing.queue()
        if queue.is_empty():
            return
        hint = (
            "held after Ctrl-C — Enter to send"
            if queue.held
            else "sends when this turn ends — ↑ edit newest · ↓ edit oldest"
        )
        lines = [f"\u23f3 Queued ({len(queue)}) · {hint}"]
        for i, preview in enumerate(queue.previews(70), 1):
            lines.append(f"  {i}. {preview}")
        sys.stdout.write("\x1b[2m" + "\n".join(lines) + "\x1b[0m\n")
        sys.stdout.flush()

    def _recall(self, direction: int) -> str:
        # Down with a non-empty queue takes the oldest, Up the newest: the same
        # order as the desktop and web composers.
        return typing.take_newest() if direction < 0 else typing.take_oldest()

    def _repl_session(self):
        if self._session is not None:
            return self._session

        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        class _SlashCompleter(Completer):
            """Completes the command word; only when the line starts with "/"."""

            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                prefix = text.split()[0] if text.split() else text
                for cmd in _COMMANDS:
                    if cmd.startswith(prefix):
                        yield Completion(cmd, start_position=-len(prefix))

        def empty_line() -> bool:
            session = self._session
            return bool(session) and not session.default_buffer.text and not typing.queue().is_empty()

        bindings = KeyBindings()

        @bindings.add("up", filter=Condition(empty_line))
        def _up(event):
            text = self._recall(-1)
            if text:
                event.current_buffer.insert_text(text)

        @bindings.add("down", filter=Condition(empty_line))
        def _down(event):
            text = self._recall(1)
            if text:
                event.current_buffer.insert_text(text)

        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._session = PromptSession(
            history=FileHistory(str(_HISTORY_PATH)),
            completer=_SlashCompleter(),
            complete_while_typing=False,
            key_bindings=bindings,
        )
        _disable_cpr(self._session)
        return self._session
