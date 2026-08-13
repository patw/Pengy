# Pengy — Application Specification

## Overview

Pengy is a local-first AI agent application that connects to any OpenAI-compatible LLM API and gives the model a set of tools to operate on the user's machine. It provides two interfaces backed by the same core:

| Interface | Entry Point | Description |
|-----------|------------|-------------|
| **Desktop GUI** | `pengy/main.py` | Qt6 three-pane chat with markdown rendering, file attachments, multi-session sidebar |
| **CLI** | `pengy/cli/main.py` | Terminal REPL with slash commands or single-shot mode for scripting |
| **Web UI** | `pengy/web/main.py` | Flask server-side-rendered chat; SSE streaming; tool confirmation modals; responsive Bootstrap layout |

All three write to the same `~/.config/pengy/` directory — settings and chat history are shared.

---

## How to Read This Spec

This document exists so someone can build their own Pengy from it — in another language, with a
different UI, different libraries, and different UX choices. To make that possible, every section
is one of two kinds:

| Marker | Meaning |
|--------|---------|
| **Conformance: required** | Build it this way or it is not Pengy. Wire formats, on-disk formats, API invariants, tool contracts, and safety rules. Another edition must interoperate: all editions read and write the same `~/.config/pengy/` files, so a chat written by one must load in another. |
| **Conformance: reference** | This is *how the Python edition happens to do it*, recorded so you can see a working answer. Reimplement freely. Threading models, widget toolkits, prompt libraries, rendering, layout, and every class or method name are reference-only. |

Where this document names a Python symbol — `LLMClient.chat()`, `ChatWorker`, `QThread`,
`threading.Event`, `rich.prompt.Prompt.ask()` — treat it as an illustrative pointer to the
reference implementation, never as a requirement. The Rust and C++ editions satisfy the same
contracts with tokio tasks and `QWaitCondition` respectively.

**The required sections are:** Data Storage, Tools, Tool Execution Flow, LLM Loop Contract, and
the parts of Design Decisions that state an invariant rather than a preference. If you read
nothing else before writing code, read **LLM Loop Contract** — it is the part that is invisible
in a demo and fatal in production.

Two things this spec deliberately does *not* pin down, because they are yours to choose: how many
frontends you build (the Python edition has three; one is fine), and what your agent's personality
and default system message are.

---

## Technology Stack

*Conformance: **reference** — reimplement freely.*

- **Language:** Python 3.10+
- **GUI Framework:** PySide6 (LGPL)
- **CLI Framework:** `rich` (tables, panels, markdown rendering, prompts)
- **Web Framework:** Flask (threaded dev server; SSE streaming responses)
- **LLM Client:** `openai` Python SDK (non-streaming)
- **Markdown Rendering:** `markdown` + `pygments` (syntax highlighting, desktop + web); `rich.markdown` (CLI)
- **Web UI:** Bootstrap 5.3 (responsive layout, light theme, offcanvas sidebar)
- **Web Search:** `ddgs` (DuckDuckGo)
- **Storage:** JSON files in `~/.config/pengy/`

---

## Architecture

*Conformance: **reference** — reimplement freely.*

```
pengy/
├── main.py                    # Desktop GUI entry point
├── assets/
│   └── icon.png               # Penguin app icon
├── cli/
│   ├── __init__.py            # Package marker
│   └── main.py                # CLI entry point — PengyCLI class (interactive + single-shot)
├── core/
│   ├── config.py              # Settings load/save + render_system_message()
│   ├── chat_manager.py        # Chat session CRUD
│   ├── llm_client.py          # OpenAI-compatible API client (generator protocol)
│   └── tools.py               # Tool schemas (TOOLS), execution (execute_tool), sudo provider
├── ui/
│   ├── main_window.py         # 3-pane main window; wires all signals
│   ├── chat_history.py        # Left sidebar — chat list + quick settings panel
│   ├── chat_view.py           # Right-top — markdown chat renderer (QTextBrowser)
│   ├── chat_input.py          # Right-bottom — input field + attachment button
│   ├── chat_worker.py         # QThread worker driving the LLM generator
│   └── settings_dialog.py     # Settings modal dialog
└── web/
    ├── __init__.py            # Package marker
    ├── app.py                 # Flask app — routes, WebWorker, SSE streaming
    ├── main.py                # Web entry point (argparse: --host, --port, --debug)
    └── templates/
        ├── base.html          # Bootstrap 5.3 base — navbar, offcanvas sidebar, CSS
        ├── chat.html          # Chat view — server-rendered history + SSE live updates
        └── settings.html      # Settings form
```

### Core Package (`pengy/core/`)

Pure Python — no Qt or terminal dependencies. Shared by both GUI and CLI.

| Module | Responsibility |
|--------|---------------|
| `config.py` | Load/save `~/.config/pengy/settings.json` with default merging; `render_system_message()` fills `{date}`, `{username}`, `{hostname}`, `{osinfo}` placeholders at call time |
| `chat_manager.py` | CRUD for `~/.config/pengy/chats.json`; chats are plain dicts with `id` (UUID), `title`, `messages[]`, `created_at` |
| `llm_client.py` | `LLMClient.chat()` — a Python generator that yields `tool_request`, `assistant_tool_calls`, `tool_result`, or `final_response` dicts. Callers `.send()` confirmation dicts back into the generator to resume after tool calls |
| `tools.py` | 15 OpenAI function-calling tool schemas (`TOOLS`) and `execute_tool(name, arguments)`. Also manages `_sudo_password_provider` (callback set by UI or CLI) and `_tool_timeout` |

### Desktop UI Package (`pengy/ui/`)

Qt6 widgets wired together in `main_window.py`. See [Desktop UI Layout](#desktop-ui-layout) below for details.

### CLI Package (`pengy/cli/`)

Terminal interface built on `rich`. See [CLI](#cli) section below for details.

### Web Package (`pengy/web/`)

Flask server-side-rendered interface. See [Web UI](#web-ui) section below for details.

---

## Desktop UI Layout

*Conformance: **reference** — reimplement freely.*

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
- **📎 Attach button** — Opens file picker; accepts text files (detected by extension, MIME type, and UTF-8 sniff) and images (JPEG, PNG, GIF, WebP); other binary files are rejected with an error dialog
- **Image paste** — Clipboard images are pasted directly into the chat (saved to a temp file, sent as base64 data URIs)
- **File chips** — Selected files shown as removable badges above the input (🖼 for images, 📄 for text); cleared after send
- **Text input** — Multi-line QPlainTextEdit; Enter to send, Shift+Enter for newline
- On send: attached text file contents are injected into the message as fenced code blocks before the user's text; images are sent as image content parts

---

## CLI

*Conformance: **reference** for the terminal implementation (prompt library, colours, layout).
The **slash-command set below is required** if you ship a CLI — it is kept identical across the
Python, Rust, and C++ editions so muscle memory and scripts carry over.*

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

Flags (shared with the Rust and C++ CLIs): `--no-save`, `--model NAME`, `--system MSG`, `--output pretty|raw|json|silent`, `--config-dir PATH`, `-v/--version`. `--model` and `--system` are in-memory overrides — they never modify `settings.json`.

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show the command reference table |
| `/new` | Start a new chat session |
| `/show [n]` | Show the full conversation (optional: last n messages) |
| `/tail [n]` | Show the last n messages (default 5) |
| `/rename <title>` | Rename the current chat |
| `/clear` | Clear the terminal screen |
| `/export [path]` | Export the current chat as Markdown |
| `/yolo [all\|safe\|none]` | Set tool confirmation: all (YOLO), safe (read-only), none — cycles if no arg |
| `/config` | Show current configuration (base URL, model, timeout, etc.) |
| `/model <name>` | Switch models (e.g. `/model gpt-4o`) |
| `/models` | Fetch available models from the endpoint's `GET /v1/models` |
| `/baseurl <url>` | Change the API base URL |
| `/apikey <key>` | Set the API key |
| `/timeout <sec>` | Set tool execution timeout |
| `/agent <string>` | Set the user agent string |
| `/context-keep <n>` | Set context elision keep-turns (0 = keep all) |
| `/list` | List recent chats with index, title, message count, and creation date |
| `/load <index>` | Load a chat by its `/list` index |
| `/delete <index>` | Delete a chat by its `/list` index |
| `/attach <path>` | Attach a text file (or use `@path` inline in your prompt) |
| `/system [message]` | Show or set the system message template |
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

## Web UI

*Conformance: **reference** — reimplement freely.*

The web interface is a single-user Flask application intended to be run on a personal server and accessed remotely (e.g. from a phone). SSL and authentication are expected to be handled by a reverse proxy (nginx).

### Entry Points

```bash
pengy-web                            # localhost:5000
pengy-web --host 0.0.0.0             # all interfaces (for nginx reverse proxy)
pengy-web --host 0.0.0.0 --port 8080
```

### Layout

```
┌──navbar: 🐧 Pengy  [model] [Confirm badge]  [⚙]──────────┐
│                                                             │
│  ┌─sidebar (260px)─┐  ┌─chat area──────────────────────┐  │
│  │  [+ New Chat]   │  │  message history (scrollable)  │  │
│  │                 │  │                                │  │
│  │  Chat 1   [×]  │  │  User bubble (right-aligned)   │  │
│  │  Chat 2   [×]  │  │  🔧 tool card (collapsed)       │  │
│  │  Chat 3   [×]  │  │  Assistant bubble (markdown)   │  │
│  └─────────────────┘  │                                │  │
│   (offcanvas on mob.) │  ┌──input + [Send]────────────┐│  │
│                        │  └────────────────────────────┘│  │
│                        └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

- **Sidebar** — 260 px fixed column on md+ screens; offcanvas drawer on mobile. Lists all chats with delete button; "New Chat" button at top.
- **Chat area** — Server-rendered message history on page load; SSE appends new content live during generation.
- **Input** — Auto-expanding textarea; Enter to send, Shift+Enter for newline.
- **Navbar** — Shows current model and tool confirmation mode badge; gear icon links to settings.

### Message Rendering

| Message type | Appearance |
|---|---|
| User | Right-aligned rounded bubble |
| Assistant | Left-aligned bubble, full markdown + syntax highlighting (Pygments `friendly` style) |
| Tool request | Collapsed accordion card with amber left border; click header to expand args |
| Tool result | Injected into card body on completion; green border = done, red = declined |
| Thinking | Spinner with "Thinking…" text, removed when response arrives |

### WebWorker

`WebWorker` mirrors the GUI's `ChatWorker` pattern. It drives `LLMClient.chat()` in a daemon thread and communicates with the SSE endpoint via a `queue.Queue`. For tool confirmation it blocks on a `threading.Event` until the browser POSTs `/confirm`. For sudo it blocks similarly until the browser POSTs `/sudo`.

The worker remains in the `_workers` dict until the SSE endpoint has drained its event queue, preventing a race condition where fast failures (e.g. bad API key) would be lost before the browser's SSE connection opened.

### Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Redirect to most recent chat (or create new) |
| POST | `/chat/new` | Create a new chat session |
| GET | `/chat/<id>` | Render chat page (server-side history) |
| POST | `/chat/<id>/send` | Append user message, start WebWorker |
| GET | `/chat/<id>/stream` | SSE endpoint — streams events until final response |
| POST | `/chat/<id>/confirm` | Unblock tool confirmation (confirmed/declined/yolo) |
| POST | `/chat/<id>/sudo` | Provide sudo password to blocked worker |
| POST | `/chat/<id>/stop` | Cancel running generation for a chat |
| POST | `/chat/<id>/delete` | Delete chat and redirect to index |
| GET | `/chat/<id>/export` | Download the chat as a Markdown file |
| POST | `/chat/<id>/rename` | Rename a chat |
| POST | `/chat/<id>/command` | Web slash commands (`/new /yolo /model /rename /export /help`) typed in the chat input |
| GET | `/models` | Fetch available models from the endpoint (settings page Fetch button) |
| GET/POST | `/settings` | View/update all config fields |

### SSE Event Types

| Type | Payload | Browser action |
|------|---------|---------------|
| `tool_request` | `name`, `args`, `auto_approved` | Append tool card; if not auto-approved, show confirmation modal |
| `tool_result` | `content`, `declined` | Update tool card body and badge |
| `final_response` | `html`, `usage` | Append assistant bubble |
| `sudo_request` | — | Show sudo password modal |
| `error` | `message` | Append error alert, re-enable input |
| `keepalive` | — | SSE comment (`: keepalive`); browser ignores |

### Tool Confirmation Flow (Web)

```
SSE sends tool_request (auto_approved=false)
       │
       ▼
Browser shows Bootstrap modal (tool name + args JSON)
       │
       ├── Execute              → POST /confirm {confirmed: true}
       ├── Yes to all this turn → POST /confirm {confirmed: true, yolo_turn: true}
       └── Decline              → POST /confirm {confirmed: false}
              │
              ▼
       WebWorker._confirm_event is set → generator resumes
```

---

## Data Storage

*Conformance: **required**.*

### Settings File: `~/.config/pengy/settings.json`

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_key": "",
  "model": "gpt-4o",
  "system_message": "You are a helpful assistant named Pengy. The current date is {date} and the user is {username} on host {hostname} which is {osinfo}.",
  "tool_confirmation": "none",
  "reasoning_effort": "",
  "preserve_reasoning": false,
  "context_keep_turns": 0,
  "ui_scale": 100,
  "theme_mode": "system",
  "theme_accent": "default",
  "user_agent": "PengyAgent/1.0",
  "llm_timeout": 300,
  "tool_timeout": 300,
  "tool_output_max_chars": 250000,
  "download_max_mb": 100,
  "image_max_dimension": 4096,
  "image_max_mb": 4.5,
  "image_quality": 85
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_url` | string | `https://api.openai.com/v1` | OpenAI-compatible API endpoint |
| `api_key` | string | (empty) | API key |
| `model` | string | `gpt-4o` | Model name |
| `system_message` | string | (see above) | Template; `{date}`, `{username}`, `{hostname}`, `{osinfo}` filled at send time |
| `tool_confirmation` | string | `"none"` | Tool confirmation mode: `"all"` (YOLO — skip all confirmations), `"safe"` (auto-approve read-only tools; confirm write/execute), `"none"` (confirm every tool) |
| `reasoning_effort` | string | `""` | Passed as `reasoning_effort` on API calls when set: `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max` (`""` = provider default) |
| `preserve_reasoning` | bool | `false` | Keep reasoning fields (`reasoning_content`, `reasoning`, `reasoning_details`) on assistant messages sent back to the API |
| `context_keep_turns` | int | `0` | Number of recent turns whose tool results are kept; older ones are elided to `[tool output from earlier turn elided]`. 0 = keep all. |
| `ui_scale` | int | `100` | Sets `QT_SCALE_FACTOR` on next launch (75/100/125/200); CLI ignores this |
| `theme_mode` | string | `"system"` | Desktop theme: `"system"`, `"light"`, or `"dark"` |
| `theme_accent` | string | `"default"` | Desktop accent color: `default`/`blue`/`teal`/`green`/`orange`/`red`/`pink`/`purple` |
| `user_agent` | string | `PengyAgent/1.0` | User-Agent header for HTTP requests (downloads, URL fetches) |
| `llm_timeout` | int | `300` | HTTP timeout in seconds for each LLM API request |
| `tool_timeout` | int | `300` | Timeout in seconds for tool execution (-1 = no timeout) |
| `tool_output_max_chars` | int | `250000` | Tool output longer than this is snipped head+tail with a `[... snipped N chars from middle ...]` marker. 0 = no limit |
| `download_max_mb` | int | `100` | Default maximum download size for `download_file` in MB. Per-call `max_size_mb` overrides it; `0` = no limit |
| `image_max_dimension` | int | `4096` | Attached images are downscaled so neither side exceeds this (px) |
| `image_max_mb` | float | `4.5` | Attached images are re-encoded until under this size (MB) |
| `image_quality` | int | `85` | JPEG quality (0–100) used when re-encoding attached images |

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

*Conformance: **required**.*

All tools are defined in `tools.py` as OpenAI function-calling schemas (`TOOLS`) and executed via `execute_tool(name, arguments)`.

Tool argument previews are shown in the chat view truncated to 40 characters: `🔧 Using tool: web_search [query=latest Python news…]`.

### `read_file(path)`
Reads a local file. Expands `~`. Returns file contents or an error string.

### `read_multiple_files(paths)`
Reads up to 20 files at once, each under a clear header. The per-file budget equals `tool_output_max_chars`; the total batch budget is five times that value. `0` disables these output limits. Binary files are rejected with a clear error.

### `write_file(path, content)`
Writes content to a file. Creates parent directories as needed. Returns success or error.

### `replace_in_file(path, old_str, new_str)`
Performs an exact string replacement in an existing file. `old_str` must match **exactly one occurrence** — if zero or multiple matches are found, the edit is rejected with specific line-number diagnostics. This is the preferred tool for targeted edits (safer than full rewrites).

### `apply_changes(changes, dry_run=False, postconditions=[])`
Applies a bounded, **transactional** set of exact-text edits across multiple files. Every operation is validated in memory first; if any one fails, **no file is written**. Use this instead of a sequence of `replace_in_file` calls when an edit must land atomically across files.

Each entry in `changes` is `{path, operations[]}`. Each operation has a `kind`:

| `kind` | Required fields | Behaviour |
|--------|-----------------|-----------|
| `replace` | `old`, `new` | Replace exact text |
| `insert_after` | `anchor`, `text` | Insert `text` immediately after `anchor` |
| `delete` | `old` | Remove exact text |

Every operation also accepts `expected_matches` (positive int, default `1`); the operation fails unless the match count is exactly that. `dry_run=true` returns the unified diff without writing. `postconditions` is a list of `{path, contains?, does_not_contain?}` checks evaluated *before* any write; a failed check aborts the whole transaction.

Safety limits (rejected with an error if exceeded): 20 files, 100 total operations, 1,000,000 total bytes, 256,000 bytes per individual text block. Paths are expanded, resolved to absolute, and must already exist; duplicate paths in one call are rejected (combine the operations instead).

Returns the unified diff on success.

### `run_bash(command, cwd=None)`
Executes a bash command via `subprocess.Popen` in its own process group. An optional `cwd` is expanded and must name an existing directory. Timeout configurable via `tool_timeout` (default 300s). Captures stdout and stderr.

**sudo support:** If the command contains `sudo`, the password provider callback is invoked. In the GUI this shows a `QInputDialog`; in the CLI it uses `Prompt.ask(..., password=True)`. The password reaches sudo via `SUDO_ASKPASS` (every `sudo` rewritten to `sudo -A`; see Design Decisions), not stdin — the command's own stdin is `/dev/null`. The password prompt line is stripped from stderr. Cancelling returns a cancellation message to the LLM. The password is cached for the duration of the LLM run (so multi-step sudo workflows within one turn don't re-prompt) and cleared when the run completes.

### `run_python(code, cwd=None)`
Writes code to a temp file and executes it with `python3`. An optional `cwd` is expanded and must name an existing directory. Timeout configurable via `tool_timeout` (default 300s). Captures stdout and stderr.

### `web_search(query, max_results=5)`
Searches the web using DuckDuckGo (`ddgs`). 5-second hard timeout on the search call. Returns numbered results with title, URL, and snippet.

### `download_file(url, filename=None, dir=None, max_size_mb=None)`
Streams an HTTP(S) file to `dir` (default `~/Downloads/`), deriving the filename from the URL when needed. Existing same-name files are overwritten. The default size cap is `download_max_mb` (100 MB); `max_size_mb=0` disables the cap. Uses the configured `user_agent`, removes partial files on failure, and uses a 120-second no-data stall timeout.

### `fetch_url(url, max_chars=None)`
Fetches a URL and returns its text content. HTML is stripped to readable text (scripts, styles, and head removed via `HTMLParser`). Responses are limited by `tool_output_max_chars` by default; `max_chars` overrides the limit and `0` disables it (subject to the 2 MB response cap).

### `directory_tree(path, max_depth=3, show_hidden=False)`
Shows a visual tree of the directory structure. Skips common noise directories (`.git`, `node_modules`, `__pycache__`, etc.) by default. Entries capped at 500; output capped at 40,000 characters. Uses Unicode box-drawing characters (├── └── │).

### `search_content(pattern, path, file_glob=None, context_lines=0, max_results=50, regex=False)`
Searches for text in files under a directory. Matching is literal by default; set `regex=true` to interpret the pattern as a regular expression (invalid regexes return an error). Returns matching lines with file path, line number, context lines, and a `▸` marker on matched lines. Results are grouped into contiguous regions to avoid duplicate context. Skips binary files and common noise directories.

### `glob(pattern, path=None)`
Finds files matching a glob pattern. Supports `**` for recursive search (e.g. `src/**/*.py`). Respects `.gitignore`-style skip directories (`node_modules`, `__pycache__`, `.git`, `venv`, `build`, `dist`, `target`, etc.). Returns up to 200 matching paths sorted by name with file sizes. Prefer this over `run_bash('find ...')` or `run_bash('ls ...')` — it's faster, safer, and respects project boundaries.

### `todowrite(todos)`
Creates and updates a structured task list for tracking progress during complex multi-step operations. The LLM must send the **complete** list every time — not incremental updates. At most one task may be `in_progress` at any time (zero is allowed); tasks may be `pending`, `in_progress`, or `completed`. The tool validates these rules and echoes back the formatted list with status icons (`[ ]`, `[→]`, `[✓]`). This gives the LLM a persistent scratchpad that survives context window limits and tool-call round-trips.

### `ask_user_question(questions)`
Asks the user one or more multiple-choice questions to clarify requirements, gather preferences, or resolve ambiguity. Each question includes a short header, the question text, and a list of options with descriptions. This tool should be used when instructions are vague, multiple valid approaches exist, or the LLM needs a decision before proceeding. The harness handles rendering the form and returning the user's choices — this tool should never reach `execute_tool` directly.

---

#### Why These Three Tools?

The original 11 tools cover filesystem operations, code execution, web access, and codebase exploration — the mechanical layer of an agent. `glob`, `todowrite`, and `ask_user_question` address the **cognitive** and **interaction** gaps:

- **`glob`** replaces the common anti-pattern of the LLM calling `run_bash('find . -name "*.py"')` or `run_bash('ls -R')`, which is slow, noisy, and often misses `.gitignore`-style exclusions. `glob` is fast, structured, and respects project boundaries — the agent gets exactly what it needs in one call.
- **`todowrite`** gives the LLM a persistent scratchpad that survives context-window truncation and tool-call round-trips. Without it, the LLM either repeats work after context elision or drifts from its plan across multiple turns. The structured format (exactly one in-progress, send the full list) was deliberately chosen over free-form notes because it forces the model to maintain a single source of truth.
- **`ask_user_question`** closes the loop on ambiguous requests. Before this tool, the LLM had to either guess or produce a long-winded "I need more information" response that the user then had to manually address in a follow-up message. Now the agent can pause, ask a structured question, and resume with the answer — turning vague prompts into precise results without breaking the conversation flow.

`apply_changes` was added later for the same reason at the *filesystem* layer: a multi-file refactor expressed as N separate `replace_in_file` calls can fail halfway, leaving the tree in a state neither the user nor the model expected. Making the whole edit set transactional — validate everything in memory, write only if all of it holds — means a failed edit is always a no-op the model can retry cleanly.



## Tool Execution Flow

*Conformance: **required**.*

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

*Conformance: **required**.*

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

## LLM Loop Contract

*Conformance: **required**.*

Everything in this section is wire-level behaviour against an
OpenAI-compatible endpoint. An implementation that skips it will appear to work in demos and
then fail on real conversations — usually as an HTTP 400 that poisons a saved chat permanently,
or as a hard failure the first time a provider rate-limits. Language, threading model, and UI
are free choices; the rules below are not.

### Request shape

Every request sends the full message list, the complete tool schema array, and `tool_choice: "auto"`.
`reasoning_effort` is included **only** when the setting is a non-empty string (sending `""`
is an error on some providers). The HTTP timeout is `llm_timeout` seconds.

The system message is never stored in the chat. It is rendered (templates filled) and prepended
to a copy of the message list at request time, so `{date}` is always current.

### Message sequence invariants

These are enforced by the API, not by convention. Violating any of them returns a 400 for the
whole conversation, not just the offending message:

1. An `assistant` message carrying `tool_calls` **must** be followed by one `tool` message per
   `tool_calls[].id`, before any other role appears.
2. Every `tool` message **must** carry a `tool_call_id` matching an id declared by the immediately
   preceding `assistant` message.
3. A `tool` message with no matching preceding declaration is invalid.

The dangerous part is that a conversation can violate these *after the fact*. If the user presses
Stop, or the process dies, between an assistant tool-call message and its results being appended,
the saved chat is permanently unloadable — every future request replays the corruption.

Implementations therefore **must** repair the message list before sending. The reference
implementation does this in `clean_dangling_tool_calls()`:

- Walk the list, tracking tool-call ids that have been declared but not yet satisfied.
- For an `assistant` message with `tool_calls`, consume the `tool` messages that immediately
  follow and match. For any declared id left unsatisfied, **synthesize** a result:
  `{"role": "tool", "tool_call_id": <id>, "content": "Tool execution was cancelled by user."}`
- Drop any `tool` message whose id was never declared (orphan).
- Return a new list; never mutate the caller's.

Synthesizing rather than deleting matters: deleting the assistant message would erase the model's
own record of what it tried to do, and the user's transcript would silently lose a turn.

### Persistence

Only `user`, `assistant` (including those with `tool_calls`), and `tool` messages are persisted.
A reloaded chat therefore renders without re-running any tool, and can be continued by passing
the stored messages straight back to the API.

### Context elision

Controlled by `context_keep_turns` (0 = keep everything). A **turn** is a `user` message and every
message up to — but not including — the next `user` message.

Tool results outside the most recent *N* turns have their `content` replaced with the literal
string `[tool output from earlier turn elided]`. The message is otherwise left intact — role and
`tool_call_id` are preserved, because removing it would violate the pairing invariants above.
This is a copy, not a mutation: elision applies to what is *sent*, never to what is *saved*.

### Retry and backoff

Retryable statuses are **429** and **529**. Everything else propagates immediately. Up to
`_MAX_RETRIES = 5` retries (6 attempts total).

Delay for attempt *n* (0-indexed):

- If the response carries a retry hint, use it: `retry-after-ms` (OpenAI, integer milliseconds)
  takes precedence over the standard `retry-after` (seconds or HTTP-date). Cap at 60s.
- Otherwise exponential: `min(1.0 * 2^n, 60.0)` seconds.
- Apply ±25% jitter to whichever base was chosen, with a 0.1s floor.

The sleep must be **interruptible** — a user pressing Stop during a 60-second backoff cannot be
made to wait it out. Cancelling during backoff yields a normal final response
(`"Request cancelled during backoff."`), not an error.

Each retry emits a `retrying` event so the UI can show progress; it carries `attempt`,
`max_attempts`, `delay_secs`, `status_code`, and `message`. The HTTP client is recreated between
attempts.

### Token usage

Usage is accumulated across *every* API call in a turn, including tool-call round-trips and
retries, and reported once on the final response as `prompt_tokens` / `completion_tokens` /
`total_tokens`. Providers that omit `usage` must not break the turn.

### Tool execution safety net

Tool dispatch runs with two independent deadlines, because they catch different failures:

- Subprocess tools (`run_bash`, `run_python`) enforce `tool_timeout` at the process level and are
  killed by **process group**, so orphaned grandchildren die with them.
- Every tool additionally runs under an outer deadline of `tool_timeout + 30` seconds. This
  catches tools with no internal deadline — `read_file` on a hung network mount, `fetch_url`
  against a trickling server — without requiring each one to implement its own guard. On expiry
  the loop returns a timeout string and moves on rather than blocking forever.

Tool output longer than `tool_output_max_chars` is snipped head-and-tail with a marker naming the
setting, so the model can see the output was truncated and why.

### Per-run isolation

Sudo provider, cached sudo password, and the set of running subprocesses are **per-run** state,
not global. Any implementation supporting more than one concurrent run (tabs, multiple web
sessions) must scope them, or a sudo prompt raised by one run will be answered into another, and
a Stop on one run will kill another run's subprocesses. Single-run frontends may share one
default context.

### Images

Attached images are sent as OpenAI-style content parts on the user message:
`{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<data>"}}`, alongside any
`{"type": "text", ...}` part. Before encoding, images are downscaled so neither side exceeds
`image_max_dimension`, then re-encoded at `image_quality` until under `image_max_mb`.

---

## Settings Dialog (Desktop)

*Conformance: **reference** — reimplement freely.*

| Field | Widget | Notes |
|-------|--------|-------|
| Base URL | QLineEdit | OpenAI-compatible endpoint |
| API Key | QLineEdit (masked) | Stored in settings.json (plaintext) |
| Model | QComboBox (editable) | Pre-populated with current model; "↻ Fetch" button calls `GET /v1/models` to populate the dropdown |
| User Agent | QLineEdit | User-Agent for tool HTTP requests |
| System Message | QTextEdit | Supports `{date}`, `{username}`, etc. templates |
| Tool Confirmation | QComboBox | "YOLO (All)", "Safe Only", "None" — controls which tools require confirmation |
| Reasoning effort | QComboBox | Provider default / none / minimal / low / medium / high / xhigh / max |
| Reasoning preservation | QCheckBox | Keep reasoning fields on messages sent back to the API |
| Keep tool results | QSpinBox | Number of recent turns to keep tool results for (0 = keep all) |
| Theme mode | QComboBox | System / Light / Dark |
| Accent color | QComboBox | Default / Blue / Teal / Green / Orange / Red / Pink / Purple |
| UI Scale | QComboBox | 75%, 100%, 125%, 200% — takes effect on relaunch |
| Tool timeout | QSpinBox | Seconds (-1 = no timeout) |

## Tasks (Prompt Templates)

*Conformance: **required**.*

Tasks are reusable prompt templates stored in `~/.config/pengy/tasks.json` (same format across all Pengy editions). Each task has a title and a template body; `%placeholder%` tokens are collected via a form when the task is played, and the rendered prompt is sent through the normal chat path. Managed via the Tasks dialog in the desktop GUI (currently GUI-only — no CLI or web surface).

---

## App Identity

*Conformance: **reference** — reimplement freely.*

- **Application name:** "Pengy" (set via `QApplication.setApplicationName`)
- **Icon:** `pengy/assets/icon.png` — PNG penguin, loaded at startup via `QApplication.setWindowIcon`
- The desktop app shows in taskbar, alt-tab, and window decorations on X11/XWayland. On native Wayland, the provided `pengy.desktop` file may be needed for taskbar icon.
- The CLI has no icon but uses the penguin emoji (🐧) in its welcome panel.

---

## Dependencies

*Conformance: **reference** — reimplement freely.*

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

*Conformance: **mixed**. These record why the reference implementation chose what it did. Where a
decision states an invariant (message ordering, per-run isolation, transactional edits) it is
required; where it states a preference (non-streaming, Qt, single-threaded CLI) you may diverge —
but read the reasoning first, because most of these were paid for with a bug.*

**Generator protocol for tool flow:** `LLMClient.chat()` is a Python generator that yields tool request / result / final response dicts. This allows both the GUI (via QThread + threading.Event) and the CLI (synchronous) to drive the same core with different concurrency models. The generator pauses on `yield` during tool confirmation, and resumes when the caller `.send()`s a confirmation dict.

**Non-streaming:** The OpenAI client uses `chat.completions.create` (no `stream=True`). Full responses render at once. This simplifies the architecture (no incremental state management) and is acceptable because tool call round-trips dominate latency for agentic workflows.

**System message templating at send time:** Templates are resolved fresh on every send so `{date}` is always accurate regardless of when the config was saved.

**Sudo via `SUDO_ASKPASS`:** Rather than a PTY (which would handle any interactive prompt but adds significant complexity), the app specifically detects `sudo` in bash commands, prompts for a password, and hands it to sudo's askpass interface: every `sudo` in the command is rewritten to `sudo -A`, and `SUDO_ASKPASS` points at a mode-0700 temp script that echoes the `PENGY_SUDO_PASSWORD` environment variable given to the child process. The password never touches disk, and the script is removed when the command finishes. The password is cached in memory for the duration of the LLM run to avoid re-prompting on multi-step workflows, and cleared when the run completes.

The original design piped the password to the shell's stdin and rewrote only the *first* `sudo` to `sudo -S`. That quietly failed whenever anything else in the command touched stdin, which an LLM emits routinely: a pipeline (`echo x | sudo tee f`) gave sudo the pipe instead of the password, a redirect (`sudo cmd < /dev/null`) overrode it, an earlier command that reads stdin (`cat`, `read`) consumed the password first, and a second `sudo` had nothing left to read and died with `no tty present`. Only the single-`sudo`, no-stdin case worked. Askpass has none of those constraints — it is per-invocation and independent of stdin — so every occurrence can be rewritten safely. The command's own stdin is now `/dev/null` in all cases, which also stops a non-sudo command from hanging on a terminal read.

**Portability of the askpass approach:** `-A`/`SUDO_ASKPASS` is supported by both sudo implementations Ubuntu ships. Verified on Ubuntu 26.04 against **sudo-rs 0.2.13** (the default since 25.10, `/usr/bin/sudo` via the alternatives system) and **legacy sudo 1.9.17p2** (`/usr/bin/sudo.ws`): both invoke the helper exactly once per `sudo` for every command shape above, and both pass `PENGY_SUDO_PASSWORD` through to it. Flag combinations the rewrite can produce (`-A -u`, `-A -g`, `-A --`) parse on both. The helper script uses only POSIX `printf '%s\n'`, which is byte-identical between dash's builtin and uutils `printf` (the Rust coreutils Ubuntu now ships) — including passwords containing quotes, `$`, `%s`, backslashes, and spaces. Note that the earlier `sudo -S` approach was *not* broken by the sudo-rs switch; sudo-rs supports `-S` too. The stdin collisions above were the sole cause.

**File attachment injection (GUI):** Attached files are formatted as fenced code blocks and prepended to the message text before sending. The LLM sees them as part of the user turn, so no special API handling is needed.

**JSON storage:** Human-readable, easy to backup, no database dependencies. Shared between GUI and CLI.

**PySide6 over PyQt6:** LGPL license is more permissive than GPL.

**CLI shares core with GUI:** Same config, same chat history, same tool execution, same LLM client. The CLI is not a separate project — it's an alternative frontend to the same agent.

**Single-threaded CLI:** The CLI runs the generator in the main thread. No threading complexity. Tool confirmation blocks on user input, which is natural in a terminal.
