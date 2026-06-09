"""Chat history management for Pengy."""
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

CHATS_DIR = Path.home() / ".config" / "pengy"
CHATS_FILE = CHATS_DIR / "chats.json"


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
    """Load all chat sessions from JSON file, with corruption recovery."""
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    data = _safe_json_load(CHATS_FILE)
    return data if data is not None else []


def save_chats(chats: list[dict]) -> None:
    """Save all chat sessions to JSON file atomically."""
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(CHATS_FILE, chats)


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
    """Remove or repair dangling assistant tool_calls that lack tool results.

    If an assistant message has tool_calls but the next message is not a
    matching tool-role message, synthesize a 'cancelled' tool result so the
    API won't reject the message list.

    Returns a *new* list — does not mutate the input.
    """
    cleaned = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        cleaned.append(msg)
        i += 1

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tc_ids = {tc["id"] for tc in msg["tool_calls"]}
            # Consume any tool messages that follow that match these IDs
            while i < len(messages) and messages[i].get("role") == "tool":
                tc_id = messages[i].get("tool_call_id", "")
                if tc_id in tc_ids:
                    tc_ids.discard(tc_id)
                    cleaned.append(messages[i])
                    i += 1
                else:
                    break
            # Any unmatched tool_call IDs get a synthetic cancelled result
            for missing_id in sorted(tc_ids):
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
