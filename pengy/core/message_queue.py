"""Per-tab queue of messages typed while a turn is in flight.

Pure logic: no Qt, no chat state, no config -- the window owns one per tab, the
composer renders :meth:`MessageQueue.previews`, and ``tests/test_message_queue.py``
drives it directly.

Nothing here is ever persisted. A message that was never sent must not appear in
``chats.json`` as if it had been -- that is the whole reason this is a separate
type rather than entries in ``chat["messages"]``.

Mirrors ``gui/messagequeue.h`` in the Rust and C++ editions, method for method, so
the three desktop windows behave identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_ELLIPSIS = "\u2026"


@dataclass
class QueuedMessage:
    """One message typed while a turn was running.

    ``text`` is the composer payload exactly as it would have been sent (attached
    text files are already embedded by ``ChatInputWidget._on_submit``); ``images``
    are the composer's remaining attachments, imported when the message is sent.
    """

    text: str = ""
    images: list = field(default_factory=list)


class MessageQueue:
    """FIFO of pending messages, with the recall order the composer needs."""

    def __init__(self) -> None:
        self._items: list[QueuedMessage] = []
        self._held = False

    # ── Adding ────────────────────────────────────────────────────
    def enqueue(self, text: str, images: list | None = None) -> None:
        """Append. Empty (no text, no images) is ignored so a stray Enter cannot
        create a phantom queue entry."""
        if not text.strip() and not images:
            return
        self._items.append(QueuedMessage(text, list(images or [])))

    # ── Inspecting ────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._items)

    def is_empty(self) -> bool:
        return not self._items

    @property
    def items(self) -> list[QueuedMessage]:
        """Oldest first. A copy: callers render it, they do not mutate it."""
        return list(self._items)

    @property
    def held(self) -> bool:
        """Parked because the user pressed Stop: a held queue is not drained when
        a turn ends (there is no turn running any more), and the strip says so."""
        return self._held

    @held.setter
    def held(self, value: bool) -> None:
        self._held = bool(value)

    # ── Draining ──────────────────────────────────────────────────
    def take_head(self) -> QueuedMessage:
        """Remove and return the oldest: what a finished turn sends next."""
        return self._items.pop(0) if self._items else QueuedMessage()

    def take_newest(self) -> QueuedMessage:
        """Remove and return the newest: what Up recalls for editing."""
        return self._items.pop() if self._items else QueuedMessage()

    def take_oldest(self) -> QueuedMessage:
        """Remove and return the oldest, for editing with Down."""
        return self.take_head()

    def clear(self) -> None:
        self._items.clear()
        self._held = False

    # ── Rendering ─────────────────────────────────────────────────
    def previews(self, max_chars: int = 70) -> list[str]:
        """One single-line, elided summary per item, for the composer's strip."""
        out: list[str] = []
        for item in self._items:
            stripped = item.text.strip()
            text = stripped.splitlines()[0].strip() if stripped else ""
            if not text:
                count = len(item.images)
                text = "[1 image]" if count == 1 else f"[{count} images]"
            if max_chars > 1 and len(text) > max_chars:
                text = text[: max_chars - 1] + _ELLIPSIS
            out.append(text)
        return out
