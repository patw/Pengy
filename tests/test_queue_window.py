"""Window-level queue behaviour: enqueue while busy, release at turn end, park on Stop.

The composer tests cover the widget; `tests/test_message_queue.py` covers the state
machine. This file covers the wiring between them -- the part that decides *when* a
queued message is spent -- by driving a real MainWindow against a temp config with
the LLM client stubbed out (same fixture shape as tests/test_tabs.py).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication

from pengy.ui.main_window import MainWindow, _TabSession


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tmp_cfg():
    from pengy.core.config import set_config_dir

    with tempfile.TemporaryDirectory(prefix="pengy-queuetest-") as cfg_dir:
        set_config_dir(cfg_dir)
        yield Path(cfg_dir)
    set_config_dir(None)


@pytest.fixture
def window(qapp, tmp_cfg, monkeypatch):
    """A fresh MainWindow pointed at a temp config, no real LLM."""
    from pengy.ui import main_window as mw_mod
    monkeypatch.setattr(mw_mod.LLMClient, "chat", lambda *a, **kw: (_ for _ in ()))
    monkeypatch.setattr(mw_mod.LLMClient, "__init__", lambda self, *a, **kw: None)

    w = MainWindow()
    qapp.processEvents()
    yield w
    for cid, session in list(w.open_tabs.items()):
        if session.chat:
            from pengy.core.chat_manager import save_chat
            save_chat(session.chat)
    w.open_tabs.clear()
    while w.tab_widget.count() > 0:
        w.tab_widget.removeTab(0)
    w.close()
    qapp.processEvents()
    w.deleteLater()
    qapp.processEvents()


def _active(window: MainWindow) -> _TabSession:
    session = window._tab_for_chat(window.active_chat_id)
    assert session is not None
    return session


def _busy(window: MainWindow, monkeypatch):
    """Put the active tab in the middle of a turn, with a worker the finish path
    can recognise (mirrors test_tabs.py's fake-worker setup)."""
    from pengy.ui.chat_worker import ChatWorker

    session = _active(window)
    worker = ChatWorker(object(), messages=[])
    session.worker = worker
    session.thinking = True
    window._worker_to_chat[id(worker)] = window.active_chat_id
    # Connect for real: the Stop path disconnects each signal, and a worker that
    # was never connected produces five "failed to disconnect" warnings.
    worker.response.connect(window._on_worker_response)
    worker.error.connect(window._on_worker_error)
    worker.finished.connect(window._on_worker_finished)
    worker.sudo_password_requested.connect(window._on_sudo_password_requested)
    worker.question_requested.connect(window._on_worker_question)
    monkeypatch.setattr(window, "_sender_chat_id", lambda: window.active_chat_id)
    monkeypatch.setattr(window, "sender", lambda: worker)
    return session, worker


def _content_list(session: _TabSession) -> list[str]:
    return [m.get("content") or "" for m in session.chat["messages"]]


# ── Enqueue ───────────────────────────────────────────────────────

def test_typing_while_busy_queues_instead_of_sending(window, monkeypatch):
    session, _ = _busy(window, monkeypatch)
    started = []
    monkeypatch.setattr(window, "_start_turn",
                        lambda s, text, images=None: started.append(text))

    window.send_message("my next thought")

    assert started == []                                  # nothing was sent
    assert len(session.queue) == 1
    assert session.queue.items[0].text == "my next thought"
    # ...and it is NOT in the chat transcript: an unsent message is not history.
    assert "my next thought" not in _content_list(session)


def test_typing_while_busy_shows_the_strip(window, monkeypatch):
    _busy(window, monkeypatch)
    window.send_message("queued text")
    label = window.chat_input._queue_label
    assert not label.isHidden()
    assert "Queued (1)" in label.text()
    assert "queued text" in label.text()


def test_idle_send_starts_a_turn_directly(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_start_turn",
                        lambda s, text, images=None: started.append(text))
    window.send_message("hello")
    assert started == ["hello"]
    assert len(_active(window).queue) == 0


# ── Release ───────────────────────────────────────────────────────

def test_turn_end_releases_the_oldest_queued_message(window, monkeypatch):
    session, _ = _busy(window, monkeypatch)
    started = []
    monkeypatch.setattr(window, "_start_turn",
                        lambda s, text, images=None: started.append(text))
    window.send_message("first queued")
    window.send_message("second queued")
    assert len(session.queue) == 2

    window._on_worker_finished()

    assert started == ["first queued"]      # one per turn, oldest first
    assert len(session.queue) == 1
    assert session.queue.items[0].text == "second queued"


def test_failed_turn_keeps_the_queue(window, monkeypatch):
    session, _ = _busy(window, monkeypatch)
    started = []
    monkeypatch.setattr(window, "_start_turn",
                        lambda s, text, images=None: started.append(text))
    window.send_message("after the failure")

    window._on_worker_error("boom")

    assert started == []                    # not spent on a chat that just failed
    assert len(session.queue) == 1
    assert not window.chat_input._queue_label.isHidden()


def test_stop_parks_the_queue_and_enter_releases_it(window, monkeypatch):
    session, _ = _busy(window, monkeypatch)
    started = []
    monkeypatch.setattr(window, "_start_turn",
                        lambda s, text, images=None: started.append(text))
    window.send_message("parked thought")

    window._stop_worker()
    assert session.queue.held is True
    assert started == []
    assert "held after Stop" in window.chat_input._queue_label.text()

    # Enter on an empty composer is how a parked queue gets going again.
    window._on_empty_submit()
    assert started == ["parked thought"]
    assert session.queue.is_empty()
    assert session.queue.held is False


# ── Recall ────────────────────────────────────────────────────────

def test_recall_up_edits_the_newest_and_down_the_oldest(window, monkeypatch):
    session, _ = _busy(window, monkeypatch)
    monkeypatch.setattr(window, "_start_turn", lambda s, text, images=None: None)
    window.send_message("one")
    window.send_message("two")

    window._recall_queued(-1)                       # Up
    assert window.chat_input._edit.toPlainText() == "two"
    assert len(session.queue) == 1

    # Recall refuses while the composer holds a draft: Up/Down are cursor keys then.
    window._recall_queued(-1)
    assert window.chat_input._edit.toPlainText() == "two"
    assert len(session.queue) == 1

    window.chat_input.set_composer_text("")
    window._recall_queued(1)                        # Down
    assert window.chat_input._edit.toPlainText() == "one"
    assert session.queue.is_empty()
