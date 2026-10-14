"""F6/F7: a prompt nobody can answer must not strand a run.

Two shapes of the same problem:

- **F7, headless CLI.** With no TTY on stdin (cron, script, CI) a tool call that
  needs confirmation aborted the *whole run* at the first write tool, and the
  model was told "declined by user", which reads as a refusal.  It now tells the
  model the truth -- nobody is available to confirm -- and the run carries on.
- **F6, web worker with no browser.** A confirm/question/sudo prompt waits
  indefinitely (v1.11.4 removed the silent expiry on purpose).  With the tab
  closed, the worker's thread stays alive forever and the chat shows an active
  task forever.  The prompt now has a *lease*: announced each minute while
  nobody is attached, and the run is stopped with a notice when it expires.
  While a browser is attached it still waits indefinitely -- that is the point.
"""
from __future__ import annotations

import json
import threading

import pytest

from pengy.core.llm_client import NO_USER_CONFIRM, LLMClient
from tests.test_llm_loop import collect_auto, completion, tool_call
from tests.test_timeout_retry import Wire


@pytest.fixture
def wire():
    server = Wire()
    yield server
    server.close()


@pytest.fixture
def client(wire):
    return LLMClient(base_url=wire.base_url, api_key="test-key", model="stub-model")


def _drive_with_answer(gen, send_value) -> list:
    """Drive a generator that pauses for confirmation, answering with send_value."""
    events = [next(gen)]                      # assistant_tool_calls
    events.append(next(gen))                 # tool_request
    events.append(gen.send(send_value))      # tool_result
    events.extend(collect_auto(gen))
    return events


# ── F7: the model is told the truth about a headless run ──────────────────────

def test_headless_confirmation_is_not_a_refusal(wire, client):
    """The tool result the model sees must not read as a user declining."""
    wire.queue((200, completion(tool_calls=[
        tool_call("t1", "run_bash", json.dumps({"command": "echo hi"}))])))
    wire.queue((200, completion(content="carried on")))

    events = _drive_with_answer(
        client.chat([{"role": "user", "content": "go"}], tool_confirmation="none"),
        {"confirmed": False, "unavailable": True, "tool_call_id": "t1"})

    result = next(e for e in events if e["type"] == "tool_result")
    assert result["declined"] is True
    assert result["content"] == NO_USER_CONFIRM
    assert "declined by user" not in result["content"]
    # The run continues: the model gets a further turn and answers.
    assert events[-1]["content"] == "carried on"


def test_a_real_decline_still_says_declined(wire, client):
    """Negative control: a user who really declined must not be told 'no user'."""
    wire.queue((200, completion(tool_calls=[
        tool_call("t1", "run_bash", json.dumps({"command": "echo hi"}))])))
    wire.queue((200, completion(content="stopped")))

    events = _drive_with_answer(
        client.chat([{"role": "user", "content": "go"}], tool_confirmation="none"),
        {"confirmed": False, "tool_call_id": "t1"})

    result = next(e for e in events if e["type"] == "tool_result")
    assert result["content"] == "Tool execution was declined by user."


def test_cli_headless_confirmation_returns_unavailable(monkeypatch):
    """The CLI must not abort the run when there is no terminal to ask."""
    import sys
    from pengy.cli.main import PengyCLI

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    cli = PengyCLI(no_save=True)
    cli.config = {**cli.config, "tool_confirmation": "none"}

    confirm = cli._get_tool_confirmation({"name": "run_bash", "tool_call_id": "t1"})
    assert confirm == {"confirmed": False, "unavailable": True, "tool_call_id": "t1"}

    # A write tool is not the end of the run: no abort_run, and one notice only.
    assert "abort_run" not in confirm
    cli._get_tool_confirmation({"name": "run_bash", "tool_call_id": "t2"})
    assert cli._no_user_notice is True


def test_cli_headless_safe_mode_still_runs_readonly(monkeypatch):
    """Negative control: safe mode auto-approves read-only tools with no TTY."""
    import sys
    from pengy.cli.main import PengyCLI

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    cli = PengyCLI(no_save=True)
    cli.config = {**cli.config, "tool_confirmation": "safe"}

    confirm = cli._get_tool_confirmation({"name": "read_file", "tool_call_id": "t1"})
    assert confirm == {"confirmed": True, "tool_call_id": "t1"}


# ── F6: the web prompt lease ──────────────────────────────────────────────────

@pytest.fixture
def worker():
    from pengy.web.app import WebWorker
    return WebWorker({"id": "c1", "title": "T", "messages": []}, {})


def test_prompt_waits_forever_while_a_browser_is_attached(worker, monkeypatch):
    """The v1.11.4 guarantee: a prompt must not expire under a watching user."""
    from pengy.web import app

    monkeypatch.setattr(app, "_PROMPT_LEASE_SECONDS", 0.05)
    monkeypatch.setattr(app, "_PROMPT_POLL_SECONDS", 0.01)
    worker._viewers = 1
    event = threading.Event()

    threading.Timer(0.3, event.set).start()
    worker._await_user(event)          # returns only because the user answered
    assert event.is_set()
    assert worker._cancelled is False
    assert not [e for e in worker._events if e.get("type") == "notice"]


def test_prompt_is_announced_while_nobody_is_attached(worker, monkeypatch):
    from pengy.web import app

    monkeypatch.setattr(app, "_PROMPT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(app, "_PROMPT_NOTICE_SECONDS", 0.05)
    monkeypatch.setattr(app, "_PROMPT_LEASE_SECONDS", 0.3)
    worker._viewers = 0
    event = threading.Event()

    worker._await_user(event)

    notices = [e for e in worker._events if e.get("type") == "notice"]
    assert len(notices) >= 1
    assert "no browser" in notices[0]["message"].lower()
    # The lease ends the run, and says so -- never a silent decline.
    assert worker._cancelled is True
    assert "being stopped" in notices[-1]["message"]


def test_answered_prompt_never_notices(worker, monkeypatch):
    """Negative control: an answered prompt is not a lease event."""
    from pengy.web import app

    monkeypatch.setattr(app, "_PROMPT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(app, "_PROMPT_LEASE_SECONDS", 5.0)
    worker._viewers = 0
    event = threading.Event()
    event.set()

    worker._await_user(event)
    assert not [e for e in worker._events if e.get("type") == "notice"]
    assert worker._cancelled is False


def test_viewer_count_tracks_the_sse_connection(worker):
    """The lease keys on attached viewers, so the counter must not leak."""
    gen = worker.iter_events(start_index=0, timeout=0.01)
    next(gen)
    assert worker._viewers == 1
    gen.close()
    assert worker._viewers == 0
