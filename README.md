# Pengy 🐧

A lightweight Qt6 desktop app for chatting with LLMs via any OpenAI-compatible API, with built-in tools that let the model operate on your local machine.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![PySide6](https://img.shields.io/badge/GUI-PySide6-brightgreen)

## Features

- **OpenAI-compatible** — Works with OpenAI, Ollama, vLLM, LM Studio, OpenRouter, Groq, or any local endpoint
- **Built-in tools** — File read/write, bash execution (with sudo support), Python execution, DuckDuckGo web search, file download, and URL fetching
- **File attachments** — Attach text files directly from the input bar; contents are injected into your message
- **Tool confirmation** — Approve or decline every tool call; YOLO mode skips confirmation for power users
- **Markdown rendering** — Full markdown with syntax-highlighted code blocks
- **Templated system message** — Auto-fills `{date}`, `{username}`, `{hostname}`, and `{osinfo}` at send time
- **Multi-session** — Create, switch, and delete chat sessions; history saved locally as JSON
- **Penguin icon** — Shows as "Pengy" with a proper icon in the taskbar (not "python3")

## Screenshot

![Pengy Interface](screenshot.png)

## Installation

**Requirements:** Python 3.10+

### pip

```bash
pip install -r requirements.txt
./run_pengy.sh
```

### uv

```bash
uv venv
uv pip install -r requirements.txt
./run_pengy.sh
```

Or run directly without the script:

```bash
# pip
python pengy/main.py

# uv
uv run python pengy/main.py
```

## Configuration

Click **⚙ Settings** in the sidebar:

| Field          | Description                                              |
|----------------|----------------------------------------------------------|
| Base URL       | API endpoint, e.g. `http://localhost:11434/v1` for Ollama|
| API Key        | Your API key (or anything for local endpoints)           |
| Model          | Model name, e.g. `gpt-4o`, `llama3`, `gemma`            |
| System Message | Supports `{date}`, `{username}`, `{hostname}`, `{osinfo}`|
| YOLO Mode      | Skip tool confirmation dialogs                           |
| UI Scale       | 75 / 100 / 125 / 200 % — takes effect on next launch    |

Settings and chat history are saved to `~/.config/pengy/`.

## Tools

The LLM can call these tools (confirmation dialog shown unless YOLO mode is on):

| Tool            | Description                                                      |
|-----------------|------------------------------------------------------------------|
| `read_file`     | Read a local file                                                |
| `write_file`    | Write content to a local file                                    |
| `run_bash`      | Run a bash command (60s timeout); prompts for sudo password in UI|
| `run_python`    | Execute Python code (30s timeout)                                |
| `web_search`    | Search the web via DuckDuckGo                                    |
| `download_file` | Download a URL to `~/Downloads/`                                 |
| `fetch_url`     | Fetch a URL's text content into context (useful for docs)        |

## File Attachments

Click **📎** in the input bar to attach a text file. Supported: `.py`, `.md`, `.json`, `.yaml`, `.toml`, `.ini`, `.cfg`, `.sh`, `.txt`, `.csv`, `.sql`, and more. Binary files are rejected. Attached files appear as chips above the input and are injected as fenced code blocks when you send.

## Project Structure

```
pengy/
├── main.py                 # Entry point
├── assets/
│   └── icon.svg            # App icon
├── core/
│   ├── config.py           # Settings load/save + system message templating
│   ├── chat_manager.py     # Chat session CRUD
│   ├── llm_client.py       # API client (generator protocol for tool handling)
│   └── tools.py            # Tool definitions and execution
└── ui/
    ├── main_window.py      # Main window; wires all signals
    ├── chat_history.py     # Sidebar chat list + quick settings
    ├── chat_view.py        # Markdown chat renderer
    ├── chat_input.py       # Input field + file attachment
    ├── chat_worker.py      # Background thread driving the LLM generator
    └── settings_dialog.py  # Settings dialog
```

## API Compatibility

| Service     | Base URL                         |
|-------------|----------------------------------|
| OpenAI      | `https://api.openai.com/v1`      |
| Ollama      | `http://localhost:11434/v1`      |
| LM Studio   | `http://localhost:1234/v1`       |
| vLLM        | `http://localhost:8000/v1`       |
| OpenRouter  | `https://openrouter.ai/api/v1`   |
| Groq        | `https://api.groq.com/openai/v1` |

## Dependencies

| Package  | Purpose                     |
|----------|-----------------------------|
| PySide6  | Qt6 GUI framework           |
| openai   | OpenAI-compatible API client|
| markdown | Markdown rendering          |
| pygments | Syntax highlighting         |
| ddgs     | DuckDuckGo web search       |
