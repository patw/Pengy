# Pengy — Application Specification

## Overview

Pengy is a Qt6 desktop application for interacting with LLMs via the OpenAI-compatible API. It provides a three-pane interface for chat, with built-in tools for local operations (file read/write, bash execution, Python execution, web search, file download, and URL fetching). The app stores all data locally in JSON format.

---

## Technology Stack

- **Language:** Python 3.10+
- **GUI Framework:** PySide6 (LGPL)
- **LLM Client:** `openai` Python SDK (non-streaming)
- **Markdown Rendering:** `markdown` + `pygments` (syntax highlighting)
- **Web Search:** `ddgs` (DuckDuckGo)
- **Storage:** JSON files in `~/.config/pengy/`

---

## Architecture

```
pengy/
├── main.py                    # Entry point; sets app name, icon, scale factor
├── assets/
│   └── icon.svg               # Penguin app icon (SVG, loaded at startup)
├── core/
│   ├── config.py              # Settings JSON loading/saving; render_system_message()
│   ├── chat_manager.py        # Chat session CRUD operations
│   ├── llm_client.py          # OpenAI-compatible API client (generator protocol)
│   └── tools.py               # Tool schemas (TOOLS) and execution (execute_tool)
└── ui/
    ├── main_window.py         # 3-pane main window; wires all signals
    ├── chat_history.py        # Left sidebar — chat list + quick settings panel
    ├── chat_view.py           # Right-top — markdown chat renderer (QTextBrowser)
    ├── chat_input.py          # Right-bottom — input field + attachment button
    ├── chat_worker.py         # QThread worker driving the LLM generator
    └── settings_dialog.py     # Settings modal dialog
```

---

## UI Layout

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
│  YOLO: OFF         │                                                  │
│                    │  Assistant 🤖                                    │
│                    │  Here are the files in /tmp: ...                 │
│                    │                                                  │
│                    │  ─────────────────────────────────────────────   │
│                    │  📄 notes.md  ✕                                  │
│                    │  [📎] [Type a message...                      ]  │
└────────────────────┴──────────────────────────────────────────────────┘
```

### Left Pane (Sidebar)
- **New Chat button** — Creates a new chat session
- **Settings button** — Opens the settings dialog
- **Chat history list** — Scrollable, sorted newest first; click to load, right-click or trash icon (🗑) to delete
- **Quick settings panel** — Shows current model name, YOLO mode toggle, and a status dot (green = idle, blinking = waiting for LLM)

### Right-Top Pane (Chat View)
- Markdown-rendered chat messages via `QTextBrowser`
- **User messages:** bold dark-blue "You 🧑" label, plain body text
- **Assistant messages:** bold dark-green "Assistant 🤖" label, HTML-rendered markdown
- **Tool requests:** bold amber `🔧 Using tool: <name> [<args truncated to 40 chars>]`
- **Tool output:** italic "Tool output" label, monospace content
- Syntax-highlighted code blocks (monokai style, `#f5f5f5` background)
- Auto-scrolls to bottom on new content

### Right-Bottom Pane (Chat Input)
- **📎 Attach button** — Opens file picker; accepts text files only (detected by extension, MIME type, and UTF-8 sniff); binary files are rejected with an error dialog
- **File chips** — Selected files shown as removable badges above the input; cleared after send
- **Text input** — Multi-line; Enter to send, Shift+Enter for newline
- On send: attached file contents are injected into the message as fenced blocks before the user's text

---

## Data Storage

### Settings File: `~/.config/pengy/settings.json`

```json
{
  "base_url": "https://api.openai.com/v1",
  "api_key": "",
  "model": "gpt-4o",
  "system_message": "You are a helpful assistant. The current date is {date} and the user is {username} on host {hostname} which is {osinfo}.",
  "yolo_mode": false,
  "ui_scale": 100
}
```

| Field            | Type   | Default                         | Description                                        |
|------------------|--------|---------------------------------|----------------------------------------------------|
| `base_url`       | string | `https://api.openai.com/v1`     | OpenAI-compatible API endpoint                     |
| `api_key`        | string | (empty)                         | API key                                            |
| `model`          | string | `gpt-4o`                        | Model name                                         |
| `system_message` | string | (see above)                     | Template; `{date}`, `{username}`, `{hostname}`, `{osinfo}` filled at send time |
| `yolo_mode`      | bool   | `false`                         | Skip tool confirmation dialogs                     |
| `ui_scale`       | int    | `100`                           | Sets `QT_SCALE_FACTOR` on next launch (75/100/125/200) |

### System Message Templating

`config.render_system_message(template)` is called at send time (not at save time), so `{date}` always reflects today. Variables:

| Placeholder   | Source                          |
|---------------|---------------------------------|
| `{date}`      | `datetime.date.today()`         |
| `{username}`  | `getpass.getuser()`             |
| `{hostname}`  | `socket.gethostname()`          |
| `{osinfo}`    | `platform.system() + release()` |

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

Only `user` and `assistant` (final text) messages are persisted. Tool call messages and tool results are not saved.

---

## Tools

All tools are defined in `tools.py` as OpenAI function-calling schemas (`TOOLS`) and executed via `execute_tool(name, arguments)`.

Tool argument previews are shown in the chat view truncated to 40 characters: `🔧 Using tool: web_search [query=latest Python news…]`.

### `read_file(path)`
Reads a local file. Expands `~`. Returns file contents or an error string.

### `write_file(path, content)`
Writes content to a file. Creates parent directories as needed. Returns success or error.

### `run_bash(command)`
Executes a bash command via `subprocess.run`. 60-second timeout. Captures stdout and stderr.

**sudo support:** If the command contains `sudo`, a password dialog is shown in the UI. The password is passed via `sudo -S` (stdin); the password prompt line is stripped from stderr. Cancelling the dialog returns a cancellation message to the LLM without executing the command.

### `run_python(code)`
Writes code to a temp file and executes it with `python3`. 30-second timeout. Captures stdout and stderr.

### `web_search(query, max_results=5)`
Searches the web using DuckDuckGo (`ddgs`). Returns numbered results with title, URL, and snippet.

### `download_file(url, filename=None)`
Downloads a file to `~/Downloads/`. Derives filename from URL if not specified. Returns destination path and file size.

### `fetch_url(url)`
Fetches a URL and returns its text content (up to 50,000 characters). HTML is stripped to readable text (scripts and styles removed). Useful for pulling in documentation before coding.

---

## Tool Execution Flow

```
LLM responds with tool call(s)
       │
       ├─ YOLO mode ON ──► show tool + args in chat → execute → feed result back → loop
       │
       └─ YOLO mode OFF
              │
              ▼
        Confirmation dialog (tool name + full args JSON)
              │
              ├── OK      → execute → feed result back → loop
              └── Cancel  → "Tool execution was declined by user." → loop
```

The `ChatWorker` drives `LLMClient.chat()` (a Python generator) in a `QThread`. For tool confirmation and sudo password prompts, the worker blocks on a nested `QEventLoop` while the main thread shows the dialog, then resumes via `.send()` or a signal.

---

## Message Flow

```
User types + optionally attaches files → Enter
       │
       ▼
Attached files injected as fenced blocks into message text
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

---

## Settings Dialog

| Field          | Widget              | Notes                                          |
|----------------|---------------------|------------------------------------------------|
| Base URL       | QLineEdit           | OpenAI-compatible endpoint                     |
| API Key        | QLineEdit (masked)  | Stored in settings.json (plaintext)            |
| Model          | QLineEdit           | Any model name the endpoint accepts            |
| System Message | QTextEdit           | Supports `{date}`, `{username}`, etc. templates|
| YOLO Mode      | QCheckBox           | Skips tool confirmation dialogs                |
| UI Scale       | QComboBox           | 75%, 100%, 125%, 200% — takes effect on relaunch|

---

## App Identity

- **Application name:** "Pengy" (set via `QApplication.setApplicationName`)
- **Icon:** `pengy/assets/icon.svg` — SVG penguin, loaded at startup via `QApplication.setWindowIcon`
- Shows in taskbar, alt-tab, and window decorations on X11/XWayland. On native Wayland a `.desktop` file may be needed for taskbar icon.

---

## Dependencies

| Package    | Version    | Purpose                          |
|------------|------------|----------------------------------|
| PySide6    | >= 6.6.0   | Qt6 GUI framework                |
| openai     | >= 1.0.0   | OpenAI-compatible API client     |
| markdown   | >= 3.5     | Markdown to HTML conversion      |
| pygments   | >= 2.17.0  | Syntax highlighting              |
| ddgs       | >= 9.0.0   | DuckDuckGo web search            |

---

## Design Decisions

**Non-streaming:** Full responses render at once. Simpler architecture, no incremental state management.

**Generator protocol:** `LLMClient.chat()` yields tool request or final response dicts. The worker calls `generator.send(confirmation)` to resume after tool confirmation or sudo dialogs, keeping the main thread's event loop free.

**System message templating at send time:** Templates are resolved fresh on every send so `{date}` is always accurate regardless of when the config was saved.

**Sudo via `-S`:** Rather than a PTY (which would handle any interactive prompt but adds significant complexity), the app specifically detects `sudo` in bash commands, prompts for a password via `QInputDialog`, and passes it to `sudo -S`. Covers the common case with minimal added complexity.

**File attachment injection:** Attached files are formatted as fenced code blocks and prepended to the message text before sending. The LLM sees them as part of the user turn, so no special API handling is needed.

**JSON storage:** Human-readable, easy to backup, no database dependencies.

**PySide6 over PyQt6:** LGPL license is more permissive than GPL.
