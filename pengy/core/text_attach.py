"""Size limits for text files attached to a prompt.

Attached text goes into the prompt verbatim, so its size is paid for in context:
a file that fits the window is a normal turn, a file that doesn't is a context-
limit error, and reactive recovery then spends up to 4 summarisation attempts
trying to make a prompt that was never going to fit.

The limit is derived from the model's own context window, which Pengy already
tracks per model (see ``config.compaction_policy``).  Bytes-to-tokens is not a
constant across tokenizers, so the conversion is deliberately pessimistic:
measured against the fleet's own engine (Swift 1.5 on basalt), 40 KB of prose
tokenised at 4.13 bytes/token and 40 KB of Python source at 3.97, and denser
tokenizers exist.  3.5 bytes/token is the number used here, so a "safe" file
stays safe on a tokenizer 15% hungrier than the measured ones.

Measured against the fleet's default 256K window, a 95%-of-context attachment
(243K tokens, ~950 KB) leaves ~13K tokens for the system prompt, the tool
definitions, the chat history and the reply.  That is not a usable turn, it is a
guaranteed context-limit error, so a single file gets a quarter of the window
and the rest of the request gets the other three quarters.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pengy.core.config import compaction_policy, load_config

#: Conservative: measured 3.97 (source) to 4.13 (prose) bytes/token.
BYTES_PER_TOKEN = 3.5
#: Share of the model's context window one attached file may take.
CONTEXT_SHARE = 0.25
#: Context window assumed when the model's is unknown (``context_window: 0``).
UNKNOWN_CONTEXT_TOKENS = 256_000
#: Above this a file is not prompt input at all: refuse it at attach time.
MAX_FILE_BYTES = 1024 * 1024
#: Floor and ceiling on the per-file embed budget.
MIN_EMBED_BYTES = 32 * 1024
MAX_EMBED_BYTES = 1024 * 1024
#: One message may carry several files; the total is this many per-file budgets.
TOTAL_BUDGETS = 2


def context_tokens_for(config: dict[str, Any], model: str) -> int:
    """The context window Pengy has for *model*, falling back to 256K."""
    _, window = compaction_policy(config, model)
    try:
        window = int(window)
    except (TypeError, ValueError):
        window = 0
    return window if window > 0 else UNKNOWN_CONTEXT_TOKENS


def embed_budget_bytes(context_tokens: int) -> int:
    """Bytes one attached text file may contribute to the prompt."""
    budget = int(context_tokens * BYTES_PER_TOKEN * CONTEXT_SHARE)
    return max(MIN_EMBED_BYTES, min(MAX_EMBED_BYTES, budget))


def budget_for_model(model: str = "", config: dict[str, Any] | None = None) -> int:
    """Per-file budget for the model in play, reading settings when not given."""
    cfg = config if config is not None else load_config()
    return embed_budget_bytes(context_tokens_for(cfg, model or cfg.get("model", "")))


def total_budget_bytes(context_tokens: int) -> int:
    """Budget for all text attachments in a single message."""
    return embed_budget_bytes(context_tokens) * TOTAL_BUDGETS


def oversized_reason(size_bytes: int) -> str | None:
    """Why a file of *size_bytes* is refused, or None if it is acceptable."""
    if size_bytes <= MAX_FILE_BYTES:
        return None
    return (f"{size_bytes / (1024 * 1024):.1f} MB is over the "
            f"{MAX_FILE_BYTES // 1024} KB attachment limit")


def read_text_attachment_bytes(data: bytes, budget: int) -> tuple[str, str]:
    """Same cap as :func:`read_text_attachment`, for bytes already in hand."""
    if len(data) <= budget:
        return data.decode("utf-8", errors="replace"), ""
    head = data[:budget].decode("utf-8", errors="replace")
    note = (f"\n[... {budget:,} of {len(data):,} bytes embedded; "
            f"the rest is truncated, not omitted silently]")
    return head, note


def read_text_attachment(path: Path, budget: int) -> tuple[str, str]:
    """Read *path* for embedding, returning ``(content, trailing_note)``.

    Content is capped at *budget* bytes (cut on a byte boundary, then decoded
    with replacements), and the note says so in the message: a truncated file
    must announce itself, because the model is being asked about less than the
    file the user attached.
    """
    return read_text_attachment_bytes(Path(path).read_bytes(), budget)
