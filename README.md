# Pengy 🐧

**A local-first AI agent with tools.** Desktop GUI, web UI, **and** command-line — all backed by the same agent core, talking to any OpenAI-compatible API.

[![PyPI - Version](https://img.shields.io/pypi/v/pengy)](https://pypi.org/project/pengy/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pengy)](https://pypi.org/project/pengy/)
[![PyPI - License](https://img.shields.io/pypi/l/pengy)](https://github.com/patw/pengy/blob/main/LICENSE)

---

## What is Pengy?

Pengy is an LLM agent that runs on your own machine. It connects to OpenAI, Ollama, vLLM, Groq, OpenRouter, or any local endpoint, and gives the model 15 built-in tools to operate on your filesystem, run code, search the web, and more — all with your approval.

Three interfaces, one agent:

| **🐧 Pengy Desktop** | **🐧 Pengy CLI** | **🐧 Pengy Web** |
|---|---|---|
| Qt6 GUI with tabbed chat, markdown rendering, sidebar with history & quick settings, file attachments | Terminal REPL with slash commands, single-shot mode for scripting | Responsive web UI with SSE streaming. Run on a server, use from your phone |

All three share the same core, tools, chat history, and config. Use whichever fits your flow.

---

## Quick Start

### Install

```bash
# Recommended — uv installs Pengy with a compatible Python automatically
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install "pengy[all]"

# Or with pip (Python 3.10+)
pip install "pengy[all]"
```

You can install just the parts you need: `pengy[gui]`, `pengy[cli]`, or `pengy[web]`.

### Desktop GUI

```bash
pengy
```

### CLI (interactive or single-shot)

```bash
pengy-cli
pengy-cli "What is the capital of France?"
```

### Web UI

```bash
pengy-web
```

The web UI is for single-user personal use. For remote access, put it behind nginx with SSL; use `--trusted-host` to set the public hostname when reverse-proxying.

---

## Features

- **OpenAI-compatible** — Works with OpenAI, Ollama, vLLM, LM Studio, OpenRouter, Groq, or any local endpoint
- **15 built-in tools** — Read, write, and edit files; run bash (with sudo support) and Python; search the web and fetch URLs; explore directories, glob files, and search code; track multi-step ops with structured to-do lists; ask clarifying questions when instructions are vague
- **Agentic workflow** — The LLM chains multiple tool calls per turn, piping results from one into the next
- **Tool confirmation** — Three modes: auto-approve everything, auto-approve read-only tools only, or confirm every call
- **Tabbed chat** — Multiple concurrent chat sessions, each with its own worker thread
- **Theme system** — System/light/dark modes plus 8 accent colours; fonts scale with the UI
- **Tasks** — Reusable prompt templates with `%placeholder%` tokens for workflows you run on repeat
- **Model discovery** — Fetch available models from your endpoint with one click or `/models`
- **File attachments** — GUI: attach from the input bar; CLI: `/attach` or `@path` syntax
- **Templated system message** — Auto-fills `{date}`, `{username}`, `{hostname}`, `{osinfo}` at send time
- **Persistent config** — Settings, task templates, and chat history in `~/.config/pengy/`, shared between all interfaces and across all editions (Python, Rust, C++)

---

## Screenshots

| Main chat UI | Settings / theme controls | Tasks templates |
|---|---|---|
| ![Pengy main chat UI](pengyui.png) | ![Pengy settings and theme controls](pengyconfig.png) | ![Pengy tasks template manager](pengytasks.png) |

---

## Configuration

**Desktop:** Click ⚙ Settings in the sidebar.  
**CLI:** Run `/config` to view, `/model <name>` to switch models.  
**Web:** Click ⚙ in the top-right navbar.

| Setting | Description |
|---------|-------------|
| Base URL | API endpoint (e.g. `http://localhost:11434/v1` for Ollama) |
| API Key | Your API key (or anything for local endpoints) |
| Model | Model name, e.g. `gpt-4o`, `llama3`, `gemma` |
| System Message | Supports `{date}`, `{username}`, `{hostname}`, `{osinfo}` placeholders |
| Tool Confirmation | All / Safe / None — controls which tools require approval |
| Theme Mode (GUI) | System / Light / Dark — follows OS palette |
| Accent Color (GUI) | Default, Blue, Teal, Green, Orange, Red, Pink, or Purple |
| UI Scale (GUI) | 75–200% — restart for full native-widget scaling |

---

## Tasks

Tasks are reusable prompt templates for workflows you repeat often — summarizing a YouTube video, drafting a release note, or running a code-review checklist. Open **Tasks** from the desktop sidebar to create, edit, delete, or play templates.

Use `%placeholder%` tokens anywhere in the template to ask for values when the task is played:

```text
Summarize this YouTube video: %Youtube Video URL%
Always use the youtube transcription skill.
```

When you hit **▶ Play**, Pengy collects each placeholder once, renders the full prompt, and sends it through the normal chat pipeline — tools, skills, history, and confirmation settings all work exactly like a hand-typed prompt. Tasks live in `~/.config/pengy/tasks.json`, shared across all interfaces and editions.

---

## Tools

Pengy gives the LLM these tools to operate on your machine:

| Tool | Description |
|------|-------------|
| `read_file` / `read_multiple_files` | Read one or more files at once |
| `write_file` | Write or overwrite a file |
| `replace_in_file` | Targeted text replacement (safer than full rewrites) |
| `apply_changes` | Multi-file transactional edits with diff preview |
| `run_bash` | Execute shell commands (configurable timeout; sudo support) |
| `run_python` | Execute Python code |
| `web_search` | DuckDuckGo web search |
| `download_file` | Download a URL to `~/Downloads/` |
| `fetch_url` | Fetch a URL's text content into context |
| `directory_tree` | Visual directory structure listing |
| `search_content` | Regex search across files in a codebase |
| `glob` | File pattern matching — respects `.gitignore`-style skips |
| `todowrite` | Structured task list for tracking multi-step operations |
| `ask_user_question` | Multi-choice questions to clarify vague requests |

---

## Skills

The 15 built-in tools cover the basics, but Pengy is designed to be extended with **skills** — your own custom instructions and scripts stored as plain markdown files.

A skill is just a `skillname/skillname_skill.md` file with instructions Pengy can read, optionally backed by a bash or Python script. No SDK, no manifest, no packaging — point Pengy at a directory and it figures out the rest.

This means your Pengy can do whatever you need it to:
- Fetch weather from an API
- Control devices on your home network
- Query your local databases
- Generate reports from your own data
- Run system administration tasks
- Send notifications, emails, or messages
- Map repository structure and run test suites
- Anything you can describe in a prompt and a script

Skills are also self-authoring — ask Pengy to create one for you, and it writes the markdown, writes the script, and updates the index, all in one conversation.

**📖 Read the full guide:** [`skills/README.md`](skills/README.md) — covers the philosophy, how skills work, 4 complete examples, and how to make your own.

---

## API Compatibility

| Service | Base URL |
|---------|----------|
| OpenAI | `https://api.openai.com/v1` |
| Ollama | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |
| vLLM | `http://localhost:8000/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Groq | `https://api.groq.com/openai/v1` |

---

## Development

### Project structure

```
pengy/
├── main.py              # Desktop GUI entry point
├── cli/                 # CLI entry point
├── core/                # Config, chat manager, tools, LLM client
├── ui/                  # Chat view, input, workers, settings, theme
└── web/                 # Flask app, routes, SSE, templates
```

### Install from source

```bash
git clone https://github.com/patw/pengy.git
cd pengy
uv sync --extra all
```

Or with pip:

```bash
pip install -e ".[all]"
```

### Running tests

```bash
python -m pytest tests/ -v
```

### Dependencies

| Package | Purpose |
|---------|---------|
| PySide6 | Qt6 GUI framework |
| flask | Web UI framework |
| openai | OpenAI-compatible API client |
| markdown | Markdown rendering (GUI + Web) |
| pygments | Syntax highlighting (GUI + Web) |
| ddgs | DuckDuckGo web search |
| rich | CLI formatting (tables, panels, markdown) |

---

## Also Available

Pengy (Python) is the **reference implementation**. Two high-performance ports share the same `~/.config/pengy/` data directory:

| Edition | Language | Notes |
|---------|----------|-------|
| [**Pengy**](https://github.com/patw/Pengy) | Python | Reference implementation — easiest to hack on |
| [**PengyR**](https://github.com/patw/PengyR) | Rust + Qt6 | High-performance native binary, statically-linked core |
| [**PengyCPP**](https://github.com/patw/PengyCPP) | C++17 + Qt6 | Highest performance, smallest memory footprint |

All three offer the same 15 tools, desktop theme controls, reusable task templates, three interfaces (GUI/CLI/Web), and full chat/task interop. PengyR and PengyCPP ship pre-built AppImage, `.deb`, `.dmg`, and `.zip` releases.

---

## Documentation

- [Configuration reference](docs/configuration.md) — all settings.json fields explained
- [Reverse proxy setup](docs/reverse-proxy.md) — nginx, Caddy, SSH tunnels, Docker
- [Skills deep-dive](docs/skills.md) — skill patterns, `~/.secrets`, `uv` dependencies
- [API compatibility](docs/api-compatibility.md) — provider support, model discovery, local endpoints
- [FAQ](docs/faq.md) — common questions and troubleshooting
- [Building from source](docs/building.md) — platform-specific build instructions
- [Changelog](CHANGELOG.md) — version history

## License

MIT
