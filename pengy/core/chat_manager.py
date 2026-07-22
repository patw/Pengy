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


def _chats_path() -> Path:
    """Return path to chats.json in the current config directory."""
    return get_config_dir() / "chats.json"


# ---------------------------------------------------------------------------
# in-memory cache
# ---------------------------------------------------------------------------
# chats.json is a single (potentially large) file that gets fully re-parsed on
# every read. A short-lived process reads it many times per user action
# (startup loads it, then get_chat re-loads it; the web chat view loads it
# twice per request). We cache the parsed list keyed by the file's
# (mtime_ns, size); any external writer (the CLI, or the Rust/C++ editions
# sharing ~/.config/pengy/) bumps mtime and transparently invalidates us.
_cache_lock = threading.Lock()
_cache_key: tuple[int, int] | None = None   # (st_mtime_ns, st_size)
_cache_chats: list[dict] | None = None


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


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


def _safe_json_load(path: Path) -> list | None:
    """Load JSON array from *path*, returning None if missing or corrupt.

    A corrupt file is renamed to *.corrupt-timestamp* so data can be
    recovered manually.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise json.JSONDecodeError("Expected a JSON array", "", 0)
        return data
    except (json.JSONDecodeError, OSError):
        import time
        backup = path.with_name(
            f"{path.name}.corrupt-{int(time.time())}"
        )
        try:
            shutil.move(str(path), str(backup))
        except OSError:
            pass
        return None


def load_chats() -> list[dict]:
    """Load all chat sessions from JSON file, with corruption recovery.

    Returns a shallow copy of the cached list, so callers may freely
    insert/remove/replace entries without disturbing the cache. The parse is
    skipped entirely when the file is unchanged since the last read or write.
    """
    global _cache_key, _cache_chats
    path = _chats_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with _cache_lock:
        key = _stat_key(path)
        if key is not None and key == _cache_key and _cache_chats is not None:
            return list(_cache_chats)

        data = _safe_json_load(path)
        if data is None:
            data = []
        # _safe_json_load may have moved a corrupt file aside; re-stat so the
        # cache key reflects whatever is on disk now.
        _cache_chats = data
        _cache_key = _stat_key(path)
        return list(data)


def save_chats(chats: list[dict]) -> None:
    """Save all chat sessions to JSON file atomically."""
    global _cache_key, _cache_chats
    path = _chats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock:
        _atomic_write(path, chats)
        # Prime the cache with what we just wrote so the next load_chats() (e.g.
        # the load→mutate→save cycle in save_chat) skips a re-parse.
        _cache_chats = list(chats)
        _cache_key = _stat_key(path)


def create_chat() -> dict:
    """Create a new chat session."""
    chat = {
        "id": str(uuid.uuid4()),
        "title": "New Chat",
        "messages": [],
        "created_at": datetime.now().isoformat(),
    }
    chats = load_chats()
    chats.insert(0, chat)
    save_chats(chats)
    return chat


def delete_chat(chat_id: str) -> None:
    """Delete a chat session."""
    chats = load_chats()
    chats = [c for c in chats if c["id"] != chat_id]
    save_chats(chats)


def save_chat(chat: dict) -> None:
    """Save a single chat session."""
    chats = load_chats()
    for i, c in enumerate(chats):
        if c["id"] == chat["id"]:
            chats[i] = chat
            save_chats(chats)
            return
    chats.insert(0, chat)
    save_chats(chats)


def get_chat(chat_id: str) -> dict | None:
    """Get a chat session by ID."""
    chats = load_chats()
    for c in chats:
        if c["id"] == chat_id:
            return c
    return None


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
