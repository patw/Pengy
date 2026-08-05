# FAQ

## General

### What's the difference between Pengy, PengyR, and PengyCPP?

They're three implementations of the same agent, sharing the same config, chat history, and skills.

| Edition | Language | Best for |
|---------|----------|----------|
| **Pengy** | Python | Hacking on the code, quick experiments, reference |
| **PengyR** | Rust + Qt6 | A single ~13 MB statically-linked binary, higher performance |
| **PengyCPP** | C++17 + Qt6 | Smallest memory footprint, zero dependencies beyond Qt6 |

All three can be used side by side — chats created in one load in the others.

### Which models does Pengy work with?

Any OpenAI-compatible API. See [docs/api-compatibility.md](api-compatibility.md) for a full list of providers and tested models.

### Can I use Pengy offline?

Yes, with a local model. Ollama, LM Studio, or vLLM running on the same machine works great. Point Pengy's `base_url` to your local endpoint and you're set.

### Does Pengy phone home?

No. Pengy connects only to the API endpoint you configure. No telemetry, no accounts, no cloud.

## Setup

### How do I switch models?

**GUI:** Click ⚙ → change the Model field → click Fetch Models to see what's available.  
**CLI:** `/model <name>`.  
**Web:** Click ⚙ → change the Model field.

### Where is my config stored?

`~/.config/pengy/settings.json` (Linux), `~/Library/Application Support/pengy/` (macOS), or `%APPDATA%\pengy\` (Windows).

### How do I reset everything?

```bash
rm -rf ~/.config/pengy
```

This deletes settings, chat history, and tasks. Start fresh.

## Tools

### Why does the LLM use `run_bash` with `find` instead of `glob`?

The LLM doesn't know about `glob` unless it reads the tool list. Pengy's system message tells it to prefer skills over tools, but the model's built-in behaviour often defaults to `find`/`ls`. If you see this, remind it: *"Use the glob tool instead of run_bash for file searches."*

### What's the difference between `replace_in_file` and `apply_changes`?

`replace_in_file` does one exact-text replacement in one file. `apply_changes` does multiple operations (replace, insert, delete) across multiple files, validates everything in memory first, then writes atomically. Use `replace_in_file` for simple edits, `apply_changes` for sweeping changes across a codebase.

### Can I add my own tools?

Not directly — tools are baked into the Pengy binary/package. But you can achieve the same thing by writing a **skill** (a markdown file + optional script that teaches Pengy how to do something). Skills are more flexible and don't require modifying Pengy itself.

## Skills

### How do skills differ from tools?

**Tools** are built-in functions the LLM can call (read_file, run_bash, etc.). They're always available. **Skills** are custom instructions you write — markdown files that teach Pengy how to do things using its existing tools plus optional scripts. Skills are how you extend Pengy without modifying its code.

### My skill isn't working. What should I check?

1. Is the skill directory in the path Pengy checks? The system message tells Pengy where to look.
2. Does `skill_index.md` have an entry for your skill?
3. Does the `_skill.md` file have clear, specific instructions? Pengy follows the markdown literally.
4. Does the script handle errors gracefully? Pengy sees stderr output.

### Can skills call other skills?

Yes. The `daily_briefing` skill orchestrates the `weather/`, `rss/`, and `news/` skills to produce a morning briefing. Just document the dependency in the `_skill.md` instructions.

## Troubleshooting

### I get 403 errors from the web UI

If you're behind a reverse proxy, you need `--trusted-host`. See [docs/reverse-proxy.md](reverse-proxy.md).

### The GUI crashes on startup

Make sure you have a compatible Qt version (Qt 6.4+). On Linux: `apt install qt6-base-dev`. On macOS: `brew install qt@6`. On Windows: install Qt 6.8+ via the online installer.

### The LLM keeps timing out

Increase `tool_timeout` in settings.json. The default is 300 seconds. Some models are slow, especially local ones.

### My API key isn't working

- Check that `base_url` matches your provider
- Check that the key has the right permissions
- For local endpoints (Ollama, LM Studio), the API key can be anything or empty

### Chat history is getting huge

Pengy automatically elides old tool results to save context window space. You can also use `/compact` (CLI) or start a fresh chat. The `tool_output_max_chars` setting controls how much tool output is kept before snipping.
