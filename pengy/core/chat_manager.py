"""Chat history management for Pengy."""
import json
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from pengy.core.config import get_config_dir


# ---------------------------------------------------------------------------
# storage layout
# ---------------------------------------------------------------------------
# Chats live one per file in <config>/chats/<id>.json.
#
# The previous layout was a single <config>/chats.json array, so every save
# rewrote the whole corpus: ~260 ms and 30 MB of churn to append one message,
# and ~700 ms to parse at startup. Per-chat files make both proportional to the
# chat you touched (~0.5 ms to save, ~0.2 ms to open) instead of to everything
# you have ever said.
#
# <config>/chats/index.json caches the sidebar summary (id, title, created_at,
# message count, preview) so listing chats is one small read instead of 185.
# It is a *cache*, never the source of truth: if it is missing, stale, corrupt,
# or loses a race between two frontends, it is rebuilt by scanning the
# directory (~65 ms). The per-chat files are authoritative.
#
# The legacy chats.json is still read, so a machine that switches between the
# Python, Rust and C++ editions doesn't appear to lose history. It is never
# appended to -- it stays as a one-time seed of everything that existed before
# migration. The single exception: delete_chat() removes a deleted chat's
# entry from it too, so a later legacy re-import can't silently resurrect it.

_INDEX_NAME = "index.json"
_INDEX_VERSION = 1
_PREVIEW_CHARS = 200

# Serialises index read-modify-write within one process. Across processes the
# id-set check in _ensure_current() repairs whatever a lost race dropped.
_index_lock = threading.Lock()


def _chats_dir() -> Path:
    """Directory holding one JSON file per chat."""
    return get_config_dir() / "chats"


def _chat_file(chat_id: str) -> Path:
    return _chats_dir() / f"{chat_id}.json"


def _index_path() -> Path:
    return _chats_dir() / _INDEX_NAME


def _legacy_path() -> Path:
    """The pre-split single-file store. Read-only seed; only delete_chat()
    trims entries out of it (so they can't resurrect on a later import)."""
    return get_config_dir() / "chats.json"


def _chats_path() -> Path:
    """Backwards-compatible alias for the legacy store path."""
    return _legacy_path()


# ---------------------------------------------------------------------------
# low-level IO
# ---------------------------------------------------------------------------

def _atomic_write(target: Path, data) -> None:
    """Write *data* as JSON to *target* atomically (via temp + rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup_corrupt(path: Path) -> None:
    """Move a corrupt file aside so its data can be recovered by hand."""
    import time
    backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        shutil.move(str(path), str(backup))
    except OSError:
        pass


def _safe_json_load(path: Path, expect=list):
    """Load JSON from *path*, returning None if missing or corrupt.

    A corrupt file is renamed to *.corrupt-timestamp* so data can be
    recovered manually.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, expect):
            raise json.JSONDecodeError(f"Expected a JSON {expect.__name__}", "", 0)
        return data
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        _backup_corrupt(path)
        return None


def _stat_key(path: Path) -> list | None:
    """(mtime_ns, size) for *path*, or None if it doesn't exist."""
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


# ---------------------------------------------------------------------------
# summaries & index
# ---------------------------------------------------------------------------

def _preview_of(chat: dict) -> str:
    """First user message, truncated -- what /list and the sidebar show."""
    for m in chat.get("messages", []):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            # Multipart (image) content: use the first text part.
            content = next(
                (p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"),
                "",
            )
        return str(content)[:_PREVIEW_CHARS]
    return ""


def _summarize(chat: dict) -> dict:
    """The per-chat record the index caches. All of it is derived."""
    return {
        "id": chat["id"],
        "title": chat.get("title", "Untitled"),
        "created_at": chat.get("created_at", ""),
        "msg_count": len(chat.get("messages", [])),
        "preview": _preview_of(chat),
    }


def _sort_key(entry: dict):
    """Newest first. created_at is unique in practice; id breaks ties."""
    return (entry.get("created_at") or "", entry.get("id") or "")


def _chat_ids_on_disk() -> set[str]:
    """ids of the per-chat files, from one readdir."""
    try:
        return {
            e.name[:-5] for e in os.scandir(_chats_dir())
            if e.is_file() and e.name.endswith(".json") and e.name != _INDEX_NAME
        }
    except OSError:
        return set()


def _scan_chats() -> list[dict]:
    """Read every per-chat file. The fallback when the index can't be trusted."""
    chats = []
    for chat_id in _chat_ids_on_disk():
        chat = _safe_json_load(_chat_file(chat_id), expect=dict)
        if chat and chat.get("id"):
            chats.append(chat)
    chats.sort(key=_sort_key, reverse=True)
    return chats


def _read_index() -> dict:
    raw = _safe_json_load(_index_path(), expect=dict)
    if not raw or raw.get("version") != _INDEX_VERSION:
        return {}
    return raw


def _write_index(entries: list[dict], legacy_seen) -> None:
    entries = sorted(entries, key=_sort_key, reverse=True)
    _atomic_write(_index_path(), {
        "version": _INDEX_VERSION,
        "legacy_seen": legacy_seen,
        "chats": entries,
    })


def _rebuild_index(legacy_seen) -> list[dict]:
    """Regenerate the index from the authoritative per-chat files."""
    entries = [_summarize(c) for c in _scan_chats()]
    _write_index(entries, legacy_seen)
    return entries


def _import_legacy() -> bool:
    """Copy chats.json entries that have no per-chat file yet.

    Existing per-chat files always win -- this only ever adds. Returns True if
    anything was written.
    """
    legacy = _safe_json_load(_legacy_path(), expect=list)
    if not legacy:
        return False
    have = _chat_ids_on_disk()
    wrote = False
    for chat in legacy:
        if not isinstance(chat, dict) or not chat.get("id"):
            continue
        if chat["id"] in have:
            continue
        _atomic_write(_chat_file(chat["id"]), chat)
        wrote = True
    return wrote


def _ensure_current() -> list[dict]:
    """Bring the index in line with disk, then return its entries.

    Steady state is one readdir plus one small parse. The expensive paths
    (importing chats.json, rescanning every chat) run only when the cheap
    checks say something actually changed.
    """
    with _index_lock:
        _chats_dir().mkdir(parents=True, exist_ok=True)
        raw = _read_index()
        legacy_now = _stat_key(_legacy_path())

        # chats.json appeared or was rewritten -- most likely by the Rust or
        # C++ edition on a machine that runs more than one. Re-import so its
        # chats become visible here.
        if legacy_now is not None and raw.get("legacy_seen") != legacy_now:
            _import_legacy()
            return _rebuild_index(legacy_now)

        entries = raw.get("chats")
        if entries is None:
            return _rebuild_index(legacy_now)

        # The index is a cache: if it disagrees with the directory (a frontend
        # crashed mid-write, or two raced on index.json), rebuild from files.
        if {e.get("id") for e in entries} != _chat_ids_on_disk():
            return _rebuild_index(legacy_now)

        return entries


def _update_index_entry(chat: dict) -> None:
    """Insert or replace one chat's summary without rescanning everything."""
    with _index_lock:
        raw = _read_index()
        entries = raw.get("chats")
        legacy_seen = raw.get("legacy_seen", _stat_key(_legacy_path()))
        if entries is None:
            entries = [_summarize(c) for c in _scan_chats()]
        else:
            entries = [e for e in entries if e.get("id") != chat["id"]]
            entries.append(_summarize(chat))
        _write_index(entries, legacy_seen)


def _drop_index_entry(chat_id: str) -> None:
    with _index_lock:
        raw = _read_index()
        entries = raw.get("chats")
        if entries is None:
            return
        _write_index(
            [e for e in entries if e.get("id") != chat_id],
            raw.get("legacy_seen", _stat_key(_legacy_path())),
        )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def load_index() -> list[dict]:
    """Chat summaries, newest first -- id, title, created_at, msg_count, preview.

    This is what sidebars and /list want. It reads one small file instead of
    every chat, so prefer it over load_chats() wherever message bodies aren't
    actually needed.
    """
    return [dict(e) for e in _ensure_current()]


def load_chats() -> list[dict]:
    """Every chat in full, newest first.

    Only use this when message bodies are genuinely required; it reads every
    chat file. load_index() is ~500x cheaper for listing.
    """
    _ensure_current()
    return _scan_chats()


def save_chats(chats: list[dict]) -> None:
    """Save each of *chats*, leaving every other chat alone.

    Additive on purpose: it writes and updates, but never deletes. The old
    whole-array-rewrite made "save this list" and "delete everything else" the
    same operation, which is a lot of destructive power for a bulk helper.
    Use delete_chat() to remove a chat.
    """
    _chats_dir().mkdir(parents=True, exist_ok=True)
    written = []
    for chat in chats:
        if not isinstance(chat, dict) or not chat.get("id"):
            continue
        _atomic_write(_chat_file(chat["id"]), chat)
        written.append(chat)
    if not written:
        return
    with _index_lock:
        raw = _read_index()
        entries = raw.get("chats")
        legacy_seen = raw.get("legacy_seen", _stat_key(_legacy_path()))
        if entries is None:
            entries = [_summarize(c) for c in _scan_chats()]
        else:
            ids = {c["id"] for c in written}
            entries = [e for e in entries if e.get("id") not in ids]
            entries.extend(_summarize(c) for c in written)
        _write_index(entries, legacy_seen)


def create_chat() -> dict:
    """Create a new chat session."""
    chat = {
        "id": str(uuid.uuid4()),
        "title": "New Chat",
        "messages": [],
        "created_at": datetime.now().isoformat(),
    }
    _ensure_current()
    _atomic_write(_chat_file(chat["id"]), chat)
    _update_index_entry(chat)
    return chat


def delete_chat(chat_id: str) -> None:
    """Delete a chat session.

    Also trims the chat's entry out of the legacy ``chats.json`` seed (if it is
    still there) so a later legacy re-import — triggered by an index rebuild or
    an externally rewritten ``chats.json`` — can't silently resurrect it.
    """
    _ensure_current()
    try:
        _chat_file(chat_id).unlink()
    except OSError:
        pass
    _drop_index_entry(chat_id)
    _remove_from_legacy(chat_id)


def _remove_from_legacy(chat_id: str) -> None:
    """Drop *chat_id* from the legacy ``chats.json`` seed, if present."""
    legacy = _safe_json_load(_legacy_path(), expect=list)
    if not legacy:
        return
    filtered = [
        c for c in legacy
        if not (isinstance(c, dict) and c.get("id") == chat_id)
    ]
    if len(filtered) != len(legacy):
        _atomic_write(_legacy_path(), filtered)


def save_chat(chat: dict) -> None:
    """Save a single chat session -- one small file write, not the whole store."""
    if not chat or not chat.get("id"):
        return
    _chats_dir().mkdir(parents=True, exist_ok=True)
    _atomic_write(_chat_file(chat["id"]), chat)
    _update_index_entry(chat)


def save_chat_progress(chat: dict) -> None:
    """Save an in-flight chat without clobbering out-of-band metadata edits.

    A worker holds its own snapshot of the chat dict for the whole run, so a
    rename that lands mid-run (a different copy, saved by another thread) would
    be overwritten by the worker's next write. Adopt the on-disk title first;
    messages still come from the worker's copy, which is the newer one.
    """
    if not chat or not chat.get("id"):
        return
    on_disk = _safe_json_load(_chat_file(chat["id"]), expect=dict)
    if on_disk:
        title = on_disk.get("title")
        if title and title != chat.get("title"):
            chat["title"] = title
    save_chat(chat)


def get_chat(chat_id: str) -> dict | None:
    """Get a chat session by ID.

    Returns a freshly parsed dict, so callers may mutate it freely -- there is
    no shared cache to poison.
    """
    chat = _safe_json_load(_chat_file(chat_id), expect=dict)
    if chat is not None:
        return chat
    # Not split out yet (first run after upgrade, or written by another
    # edition): fall back to the legacy store.
    _ensure_current()
    return _safe_json_load(_chat_file(chat_id), expect=dict)


# ---------------------------------------------------------------------------
# message helpers
# ---------------------------------------------------------------------------

def clean_dangling_tool_calls(messages: list[dict]) -> list[dict]:
    """Remove or repair dangling/orphan tool messages.

    Handles two corruption cases that cause OpenAI-compatible APIs to 400:
    - assistant tool_calls with no following tool result → synthesizes a cancelled result
    - role: 'tool' messages with no preceding matching tool_calls → dropped

    Returns a *new* list — does not mutate the input.
    """
    cleaned = []
    pending_ids: set[str] = set()  # tool_call IDs declared but not yet satisfied
    i = 0
    while i < len(messages):
        msg = messages[i]
        i += 1

        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id in pending_ids:
                pending_ids.discard(tc_id)
                cleaned.append(msg)
            # else: orphan tool message — drop it
            continue

        cleaned.append(msg)

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tc_ids = {tc["id"] for tc in msg["tool_calls"]}
            pending_ids.update(tc_ids)
            # Consume any tool messages that follow that match these IDs
            while i < len(messages) and messages[i].get("role") == "tool":
                tc_id = messages[i].get("tool_call_id", "")
                if tc_id in pending_ids:
                    pending_ids.discard(tc_id)
                    cleaned.append(messages[i])
                    i += 1
                else:
                    break
            # Synthesize cancelled results for this assistant's unsatisfied IDs
            for missing_id in sorted(tc_ids & pending_ids):
                pending_ids.discard(missing_id)
                cleaned.append({
                    "role": "tool",
                    "tool_call_id": missing_id,
                    "content": "Tool execution was cancelled by user.",
                })

    return cleaned


def elide_old_tool_results(messages: list[dict], keep_turns: int) -> list[dict]:
    """Replace tool-result content in messages older than *keep_turns* turns.

    A "turn" is a user message and all messages until the next user message.
    Tool messages (role: 'tool') older than the last *keep_turns* turns have
    their content replaced with a stub to save context window space.

    Returns a *new* list — does not mutate the input.
    """
    if keep_turns <= 0:
        return list(messages)

    # Find indices of all user messages (turn boundaries)
    user_indices = [
        i for i, msg in enumerate(messages)
        if msg.get("role") == "user"
    ]

    if not user_indices:
        return list(messages)

    # Determine which turns are recent (counting from the end)
    # Each user message starts a turn that extends to just before the next user
    recent_indices: set[int] = set()
    num_turns = len(user_indices)
    for turn_idx in range(num_turns):
        # turn_idx=0 is the first turn, turn_idx=num_turns-1 is the last
        turns_from_end = num_turns - turn_idx
        if turns_from_end > keep_turns:
            continue  # this turn is too old

        start = user_indices[turn_idx]
        end = user_indices[turn_idx + 1] if turn_idx + 1 < num_turns else len(messages)
        for i in range(start, end):
            recent_indices.add(i)

    result = []
    for idx, msg in enumerate(messages):
        if msg.get("role") == "tool" and idx not in recent_indices:
            result.append({
                **msg,
                "content": "[tool output from earlier turn elided]",
            })
        else:
            result.append(msg)
    return result


def redact_last_message(messages: list[dict]) -> list[dict]:
    """Drop the last message and repair any dangling tool_calls it leaves behind.

    One click of "redact" in a frontend maps to one call: it pops exactly one
    raw message off the end -- a tool result, an assistant tool_calls request,
    or a final response.

    Popping a tool result can't just fall through to
    :func:`clean_dangling_tool_calls`: that function *synthesizes* a
    "cancelled" placeholder for any tool_calls entry left unsatisfied, so a
    naive pop-then-repair would regenerate an equivalent stub every time and
    redaction could never advance past it. Instead the popped tool_call_id is
    struck directly from its assistant message's ``tool_calls`` list; if that
    empties the list (and the assistant left no other text), the now-empty
    assistant message is dropped too, so redacting a single-tool-call round
    fully removes it in one click. clean_dangling_tool_calls still runs at the
    end as a defensive pass over whatever repair didn't touch.

    Safe to call repeatedly down to an empty list. Returns a *new* list —
    does not mutate the input.
    """
    if not messages:
        return []
    messages = list(messages)
    last = messages.pop()

    if last.get("role") == "tool":
        # Find the assistant message that declared this tool_call_id — not
        # necessarily the immediately preceding message, since a multi-tool
        # round has several tool results trailing one assistant message.
        for i in range(len(messages) - 1, -1, -1):
            candidate = messages[i]
            if candidate.get("role") != "assistant":
                continue
            ids = {tc.get("id") for tc in candidate.get("tool_calls") or []}
            if last.get("tool_call_id") in ids:
                assistant = dict(candidate)
                remaining = [tc for tc in assistant["tool_calls"]
                             if tc.get("id") != last.get("tool_call_id")]
                if remaining:
                    assistant["tool_calls"] = remaining
                    messages[i] = assistant
                elif assistant.get("content"):
                    assistant = {k: v for k, v in assistant.items() if k != "tool_calls"}
                    messages[i] = assistant
                else:
                    messages.pop(i)
            break

    return clean_dangling_tool_calls(messages)


def add_usage(chat: dict, usage: dict | None) -> dict:
    """Accumulate one turn's token usage into the chat's running total.

    LLMClient.chat() reports usage per turn (reset every call), so without
    this each frontend only ever showed the *last* turn's numbers — no signal
    for how much context a long-running chat has actually burned through.
    Stored on ``chat["usage"]`` (not session/tab state) so the running total
    persists across reloads and is visible from any frontend, not just the
    process that made the call. Mutates and returns ``chat["usage"]``; a
    missing or empty *usage* leaves it unchanged (and creates it, zeroed, if
    absent, so callers always get a dict back).
    """
    totals = chat.setdefault(
        "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    if usage:
        totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
        totals["completion_tokens"] += usage.get("completion_tokens", 0)
        totals["total_tokens"] += usage.get("total_tokens", 0)
    return totals
