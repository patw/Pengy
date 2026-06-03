"""Pengy CLI — command-line interface for Pengy.

Interactive mode:  pengy-cli
Single-shot mode:  pengy-cli "What is the capital of France?"
"""

import argparse
import json
import shlex
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from pengy.core import tools
from pengy.core.chat_manager import (
    create_chat, delete_chat, get_chat, load_chats, save_chat,
)
from pengy.core.config import load_config, render_system_message
from pengy.core.llm_client import LLMClient


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = 72) -> str:
    """Truncate *text* to *max_len* characters, adding an ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class PengyCLI:
    """Command-line interface for Pengy."""

    def __init__(self):
        self.config = load_config()
        self.console = Console()
        self.llm_client: LLMClient | None = None
        self.current_chat: dict | None = None
        self._update_llm_client()

        # "yes to all this turn" — resets each time the LLM returns a fresh
        # set of tool calls (i.e. each API round-trip).
        self._yolo_this_turn = False

    # ------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------

    def run_interactive(self):
        """Start the interactive REPL."""
        self.console.print()
        self.console.print(
            Panel.fit(
                "[bold]🐧 Pengy CLI[/bold]\n"
                "Type your message and press Enter.  [dim]Try /help for available commands.[/dim]",
                border_style="blue",
            )
        )

        # Load the most recent chat or create a new one.
        chats = load_chats()
        if chats:
            self.current_chat = chats[0]
            self.console.print(
                f"[dim]Resumed chat:[/dim] [bold]{self.current_chat['title']}[/bold]"
            )
        else:
            self.current_chat = create_chat()
            self.console.print("[dim]New chat created.[/dim]")

        self.console.print(
            f"[dim]Model: {self.config.get('model', '?')}  "
            f"YOLO: {'ON' if self.config.get('yolo_mode') else 'OFF'}[/dim]"
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
        try:
            # Create a throw-away chat (we still save it so the user can
            # find it later in the desktop app or via /list, but we don't
            # advertise it).
            chat = create_chat()
            chat["title"] = _truncate(prompt_text, 50)
            chat["messages"].append({"role": "user", "content": prompt_text})
            save_chat(chat)

            messages = self._build_messages(chat, prompt_text)
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
                raw = Prompt.ask("\n[bold blue]You[/bold blue]")
            except (KeyboardInterrupt, EOFError):
                raise

            text = raw.strip()
            if not text:
                continue

            if text.startswith("/"):
                self._handle_slash(text)
                continue

            # Normal message
            self.current_chat["messages"].append({"role": "user", "content": text})
            if self.current_chat["title"] == "New Chat":
                self.current_chat["title"] = _truncate(text, 50)

            messages = self._build_messages(self.current_chat, text)
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

        elif cmd == "/yolo":
            self._cmd_yolo(args)

        elif cmd == "/config":
            self._cmd_config()

        elif cmd == "/model":
            self._cmd_model(args)

        elif cmd == "/list":
            self._cmd_list()

        elif cmd == "/load":
            self._cmd_load(args)

        elif cmd == "/system":
            self._cmd_system()

        elif cmd == "/delete":
            self._cmd_delete(args)

        else:
            self.console.print(f"[red]Unknown command:[/red] {cmd}  (try /help)")

    def _cmd_help(self):
        """Show available slash commands."""
        table = Table(title="Slash Commands", border_style="dim")
        table.add_column("Command", style="bold cyan", no_wrap=True)
        table.add_column("Description")

        table.add_row("/help", "Show this help")
        table.add_row("/new", "Start a new chat")
        table.add_row("/yolo [on|off]", "Toggle YOLO mode (or set explicitly)")
        table.add_row("/config", "Show current configuration")
        table.add_row("/model <name>", "Change the model (e.g. /model gpt-4o)")
        table.add_row("/list", "List recent chats")
        table.add_row("/load <index>", "Load a chat by its /list index")
        table.add_row("/delete <index>", "Delete a chat by its /list index")
        table.add_row("/system", "Show the system message")
        table.add_row("/quit, /exit", "Exit Pengy CLI")

        self.console.print(table)

    def _cmd_yolo(self, args: list[str]):
        if args and args[0].lower() == "on":
            self.config["yolo_mode"] = True
        elif args and args[0].lower() == "off":
            self.config["yolo_mode"] = False
        else:
            self.config["yolo_mode"] = not self.config["yolo_mode"]

        status = "ON" if self.config["yolo_mode"] else "OFF"
        self.console.print(f"[green]✓ YOLO mode:[/green] [bold]{status}[/bold]")

    def _cmd_config(self):
        table = Table(title="Configuration", border_style="dim")
        table.add_column("Setting", style="bold cyan")
        table.add_column("Value")

        table.add_row("Base URL", self.config.get("base_url", "—"))
        table.add_row("Model", self.config.get("model", "—"))
        table.add_row("API Key", "••••" if self.config.get("api_key") else "(not set)")
        table.add_row("YOLO Mode", "ON" if self.config.get("yolo_mode") else "OFF")
        table.add_row("Tool Timeout", f"{self.config.get('tool_timeout', 60)}s")
        table.add_row("User Agent", self.config.get("user_agent", "—"))

        self.console.print(table)

    def _cmd_model(self, args: list[str]):
        if not args:
            self.console.print(f"[dim]Current model:[/dim] {self.config.get('model', '?')}")
            self.console.print("[dim]Usage: /model <name>[/dim]")
            return
        new_model = args[0]
        old_model = self.config.get("model", "")
        self.config["model"] = new_model
        self._update_llm_client()
        self.console.print(
            f"[green]✓ Model changed:[/green] {old_model} → [bold]{new_model}[/bold]"
        )

    def _cmd_list(self):
        chats = load_chats()
        if not chats:
            self.console.print("[dim]No saved chats.[/dim]")
            return

        table = Table(title="Chat History", border_style="dim")
        table.add_column("#", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Messages", justify="right")
        table.add_column("Created")

        for i, chat in enumerate(chats, 1):
            is_current = (
                self.current_chat
                and chat["id"] == self.current_chat["id"]
            )
            prefix = "→ " if is_current else ""
            msg_count = len(chat.get("messages", []))
            created = chat.get("created_at", "?")[:16].replace("T", " ")
            table.add_row(
                str(i),
                f"{prefix}{chat.get('title', 'Untitled')}",
                str(msg_count),
                created,
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

        chats = load_chats()
        if idx < 0 or idx >= len(chats):
            self.console.print("[red]Index out of range.[/red]")
            return

        # Save current chat before switching
        if self.current_chat:
            save_chat(self.current_chat)

        self.current_chat = chats[idx]
        self.console.print(
            f"[green]✓ Loaded:[/green] [bold]{self.current_chat['title']}[/bold] "
            f"({len(self.current_chat.get('messages', []))} messages)"
        )

    def _cmd_delete(self, args: list[str]):
        if not args:
            self.console.print("[dim]Usage: /delete <index>  (use /list to see indices)[/dim]")
            return
        try:
            idx = int(args[0]) - 1
        except ValueError:
            self.console.print("[red]Invalid index.[/red]")
            return

        chats = load_chats()
        if idx < 0 or idx >= len(chats):
            self.console.print("[red]Index out of range.[/red]")
            return

        target = chats[idx]
        title = target.get("title", "Untitled")

        if self.current_chat and target["id"] == self.current_chat["id"]:
            self.current_chat = None

        delete_chat(target["id"])
        self.console.print(f"[green]✓ Deleted:[/green] [bold]{title}[/bold]")

        # If we just deleted the current chat, load the newest one
        if self.current_chat is None:
            remaining = load_chats()
            if remaining:
                self.current_chat = remaining[0]
                self.console.print(
                    f"[dim]Loaded:[/dim] [bold]{self.current_chat['title']}[/bold]"
                )
            else:
                self.current_chat = create_chat()
                self.console.print("[dim]New chat created.[/dim]")

    def _cmd_system(self):
        template = self.config.get("system_message", "")
        rendered = render_system_message(template) if template else "(no system message)"

        self.console.print(Panel(
            f"[bold]Template:[/bold]\n{template}\n\n[bold]Rendered (at send time):[/bold]\n{rendered}",
            title="System Message",
            border_style="dim",
        ))

    # ------------------------------------------------------------------
    # LLM interaction
    # ------------------------------------------------------------------

    def _update_llm_client(self):
        """Recreate the LLM client from current config."""
        self.llm_client = LLMClient(
            base_url=self.config.get("base_url", "https://api.openai.com/v1"),
            api_key=self.config.get("api_key", ""),
            model=self.config.get("model", "gpt-4o"),
        )
        tools.set_user_agent(self.config.get("user_agent", "PengyAgent/1.0"))
        tools.set_tool_timeout(self.config.get("tool_timeout", 60))

    @staticmethod
    def _build_messages(chat: dict, _current_text: str) -> list[dict]:
        """Build the message list for an API call from a chat session."""
        messages: list[dict] = []
        config = load_config()
        system_msg = config.get("system_message", "")
        if system_msg:
            messages.append({
                "role": "system",
                "content": render_system_message(system_msg),
            })
        messages.extend(chat.get("messages", []))
        return messages

    def _drive_generator(self, messages: list[dict], chat: dict):
        """Drive the LLMClient.chat() generator, handling tool confirmations.

        This is single-threaded — tool confirmation blocks on user input,
        which is fine for a CLI.
        """
        yolo = self.config.get("yolo_mode", False)
        gen = self.llm_client.chat(messages, yolo_mode=yolo)
        self._yolo_this_turn = False
        send_value = None

        # Only certain generator transitions actually call the API (and thus
        # block waiting for the network).  We show "Thinking…" only then.
        #
        #   next(gen)  at start or after tool_result  →  API call
        #   next(gen)  after assistant_tool_calls     →  yields tool_request (instant)
        #   gen.send() after tool_request             →  executes tool locally (instant)
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
                    expecting_api_call = False   # next yield is tool_request, instant
                    self._yolo_this_turn = False
                    chat["messages"].append(response["message"])

                elif rtype == "tool_request":
                    expecting_api_call = False   # gen.send() runs tool locally
                    self._render_tool_request(response)
                    confirm = self._get_tool_confirmation(response)
                    send_value = confirm
                    if confirm and confirm.get("confirmed"):
                        if confirm.get("yolo_turn"):
                            self._yolo_this_turn = True

                elif rtype == "tool_result":
                    expecting_api_call = True    # next next() hits the API
                    self._render_tool_result(response)
                    chat["messages"].append({
                        "role": "tool",
                        "tool_call_id": response["tool_call_id"],
                        "content": response["content"],
                    })

                if rtype == "final_response":
                    break

        except StopIteration:
            pass
        except Exception as exc:
            self._clear_thinking()
            self.console.print(f"\n[red]Error:[/red] {exc}")
        finally:
            gen.close()
            save_chat(chat)

    def _show_thinking(self):
        """Print the 'Thinking…' indicator (non-newline, so the cursor sits after it)."""
        self.console.print("[dim]⏳ Thinking…[/dim]", end="\r")

    def _clear_thinking(self):
        """Clear the 'Thinking…' line by overwriting with spaces."""
        self.console.print(" " * 40, end="\r")

    # ------------------------------------------------------------------
    # rendering helpers
    # ------------------------------------------------------------------

    def _render_final(self, response: dict, chat: dict):
        """Render the final assistant response and save it."""
        content = response.get("content") or ""
        chat["messages"].append({"role": "assistant", "content": content})

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

    def _render_tool_request(self, response: dict):
        """Show the tool call that the model wants to make."""
        name = response.get("name", "?")
        args = response.get("args", {})
        args_preview = ", ".join(f"{k}={v!r}" for k, v in args.items())
        if len(args_preview) > 60:
            args_preview = args_preview[:59] + "…"

        self.console.print()
        self.console.print(
            Panel(
                f"[bold]{name}[/bold]\n{json.dumps(args, indent=2)}",
                title=f"🔧 Tool: {name} [{args_preview}]",
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
        border = "dim" if not declined else "red"
        self.console.print(
            Panel(display, title=title, title_align="left", border_style=border)
        )

    # ------------------------------------------------------------------
    # user interaction
    # ------------------------------------------------------------------

    def _get_tool_confirmation(self, response: dict) -> dict | None:
        """Ask the user to confirm or decline a tool call.

        Returns a dict to send into the generator, or None.
        """
        name = response.get("name", "?")
        yolo_mode = self.config.get("yolo_mode", False)

        if yolo_mode or self._yolo_this_turn:
            return {"confirmed": True, "tool_call_id": response["tool_call_id"]}

        while True:
            choice = Prompt.ask(
                f"  [1] Execute  [2] Yes to all this turn  [3] Decline  [bold][1/2/3][/bold]",
                default="1",
            ).strip()

            if choice == "1":
                return {"confirmed": True, "tool_call_id": response["tool_call_id"]}
            elif choice == "2":
                return {"confirmed": True, "yolo_turn": True, "tool_call_id": response["tool_call_id"]}
            elif choice == "3":
                return None
            else:
                self.console.print("[red]Please enter 1, 2, or 3.[/red]")

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
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="Optional prompt for single-shot mode. If omitted, starts interactive mode.",
    )
    args = parser.parse_args()

    cli = PengyCLI()

    if args.prompt:
        prompt_text = " ".join(args.prompt)
        cli.run_single_shot(prompt_text)
    else:
        cli.run_interactive()


if __name__ == "__main__":
    main()
