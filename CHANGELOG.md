# Changelog

## v1.6.2 (current)

- **Persistent model list and per-tab model selection (desktop GUI):** the sidebar
  "Model:" field is now an editable dropdown, pre-populated from a persistent model
  list cached in `~/.config/pengy/models_cache.json` (shared across the Python, Rust
  and C++ editions). Each chat tab remembers its own model — stored on the chat
  record — overriding the global default, and the dropdown follows the active tab.
  Settings → Fetch refreshes and re-persists the list; a hint appears under the
  dropdown when no cache has been fetched yet.

## v1.6.1

- **Qt local-image rendering fix:** raw HTML `<img>` tags now render canonical
  `file:///…` local URLs correctly in the desktop chat view. The loader also
  accepts absolute local paths emitted by models.

- **Tooling updates:**
  - `download_file` now streams directly to a configurable directory (default
    `~/Downloads`), returns the saved path and byte size, overwrites same-name
    files, supports per-call `max_size_mb` limits (`0` = unlimited), and uses a
    120-second no-data stall timeout so large transfers can finish.
  - `fetch_url` and `read_multiple_files` now follow the configured global tool
    output limit; `fetch_url` also accepts a `max_chars` override.
  - `run_bash` and `run_python` accept an optional `cwd` working directory.
  - `search_content` matches literal text by default; pass `regex=true` for
    regular-expression searches. Tool descriptions now document their limits,
    safety behavior, and argument semantics more precisely.
- **Tool defaults and controls:** tool execution now defaults to 300 seconds
  (matching the documented setting), and the new `download_max_mb` setting
  controls the default download cap (100 MB by default, `0` = unlimited).

## v1.6.0

- **New `read_image` tool** — the agent can inspect local images (screenshots,
  photos, diagrams, charts, rendered plots) and attach them to the conversation
  so vision-capable models can describe what they show.
  - Images are decoded, preprocessed (resize/compress to configurable limits:
    `image_max_dimension`, `image_max_mb`, `image_quality`), and base64-encoded
  - Parked on `ToolContext` (not the tool return value) because OpenAI-compatible
    APIs only accept string content in `role: "tool"` messages
  - Attached as a follow-up user message with `image_url` parts after the tool
    loop completes
  - Added to `READONLY_TOOLS` safe-list for auto-approval in "safe" mode
  - Limits shared via `set_image_limits()` across all frontends
  - Full test coverage: image attachment, error handling, LLM loop integration
- **Graceful degradation for text-only models**: if the API returns HTTP 400
  because the model doesn't support vision inputs, the `image_url` parts are
  automatically stripped from all messages, a clarifying note is appended
  (`"[This AI model does not support image/vision inputs…]"`), and the
  conversation retries without the image — instead of crashing with a raw
  "API error (HTTP 400)" that kills the chat. Implemented in all three editions
  (Python, C++, Rust).
- **Fix: tool output truncation now cuts on line boundaries and separates file
  reads from command output**:
  - `read_file` / `read_multiple_files`: truncate from the head only
    (contiguous, no middle gap) — the head has imports/declarations, the rest
    can be paged via `offset`
  - `run_bash` / `run_python`: remain tail-biased (head + tail, middle snipped)
    — command echo at the start, errors at the end, disposable middle
  - Both seams cut on full line boundaries so the model never sees a broken
    half-line fragment it might try to "fix"
  - Whole-file reads that fit within the limit stay bare — no `[Lines X-Y]`
    header to parse
  - Truncated file headers now show the exact continuation offset for easy
    paging
  - Giant single-line files fall back to character-boundary cutting
- **Updated README screenshots** — new settings, templates, and main UI images.

## v1.5.9

- Fix web SSE reconnect race: events are now stored in an append-only log with
  monotonic IDs and `Last-Event-ID` resume, so a dropped connection can no
  longer lose the `final_response` and leave the UI stuck on "Thinking…".
- Mobile web layout fixes: removed the double-counted safe-area padding that
  created a gap below the input bar, allowed body scroll so Firefox Android can
  bring the focused input above the keyboard, and added a focus handler that
  forces the prompt into view when the on-screen keyboard appears.

## v1.5.7

- `run_bash` now authenticates sudo via `SUDO_ASKPASS` instead of piping the password to stdin — fixes sudo in pipelines (`echo x | sudo tee f`), with redirected stdin, after a command that reads stdin, and for the second and later `sudo` in one command
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
