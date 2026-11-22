"""Type-ahead captured while a turn runs.

Nothing reads stdin during a turn: the REPL is inside the turn, so the only thing
holding what the user types is the terminal's line discipline. That is invisible
and unsafe -- a prompt raised mid-turn (tool confirmation, question form, sudo
password) reads that buffered text as its answer, so a queued message gets eaten
and a blank line there means "option 1".

So while a turn runs this module owns the file descriptor: a thread selects on
fd 0, completes lines, and the turn loop hands them to the caller, which
announces and queues them. Every prompt calls :func:`suspend` first: that stops
the thread *and* flushes a half-typed line, so a prompt can only see what was
typed at it.

Interactive only. Callers start it around a REPL dispatch, so a single-shot run
(cron, systemd timer, a Qt subagent, ``--output json``) never starts a thread and
never touches the queue. Mirrors ``cli/src/typing.rs`` (Rust) and
``namespace TypeAhead`` (C++).
"""

from __future__ import annotations

import os
import select
import sys
import threading

from pengy.core.message_queue import MessageQueue

_QUEUE = MessageQueue()
_LOCK = threading.Lock()
_PENDING: list[str] = []
_THREAD: threading.Thread | None = None
_ACTIVE = False
_STOP = threading.Event()


def queue() -> MessageQueue:
    """The queue itself: len/take/previews/held are all it exposes."""
    return _QUEUE


def is_tty() -> bool:
    try:
        return os.isatty(0)
    except OSError:
        return False


def _capture_loop() -> None:
    """Read complete lines off fd 0 until stop() is called."""
    partial = ""
    while not _STOP.is_set():
        try:
            ready, _, _ = select.select([0], [], [], 0.1)
        except (OSError, ValueError):
            return
        if not ready:
            continue
        try:
            chunk = os.read(0, 4096)
        except OSError:
            return
        if not chunk:
            return  # EOF
        partial += chunk.decode("utf-8", "replace")
        while "\n" in partial:
            line, partial = partial.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                with _LOCK:
                    _PENDING.append(line)


def start() -> None:
    """Begin capturing. No-op when stdin is not a terminal or already capturing."""
    global _THREAD, _ACTIVE
    if _ACTIVE or not is_tty():
        return
    _ACTIVE = True
    _STOP.clear()
    _THREAD = threading.Thread(target=_capture_loop, name="pengy-typeahead", daemon=True)
    _THREAD.start()


def stop() -> list[str]:
    """Stop capturing, returning whatever had arrived but was not collected."""
    global _THREAD, _ACTIVE
    if not _ACTIVE:
        return []
    _ACTIVE = False
    _STOP.set()
    thread, _THREAD = _THREAD, None
    if thread is not None:
        thread.join(timeout=1.0)
    with _LOCK:
        out = list(_PENDING)
        _PENDING.clear()
    return out


def suspend() -> bool:
    """Stop capturing for a blocking prompt and drop a half-typed line.

    Returns whether capture was active, for :func:`resume`.
    """
    if not _ACTIVE:
        return False
    for line in stop():
        _QUEUE.enqueue(line)
    try:
        import termios

        termios.tcflush(0, termios.TCIFLUSH)
    except (ImportError, OSError, AttributeError):
        pass
    return True


def resume(was_active: bool) -> None:
    if was_active:
        start()


def collect() -> list[str]:
    """Drain captured lines so the caller can announce and enqueue them."""
    with _LOCK:
        out = list(_PENDING)
        _PENDING.clear()
    return out


def enqueue(line: str) -> None:
    _QUEUE.enqueue(line)


def take_oldest() -> str:
    """Oldest queued message as text (what a finished turn sends next)."""
    return _QUEUE.take_oldest().text


def take_newest() -> str:
    """Newest queued message as text (what Up recalls for editing)."""
    return _QUEUE.take_newest().text


def _stderr_is_tty() -> bool:
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def announce(lines: list[str], write=None) -> None:
    """Queue captured lines and say so, one dim line each (stderr by default)."""
    if not lines:
        return
    write = write or sys.stderr.write
    dim, reset = ("\x1b[2m", "\x1b[0m") if _stderr_is_tty() else ("", "")
    for line in lines:
        _QUEUE.enqueue(line)
        first = line.replace("\n", " ")[:60]
        write(f"{dim}  \u21e2 queued {len(_QUEUE)}: {first}{reset}\n")
        sys.stderr.flush()
