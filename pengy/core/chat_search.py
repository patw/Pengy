"""Search chat history: case-insensitive word matching, newest first.

No index and no scoring -- a scan of the per-chat files, cheap enough on Enter
(measured ~0.5s for 360 chats / 112MB) but not per keystroke, so frontends
filter titles live and run this on Enter, off the UI thread.

Rules (shared with the Rust and C++ editions, see spec.md "Chat Search"):

* The query splits on whitespace into words; "double quotes" keep a phrase as
  one word.  Matching is ``str.lower()`` substring matching.
* A chat matches when every word appears somewhere in it -- its title, or the
  text of its user and assistant messages (words may be in different
  messages).  Tool calls and tool output are not searched: they are mostly
  file contents and command output, and grepping them buried the real hits.
* Chats whose *title* holds every word come first (from the index, no file
  read), then the rest newest first, each with up to MAX_HITS snippets.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Iterator

from pengy.core import chat_manager

MAX_RESULTS = 100
MAX_HITS = 3
SNIPPET_CHARS = 80
_LEAD_CHARS = 30

_TERM_RE = re.compile(r'"([^"]*)"|(\S+)')
_WS_RE = re.compile(r"\s+")
# Markdown markers that only add noise to a one-line snippet.
_MARKUP_RE = re.compile(r"```[\w+-]*|\*\*|__|`")


def parse_query(query: str) -> list[str]:
    """Lowercased search words; a "quoted phrase" is one word."""
    terms = []
    for phrase, word in _TERM_RE.findall(query or ""):
        term = _WS_RE.sub(" ", (phrase or word).strip()).lower()
        if term and term not in terms:
            terms.append(term)
    return terms


def text_of(content) -> str:
    """The text of a message's content: a string, or the text parts of a list."""
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def _could_match(raw: bytes, terms: list[str]) -> bool:
    """Cheap pre-check on the file's bytes, before paying for a JSON parse.

    Only plain-ASCII words without JSON-escaped characters are checked: the
    Python edition writes non-ASCII as \\uXXXX escapes, and quotes/backslashes
    are escaped by every edition, so those can only be decided after parsing.
    """
    lowered = raw.lower()
    for term in terms:
        if term.isascii() and '"' not in term and "\\" not in term:
            if term.encode() not in lowered:
                return False
    return True


def make_snippet(text: str, terms: list[str]) -> tuple[str, list[list[int]]]:
    """~SNIPPET_CHARS of *text* around the first matching word, Markdown
    markers stripped and whitespace collapsed, plus [start, end) spans of every
    word inside it (for bolding)."""
    flat = _WS_RE.sub(" ", _MARKUP_RE.sub("", text)).strip()
    lowered = flat.lower()
    # lower() can change a string's length (e.g. "İ"); positions are then
    # unreliable, so fall back to the start of the text.
    positions = [lowered.find(t) for t in terms] if len(lowered) == len(flat) else []
    found = [p for p in positions if p >= 0]
    first = min(found) if found else 0
    start = max(0, first - _LEAD_CHARS)
    end = min(len(flat), start + SNIPPET_CHARS)
    start = max(0, min(start, end - SNIPPET_CHARS))
    snippet = flat[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    spans = []
    low = snippet.lower()
    if len(low) == len(snippet):
        for term in terms:
            i = low.find(term)
            while i >= 0:
                spans.append([i + len(prefix), i + len(prefix) + len(term)])
                i = low.find(term, i + len(term))
    spans.sort()
    return prefix + snippet + suffix, spans


def match_chat(chat: dict, terms: list[str]) -> list[dict] | None:
    """Hits for *chat*, or None if some word appears nowhere in it.

    A hit is ``{"index", "role", "snippet", "spans"}`` -- ``index`` is the
    message's position in ``chat["messages"]`` (for jumping to it).  Messages
    holding more of the words are preferred, then earlier ones.
    """
    remaining = set(terms)
    remaining -= {t for t in terms if t in str(chat.get("title", "")).lower()}
    candidates = []
    for index, msg in enumerate(chat.get("messages", [])):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = text_of(msg.get("content"))
        if not text:
            continue
        lowered = text.lower()
        present = [t for t in terms if t in lowered]
        if present:
            remaining -= set(present)
            candidates.append((-len(present), index, role, text))
    if remaining:
        return None
    candidates.sort()
    hits = []
    for _, index, role, text in candidates[:MAX_HITS]:
        snippet, spans = make_snippet(text, terms)
        hits.append({"index": index, "role": role, "snippet": snippet, "spans": spans})
    hits.sort(key=lambda h: h["index"])
    return hits


def search_chats(
    query: str,
    *,
    limit: int = MAX_RESULTS,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[dict]:
    """Search every stored chat, yielding events as results are found.

    * ``{"type": "result", "id", "title", "created_at", "title_match", "hits"}``
      -- title matches first (``hits`` empty), then message matches newest first.
    * ``{"type": "progress", "scanned", "total"}`` after each chat file.
    * ``{"type": "done", "results", "truncated", "cancelled"}`` last.

    *cancelled* is polled between chats so a frontend can drop a stale search.
    """
    terms = parse_query(query)
    index = chat_manager.load_index()
    results = 0
    done = {"type": "done", "results": 0, "truncated": False, "cancelled": False}
    if not terms:
        yield done
        return

    title_ids = set()
    for entry in index:
        title = str(entry.get("title", "")).lower()
        if all(t in title for t in terms):
            if results >= limit:
                yield dict(done, results=results, truncated=True)
                return
            title_ids.add(entry["id"])
            results += 1
            yield {"type": "result", "id": entry["id"], "title": entry.get("title", ""),
                   "created_at": entry.get("created_at", ""), "title_match": True, "hits": []}

    candidates = [e for e in index if e["id"] not in title_ids and e.get("msg_count", 1)]
    for scanned, entry in enumerate(candidates, 1):
        if cancelled and cancelled():
            yield dict(done, results=results, cancelled=True)
            return
        try:
            raw = chat_manager._chat_file(entry["id"]).read_bytes()
        except OSError:
            raw = b""
        if raw and _could_match(raw, terms):
            try:
                chat = json.loads(raw)
            except ValueError:
                chat = None
            hits = match_chat(chat, terms) if isinstance(chat, dict) else None
            if hits is not None:
                if results >= limit:
                    yield dict(done, results=results, truncated=True)
                    return
                results += 1
                yield {"type": "result", "id": entry["id"], "title": chat.get("title", ""),
                       "created_at": chat.get("created_at", ""), "title_match": False,
                       "hits": hits}
        yield {"type": "progress", "scanned": scanned, "total": len(candidates)}
    yield dict(done, results=results)
