"""Chat history management for Pengy."""
import json
import uuid
from datetime import datetime
from pathlib import Path

CHATS_DIR = Path.home() / ".config" / "pengy"
CHATS_FILE = CHATS_DIR / "chats.json"


def load_chats() -> list[dict]:
    """Load all chat sessions from JSON file."""
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    if CHATS_FILE.exists():
        with open(CHATS_FILE, "r") as f:
            return json.load(f)
    return []


def save_chats(chats: list[dict]) -> None:
    """Save all chat sessions to JSON file."""
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHATS_FILE, "w") as f:
        json.dump(chats, f, indent=2)


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
