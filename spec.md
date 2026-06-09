# Pengy — Application Specification

## Overview

Pengy is a local-first AI agent application that connects to any OpenAI-compatible LLM API and gives the model a set of tools to operate on the user's machine. It provides two interfaces backed by the same core:

| Interface | Entry Point | Description |
|-----------|------------|-------------|
| **Desktop GUI** | `pengy/main.py` | Qt6 three-pane chat with markdown rendering, file attachments, multi-session sidebar |
| **CLI** | `pengy/cli/main.py` | Terminal REPL with slash commands or single-shot mode for scripting |

Both write to the same `~/.config/pengy/` directory — settings and chat history are shared.

---

## Technology Stack

- **Language:** Python 3.10+
- **GUI Framework:** PySide6 (LGPL)
- **CLI Framework:** `rich` (tables, panels, markdown rendering, prompts)
- **LLM Client:** `openai` Python SDK (non-streaming)
- **Markdown Rendering:** `markdown` + `pygments` (syntax highlighting, desktop); `rich.markdown` (CLI)
- **Web Search:** `ddgs` (DuckDuckGo)
- **Storage:** JSON files in `~/.config/pengy/`

---

## Architecture

```
pengy/
├── main.py                    # Desktop GUI entry point
├── assets/
│   └── icon.svg               # Penguin app icon (SVG)
├── cli/
│   ├── __init__.py            # Package marker
│   └── main.py                # CLI entry point — PengyCLI class (interactive + single-shot)
├── core/
│   ├── config.py              # Settings load/save + render_system_message()
│   ├── chat_manager.py        # Chat session CRUD
│   ├── llm_client.py          # OpenAI-compatible API client (generator protocol)
│   └── tools.py               # Tool schemas (TOOLS), execution (execute_tool), sudo provider
└── ui/
    ├── main_window.py         # 3-pane main window; wires all signals
    ├── chat_history.py        # Left sidebar — chat list + quick settings panel
    ├── chat_view.py           # Right-top — markdown chat renderer (QTextBrowser)
    ├── chat_input.py          # Right-bottom — input field + attachment button
    ├── chat_worker.py         # QThread worker driving the LLM generator
    └── settings_dialog.py     # Settings modal dialog
```

### Core Package (`pengy/core/`)

Pure Python — no Qt or terminal dependencies. Shared by both GUI and CLI.

| Module | Responsibility |
|--------|---------------|
| `config.py` | Load/save `~/.config/pengy/settings.json` with default merging; `render_system_message()` fills `{date}`, `{username}`, `{hostname}`, `{osinfo}` placeholders at call time |
| `chat_manager.py` | CRUD for `~/.config/pengy/chats.json`; chats are plain dicts with `id` (UUID), `title`, `messages[]`, `created_at` |
| `llm_client.py` | `LLMClient.chat()` — a Python generator that yields `tool_request`, `assistant_tool_calls`, `tool_result`, or `final_response` dicts. Callers `.send()` confirmation dicts back into the generator to resume after tool calls |
| `tools.py` | 11 OpenAI function-calling tool schemas (`TOOLS`) and `execute_tool(name, arguments)`. Also manages `_sudo_password_provider` (callback set by UI or CLI) and `_tool_timeout` |

### Desktop UI Package (`pengy/ui/`)

Qt6 widgets wired together in `main_window.py`. See [Desktop UI Layout](#desktop-ui-layout) below for details.

### CLI Package (`pengy/cli/`)

Terminal interface built on `rich`. See [CLI](#cli) section below for details.

---

## Desktop UI Layout

```
┌────────────────────┬──────────────────────────────────────────────────┐
│  + New Chat        │                                                  │
│  ⚙ Settings        │           Chat View (Markdown)                   │
│                    │                                                  │
│  ─────────────     │  You 🧑                                          │
│  Chat 1            │  Can you list files in /tmp?                     │
│  Chat 2            │                                                  │
│  Chat 3            │  🔧 Using tool: run_bash [command=ls /tmp]       │
│                    │                                                  │
│  ─────────────     │  Tool output                                     │
│  Model: gpt-4o     │  file1.txt  file2.py                             │
│  Tool Confirm: None│                                                  │
│                    │  Assistant 🤖                                    │
│                    │  Here are the files in /tmp: ...                 │
│                    │                                                  │
│                    │  ─────────────────────────────────────────────   │
│                    │  📄 notes.md  ✕                                  │
│                    │  [📎] [Type a message...                      ]  │
└────────────────────┴──────────────────────────────────────────────────┘
```

### Left Pane (Sidebar)
- **+ New Chat button** — Creates a new chat session
- **⚙ Settings button** — Opens the settings dialog
- **Chat history list** — Scrollable, sorted newest first; click to load, right-click or trash icon (🗑) to delete
- **Quick settings panel** — Shows current model name, tool confirmation mode (YOLO/Safe/None), and a status dot (green = idle, blinking = waiting for LLM)

### Right-Top Pane (Chat View)
- Markdown-rendered chat messages via `QTextBrowser`
- **User messages:** bold dark-blue "You 🧑" label, plain body text
- **Assistant messages:** bold dark-green "Assistant 🤖" label, HTML-rendered markdown
- **Tool requests:** bold amber `🔧 Using tool: <name> [<args truncated to 40 chars>]`
- **Tool output:** italic "Tool output" label, monospace content in a styled panel
- Syntax-highlighted code blocks (monokai style, `#f5f5f5` background)
- Auto-scrolls to bottom on new content

### Right-Bottom Pane (Chat Input)
- **📎 Attach button** — Opens file picker; accepts text files only (detected by extension, MIME type, and UTF-8 sniff); binary files are rejected with an error dialog
- **File chips** — Selected files shown as removable badges above the input; cleared after send
- **Text input** — Multi-line QPlainTextEdit; Enter to send, Shift+Enter for newline
- On send: attached file contents are injected into the message as fenced code blocks before the user's text

---

## CLI

The CLI provides two modes — interactive REPL and single-shot — both driven by the `PengyCLI` class in `pengy/cli/main.py`.

### Entry Points

```bash
# Interactive REPL
python -m pengy.cli.main
./run_pengy_cli.sh

# Single-shot
python -m pengy.cli.main "What is the capital of France?"
./run_pengy_cli.sh "List all files in /tmp"
```

### Interactive Mode

On startup:
1. Loads the most recent chat from `chats.json` (or creates a new one if none exist)
2. Shows a welcome panel with model name and tool confirmation status
3. Enters the REPL loop: prompt → send → stream generator → loop

The `_drive_generator()` method runs the LLM generator **in the main thread** (unlike the GUI which uses a QThread). Tool confirmation blocks on user input via `rich.prompt.Prompt.ask()` with a 3-choice menu: Execute / Yes to all this turn / Decline.

### Single-Shot Mode

1. Creates a throw-away chat (persisted unless `--no-save` is passed)
2. Sends the prompt, drives the generator to completion, and exits
3. Useful for scripting: `pengy-cli "summarize this file" && pengy-cli "translate to French"`
4. The `--no-save` flag prevents single-shot chats from polluting the sidebar history

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show the command reference table |
| `/new` | Start a new chat session |
| `/yolo [all\|safe\|none]` | Set tool confirmation: all (YOLO), safe (read-only), none — cycles if no arg |
| `/config` | Show current configuration (base URL, model, timeout, etc.) |
| `/model <name>` | Switch models (e.g. `/model gpt-4o`) |
| `/models` | Fetch available models from the endpoint's `GET /v1/models` |
| `/list` | List recent chats with index, title, message count, and creation date |
| `/load <index>` | Load a chat by its `/list` index |
| `/delete <index>` | Delete a chat by its `/list` index |
| `/attach <path>` | Attach a text file (or use `@path` inline in your prompt) |
| `/system` | Show the system message template and rendered output |
| `/compact` | Elide old tool results to free context window space |
| `/quit`, `/exit`, `/q` | Exit the CLI |

### CLI Rendering

Built on `rich`:
- **Welcome:** `Panel.fit()` with app branding
- **Assistant response:** `Panel(Markdown(...))` with green border
- **Tool requests:** `Panel` with yellow border showing tool name and full JSON args
- **Tool results:** `Panel` with dim border, content truncated to 2,000 chars for display
- **Chat list:** `rich.Table` with #, title, message count, created columns
- **Config display:** `rich.Table` with setting/value columns
- **Sudo password:** `Prompt.ask(..., password=True)` — masked input
- **"Thinking…" indicator:** Overwritten with `\r` so the cursor sits on the same line

---

## Data Storage

### Settings File: `~/.config/pengy/settings.json`

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_key": "",
  "model": "gpt-4o",
  "system_message": "You are a helpful assistant. The current date is {date} and the user is {username} on host {hostname} which is {osinfo}.",
  "tool_confirmation": "none",
  "ui_scale": 100,
  "user_agent": "PengyAgent/1.0",
  "tool_timeout": 60,
  "context_keep_turns": 0
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_url` | string | `https://api.openai.com/v1` | OpenAI-compatible API endpoint |
| `api_key` | string | (empty) | API key |
| `model` | string | `gpt-4o` | Model name |
| `system_message` | string | (see above) | Template; `{date}`, `{username}`, `{hostname}`, `{osinfo}` filled at send time |
| `tool_confirmation` | string | `"none"` | Tool confirmation mode: `"all"` (YOLO — skip all confirmations), `"safe"` (auto-approve read-only tools; confirm write/execute), `"none"` (confirm every tool) |
| `ui_scale` | int | `100` | Sets `QT_SCALE_FACTOR` on next launch (75/100/125/200); CLI ignores this |
| `user_agent` | string | `PengyAgent/1.0` | User-Agent header for HTTP requests (downloads, URL fetches) |
| `tool_timeout` | int | `60` | Timeout in seconds for tool execution (-1 = no timeout) |
| `context_keep_turns` | int | `0` | Number of recent turns whose tool results are kept; older ones are elided to `[tool output from earlier turn elided]`. 0 = keep all. |

### System Message Templating

`config.render_system_message(template)` is called at send time (not at save time), so `{date}` always reflects today. Variables:

| Placeholder | Source |
|------------|--------|
| `{date}` | `datetime.date.today().strftime("%B %d, %Y")` |
| `{username}` | `getpass.getuser()` |
| `{hostname}` | `socket.gethostname()` |
| `{osinfo}` | `f"{platform.system()} {platform.release()}"` |

### Chats File: `~/.config/pengy/chats.json`

Array of chat session objects:

```json
[
  {
    "id": "uuid-here",
    "title": "First message preview...",
    "messages": [
      {"role": "user", "content": "Hello"},
      {"role": "assistant", "content": "Hi there!"}
    ],
    "created_at": "2026-05-13T21:00:00"
  }
]
```

Only `user`, `assistant` (including those with `tool_calls`), and `tool` messages are persisted. This means a chat can be reloaded and rendered without re-running tools. When re-sending, the stored messages are passed to the API directly — the agent continues from where it left off.

---

## Tools

All tools are defined in `tools.py` as OpenAI function-calling schemas (`TOOLS`) and executed via `execute_tool(name, arguments)`.

Tool argument previews are shown in the chat view truncated to 40 characters: `🔧 Using tool: web_search [query=latest Python news…]`.

### `read_file(path)`
Reads a local file. Expands `~`. Returns file contents or an error string.

### `read_multiple_files(paths)`
Reads up to 20 files at once, each under a clear header. Individual files capped at 50,000 chars; total output capped at 120,000 chars. Binary files are rejected with a clear error.

### `write_file(path, content)`
Writes content to a file. Creates parent directories as needed. Returns success or error.

### `replace_in_file(path, old_str, new_str)`
Performs an exact string replacement in an existing file. `old_str` must match **exactly one occurrence** — if zero or multiple matches are found, the edit is rejected with specific line-number diagnostics. This is the preferred tool for targeted edits (safer than full rewrites).

### `run_bash(command)`
Executes a bash command via `subprocess.run`. Timeout configurable via `tool_timeout` (default 60s). Captures stdout and stderr.

**sudo support:** If the command contains `sudo`, the password provider callback is invoked. In the GUI this shows a `QInputDialog`; in the CLI it uses `Prompt.ask(..., password=True)`. The password is passed via `sudo -S` (stdin); the password prompt line is stripped from stderr. Cancelling returns a cancellation message to the LLM. The password is cached for the session so subsequent sudo commands don't re-prompt.

### `run_python(code)`
Writes code to a temp file and executes it with `python3`. Timeout configurable via `tool_timeout` (default 60s). Captures stdout and stderr.

### `web_search(query, max_results=5)`
Searches the web using DuckDuckGo (`ddgs`). 5-second hard timeout on the search call. Returns numbered results with title, URL, and snippet.

### `download_file(url, filename=None)`
Downloads a file to `~/Downloads/`. Derives filename from URL if not specified. Uses the configured `user_agent` for the HTTP request. Returns destination path and file size.

### `fetch_url(url)`
Fetches a URL and returns its text content (up to 50,000 characters). HTML is stripped to readable text (scripts, styles, and head removed via `HTMLParser`). Useful for pulling in documentation before coding.

### `directory_tree(path, max_depth=3, show_hidden=False)`
Shows a visual tree of the directory structure. Skips common noise directories (`.git`, `node_modules`, `__pycache__`, etc.) by default. Entries capped at 500; output capped at 40,000 characters. Uses Unicode box-drawing characters (├── └── │).

### `search_content(pattern, path, file_glob=None, context_lines=0, max_results=50)`
Searches for a regex pattern in files under a directory. If the regex is invalid, it's re-attempted as a literal search. Returns matching lines with file path, line number, context lines, and a `▸` marker on matched lines. Results are grouped into contiguous regions to avoid duplicate context. Skips binary files and common noise directories.

---

## Tool Execution Flow

```
LLM responds with tool call(s)
       │
       ├─ tool_confirmation = "all" ──► show tool + args → execute → feed result → loop
       │
       ├─ tool_confirmation = "safe" & tool is read-only ──► auto-approve → execute → loop
       │
       └─ Otherwise
              │
              ▼
        Confirmation dialog (tool name + full args JSON)
              │
              ├── Execute              → execute → feed result → loop
              ├── Yes to all this turn → execute (yolo for rest of this LLM round-trip) → loop
              └── Cancel / Decline     → "Tool execution was declined by user." → loop
```

Both the GUI and CLI "Yes to all this turn" auto-approves all remaining tool calls
from the current API response (it resets on the next assistant message with tool_calls).

### Desktop Flow

The `ChatWorker` (a `QObject` moved to a `QThread`) drives `LLMClient.chat()` via the generator protocol. For tool confirmation and sudo password prompts, the worker blocks on a `threading.Event` while the main thread shows the dialog. The main thread unblocks the worker via `send_confirmation()` or `send_sudo_password()` which set the event.

**Token usage:** After the final response, the GUI sidebar shows the turn's prompt/completion token counts. The data is accumulated across all API calls in the turn (including tool-call retries).

### CLI Flow

The `PengyCLI._drive_generator()` method runs the generator **synchronously in the main thread**. Tool confirmation blocks on `rich.prompt.Prompt.ask()` — a simple 1/2/3 menu. No threading is involved. Sudo passwords use the same password-masked prompt.

**Token usage:** Displayed in a dim footer line after the assistant's response.

---

## Message Flow

```
User types + optionally attaches files → Enter
       │
       ▼
Attached files injected as fenced blocks into message text (GUI only)
       │
       ▼
User message appended to chat view and message history
       │
       ▼
System message rendered (templates filled) and prepended
       │
       ▼
LLM API call (non-streaming, full response at once)
       │
       ├── No tool calls → render final response → save chat
       │
       └── Tool call(s) → confirm/execute loop → final response → save chat
```

**Note:** The system message is **not** stored in `chat["messages"]` — it is prepended at request time so templates are always fresh.

---

## Settings Dialog (Desktop)

| Field | Widget | Notes |
|-------|--------|-------|
| Base URL | QLineEdit | OpenAI-compatible endpoint |
| API Key | QLineEdit (masked) | Stored in settings.json (plaintext) |
| Model | QComboBox (editable) | Pre-populated with current model; "↻ Fetch" button calls `GET /v1/models` to populate the dropdown |
| System Message | QTextEdit | Supports `{date}`, `{username}`, etc. templates |
| Tool Confirmation | QComboBox | "YOLO (All)", "Safe Only", "None" — controls which tools require confirmation |
| Keep tool results | QSpinBox | Number of recent turns to keep tool results for (0 = keep all) |
| UI Scale | QComboBox | 75%, 100%, 125%, 200% — takes effect on relaunch |
| Tool timeout | QSpinBox | Seconds (-1 = no timeout) |

---

## App Identity

- **Application name:** "Pengy" (set via `QApplication.setApplicationName`)
- **Icon:** `pengy/assets/icon.svg` — SVG penguin, loaded at startup via `QApplication.setWindowIcon`
- The desktop app shows in taskbar, alt-tab, and window decorations on X11/XWayland. On native Wayland, the provided `pengy.desktop` file may be needed for taskbar icon.
- The CLI has no icon but uses the penguin emoji (🐧) in its welcome panel.

---

## Dependencies

| Package | Version | Purpose | Required by |
|---------|---------|---------|------------|
| PySide6 | >= 6.6.0 | Qt6 GUI framework | Desktop only |
| openai | >= 1.0.0 | OpenAI-compatible API client | Both |
| markdown | >= 3.5 | Markdown to HTML conversion | Desktop only |
| pygments | >= 2.17.0 | Syntax highlighting | Desktop only |
| ddgs | >= 9.0.0 | DuckDuckGo web search | Both |
| rich | >= 13.0.0 | CLI formatting (tables, panels, markdown) | CLI only |

```
# Desktop (full)
pip install PySide6 openai markdown pygments ddgs

# CLI only
pip install openai ddgs rich
```

---

## Design Decisions

**Generator protocol for tool flow:** `LLMClient.chat()` is a Python generator that yields tool request / result / final response dicts. This allows both the GUI (via QThread + threading.Event) and the CLI (synchronous) to drive the same core with different concurrency models. The generator pauses on `yield` during tool confirmation, and resumes when the caller `.send()`s a confirmation dict.

**Non-streaming:** The OpenAI client uses `chat.completions.create` (no `stream=True`). Full responses render at once. This simplifies the architecture (no incremental state management) and is acceptable because tool call round-trips dominate latency for agentic workflows.

**System message templating at send time:** Templates are resolved fresh on every send so `{date}` is always accurate regardless of when the config was saved.

**Sudo via `-S`:** Rather than a PTY (which would handle any interactive prompt but adds significant complexity), the app specifically detects `sudo` in bash commands, prompts for a password, and passes it to `sudo -S`. Covers the common case with minimal added complexity. The password is cached in memory for the session to avoid re-prompting on multi-step workflows.

**File attachment injection (GUI):** Attached files are formatted as fenced code blocks and prepended to the message text before sending. The LLM sees them as part of the user turn, so no special API handling is needed.

**JSON storage:** Human-readable, easy to backup, no database dependencies. Shared between GUI and CLI.

**PySide6 over PyQt6:** LGPL license is more permissive than GPL.

**CLI shares core with GUI:** Same config, same chat history, same tool execution, same LLM client. The CLI is not a separate project — it's an alternative frontend to the same agent.

**Single-threaded CLI:** The CLI runs the generator in the main thread. No threading complexity. Tool confirmation blocks on user input, which is natural in a terminal.
