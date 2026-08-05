# Changelog

## v1.5.5 (current)

- Fixed `search_content` tool output limits — wasn't respecting the global snip setting
- `glob` tool now auto-extracts directory prefix from patterns like `~/src/*.rs`
- `QuestionDialog` sizes to fit content instead of scrolling

## v1.5.4

- Fixed `todowrite` and `apply_changes` tool schemas so the LLM generates valid calls
- Added schema-content tests to catch this class of bug automatically

## v1.5.3

- Fixed scrollbar jumping in chat view when new content arrived
- Refreshed the UI with consistent icon set (attach, close, delete, edit, file, image, new chat, play, refresh, save, settings, stop, tasks)

## v1.5.2

- Added `apply_changes` tool — multi-file transactional edits with dry-run diff preview
- Harmonized output limits across tools so `directory_tree` and `read_multiple_files` use the same global cap
- Raised default tool output limit from 50 KB to 250 KB (less context cramping)

## v1.5.1

- Added origin guard for web UI (CSRF/DNS rebinding protection) with `--trusted-host` flag
- Robust CLI argument parsing across all entry points
- Status dot in GUI sidebar shows live connection state

## v1.5.0

- **Three new tools** — `glob` (fast file matching that respects `.gitignore`), `todowrite` (structured task lists for multi-step ops), `ask_user_question` (lets the agent clarify instead of guessing)
- **Tabbed chat** — multiple concurrent sessions, each with its own worker thread
- Proper threading for tool execution — per-tab context means stopping one tab won't kill another

## v1.4.5

- Context management: tool results are now snipped (head + tail) when they exceed the configured limit, preventing context window blowout from large file reads or search results

## v1.4.4

- Chat history rewritten: each session stored as its own file + index.json for faster loading
- HTML render cache turned O(n²) re-renders into O(n) — big speedup for long conversations
- Sidebar performance improvements

## v1.4.3

- UI audit: better confirmation dialog labels, delete confirmations, auto-growing input field, tab completion in CLI, sticky scroll in web UI, narrower API key input, theme system prep

## v1.4.2 – v1.4.1

- Performance: faster chat load and render, cleaned up old hardcoded paths

## v1.4.0

- Configurable LLM timeout setting
- Mobile-friendly web UI
- Default tool timeout bumped from 60s to 300s

## v1.3.13

- Fixed `pengy-cli` single-shot mode — no longer hangs waiting for input after printing the answer

## v1.3.12 – v1.3.11

- Image preprocessing for LLM vision APIs (resize/compress before sending)
- Inline image rendering in web UI when skills generate pictures
- Exponential backoff on 429/529 HTTP status from LLM providers

## v1.3.6 – v1.3.5

- Reasoning traces now displayed for models that emit them
- CLI and Web UX/UI improvements
- Bugfixes for tool call display and orange bubble issue

## v1.3.3 – v1.3.1

- Added `--model`, `--output`, `--config-dir`, `--system`, `--no-browser` CLI flags
- Stop button in web UI
- Many GUI fixes

## v1.3.0

- **Theme system** — light/dark/system modes with accent colours, font scaling
- **Tasks** — reusable prompt templates with `%placeholder%` tokens
- Font scaling fixes

## v1.2.x

- Reasoning effort and reasoning history options for compatible models
- GitHub Actions CI/CD and automated PyPI releases
- Initial cross-edition interop documentation
