# Changelog

## v1.7.3 (current)

- **Safer sudo handling.** `run_bash` now requires explicit `elevated=true` before invoking sudo, ignores sudo mentions in quotes/comments/data, and scopes cached sudo credentials to each web worker. Rust and C++ editions flush tool output before the hidden-password prompt for reliable terminal ordering.


- **Binary output guard.** `_snip_tool_output()` — the shared truncation point
  for `run_bash`, `run_python`, `directory_tree`, `search_content`, and
  `glob` — now runs a `_looks_binary()` heuristic first: a NUL byte anywhere
  in the first 4KB, or a non-printable/control-char ratio over ~25%, blocks
  the output outright with a short diagnostic instead of loading a binary
  blob into context. `run_bash`/`run_python` decode command output leniently
  (`errors="replace"`) instead of strictly, so output that isn't valid UTF-8
  reaches the guard as text instead of raising.
- **Redact last message.** `chat_manager.redact_last_message()` pops exactly
  one raw message off the end of a chat per call, repeatable all the way to
  an empty chat. A popped tool result strikes its id directly from the
  assistant's `tool_calls` rather than falling through to
  `clean_dangling_tool_calls()`'s "cancelled" synthesis, which would
  regenerate an identical stub forever and never let redaction advance.
  Wired as `/redact [N]` in the CLI, a redact button in the Web navbar
  (`POST /chat/<id>/redact`, 409 while a turn is in flight), and a "Redact"
  button in the GUI input row.
- **Tasks in the CLI and Web UI.** Previously GUI-only; `/tasks` and
  `/task <#>` in the CLI, and a Tasks modal (`GET /tasks`, `POST
  /tasks/render`) in the Web UI, both routing the rendered prompt through the
  normal send path.
- **Cumulative token usage.** `chat_manager.add_usage()` accumulates each
  turn's token counts into `chat["usage"]` (persisted, not session-only
  state), so the running total for a chat survives reloads and tab switches
  instead of only ever showing the last turn's numbers. All three frontends
  show it next to the model/tool-confirmation status.
- **GUI: "New Chat" sidebar performance.** Two stacked costs scaling with
  total chat count made "New Chat" visibly slow with more than a couple dozen
  chats: `themed_icon()` rebuilt a 15-pixmap `QIcon` from scratch on every
  call even though every sidebar row requests the same `(name, color)` (fixed
  with an `lru_cache`), and `create_new_chat()` called `load_chat_list()`'s
  full clear-and-rebuild on every click (fixed with
  `ChatHistoryWidget.add_chat()`, a single-row insert). Fixing the full
  rebuild uncovered a real regression: `_close_tab()`/`_load_into_new_tab()`
  delete an abandoned empty "New Chat" from disk but never removed its
  sidebar row, previously masked by the full rebuild that ran right after —
  without it, closing an empty chat and clicking New Chat again left a
  permanent ghost row each time. Fixed with the matching
  `ChatHistoryWidget.remove_chat()`.
- **GUI: fixed a crash on `ask_user_question` and on any cancel.**
  `ChatWorker.__init__` never initialized `_question_event` /
  `_pending_question_response`, even though `run()`, `cancel()`, and
  `send_question_response()` all reference them — any model call to
  `ask_user_question`, or any worker cancellation at all (Stop, closing a tab
  mid-run, sending a new message while one's in flight), crashed with
  `AttributeError: 'ChatWorker' object has no attribute '_question_event'`.
- **GUI: quick-settings whitespace gap.** The "no cached model list" hint
  label was only text-cleared once populated, not hidden — an empty `QLabel`
  still claims a line of layout height, leaving a permanent gap above "Tool
  Confirm:". Now hidden outright when a model list exists.
- **Settings: two more UI scale options.** 110% and 135% added alongside the
  existing 75/100/125/150/175/200% steps.

## v1.7.0

- **Ask the user a question, interactively.** The web UI now surfaces
  `ask_user_question` in an interactive modal showing the model's options and a
  free-text "Other" field, and routes the answers (submit or cancel) back through
  a new `POST /chat/<id>/answer` endpoint. The assistant's preamble narration is
  also streamed live instead of only appearing after a reload.
- **Narration now renders before the tool cards.** The text the model writes
  alongside its tool calls is persisted with the turn but was dropped from the
  live run — and the reload path put it *after* the tool cards, the reverse of
  the order it was written in. All three frontends (CLI, desktop GUI, web) now
  render it live, and the reload path renders it first.
- **Web hardening:** tool cards are de-duplicated on SSE reconnect, and
  attribute content is escaped (`escAttr`) so model-supplied text can't break
  out of `title="…"` and the markup.

## v1.6.4

- **Incremental persistence — a turn reaches disk before it finishes.** Every
  message a run produces (assistant tool calls, tool results, question answers,
  final reply) is written to `chats.json` as it lands instead of only when the
  turn finishes; the user message is persisted up front before the model has
  answered. A crash, cancel, or API error mid-tool-loop used to silently drop
  the whole turn's tool calls while the user message stayed on disk.
- **Mid-run renames are preserved.** The web worker now saves with
  `save_chat_progress()`, which re-reads the on-disk title before each write, so
  a `/rename` landing during a run is no longer clobbered by the worker's stale
  in-memory snapshot.
- **Dangling tool calls are repaired on any run end.** The GUI and CLI now run
  `clean_dangling_tool_calls()` before their last save on every exit path
  (final response, error, cancel, Ctrl-C), synthesizing a placeholder tool
  message for any orphaned assistant `tool_calls` so the next request does not
  HTTP 400.
- New web tests (`TestIncrementalPersistence`) cover crash recovery, question
  answers, and mid-run renames.

## v1.6.3

- **Fix: Stop button left the sidebar status bubble stuck.** Pressing Stop cleared
the tab's thinking/tool-running state but never refreshed the quick-settings
status dot, so the bubble stayed on "Thinking…" (blinking red) or
"Running Tool…" (orange) instead of returning to green "Idle". The Stop handler
now repaints the status bubble, matching the normal completion and error paths.
Fixed in all three editions (Python, C++, Rust).

## v1.6.2

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
