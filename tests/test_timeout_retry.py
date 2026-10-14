"""Provider-timeout auto-retry: 408/504 answers and the SDK's own deadline.

Pengy asks for the whole reply in one non-streaming request, so a timeout is the
failure most often caused by a busy endpoint rather than a bad request -- and
until now it was the one failure with no recovery: the OpenAI client is built
with ``max_retries=0`` and only 429/529 were retried, so a single timeout ended
the turn.

These tests pin the new contract:

- 408 and 504 answers are retried with the same backoff ladder as 429/529.
- ``APITimeoutError`` (our own ``llm_timeout`` deadline, no HTTP status) is retried.
- The budget is ``_MAX_TIMEOUT_RETRIES`` per request, then the ordinary timeout
  message is raised unchanged -- the retry is invisible on success, and the
  give-up text is the one the user already knows.
- The context-summary request gets the same budget.
- Negative controls: connection refused, 400 and 401 are NOT retried, and 429
  keeps its own 5-attempt "Overloaded" budget.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from openai import APITimeoutError, APIStatusError

import openai.resources.chat.completions as completions_resource
from pengy.core.llm_client import (
    LLMClient, _MAX_TIMEOUT_RETRIES, _TIMEOUT_STATUSES, _is_timeout_error,
)
from tests.test_llm_loop import collect_auto, completion


TIMEOUT_BODY = {"error": {"message": "Request timed out", "type": "timeout"}}
OVERFLOW_BODY = {"error": {"message": "This model's maximum context length is 1024 tokens",
                           "code": "context_length_exceeded"}}


# ── Wire fixture: queued (status, body, headers), with a repeat tail ──────────

class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        server = self.server
        length = int(self.headers.get("Content-Length", 0))
        server.requests.append(json.loads(self.rfile.read(length) or b"{}"))
        if server.replies:
            status, body, headers = server.replies.pop(0)
        elif server.repeat is not None:
            status, body, headers = server.repeat
        else:
            status, body, headers = 500, {"error": {"message": "stub exhausted"}}, {}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Wire:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests, self.httpd.replies = [], []
        self.httpd.repeat = None
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/v1"

    @property
    def requests(self) -> list:
        return self.httpd.requests

    def queue(self, *replies):
        """Each reply is (status, body) or (status, body, headers)."""
        for reply in replies:
            body = reply[1]
            headers = reply[2] if len(reply) > 2 else {}
            self.httpd.replies.append((reply[0], body, headers))

    def repeat(self, *reply):
        """Serve this reply forever once the queue drains (budget tests)."""
        body = reply[1]
        headers = reply[2] if len(reply) > 2 else {}
        self.httpd.repeat = (reply[0], body, headers)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def wire():
    server = Wire()
    yield server
    server.close()


@pytest.fixture
def client(wire):
    return LLMClient(base_url=wire.base_url, api_key="test-key", model="stub-model")


@pytest.fixture
def no_sleep(monkeypatch):
    """Record requested backoffs instead of paying them in wall clock."""
    delays: list[float] = []
    monkeypatch.setattr("pengy.core.llm_client._sleep_interruptible",
                        lambda seconds, is_cancelled: delays.append(seconds))
    return delays


def retrying_events(events: list) -> list:
    return [e for e in events if e.get("type") == "retrying"]


def raise_timeouts(wire: Wire, monkeypatch, times: int):
    """Make the SDK raise its own deadline for the first *times* requests."""
    request = httpx.Request("POST", f"{wire.base_url}/chat/completions")
    original = completions_resource.Completions.create
    calls: list[int] = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) <= times:
            raise APITimeoutError(request)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(completions_resource.Completions, "create", flaky)
    return calls


# ── Classification ────────────────────────────────────────────────────────────

def test_timeout_statuses_are_the_documented_pair():
    assert _TIMEOUT_STATUSES == {408, 504}


def test_classification_covers_both_shapes_and_excludes_the_rest(wire):
    def status_error(code: int) -> APIStatusError:
        response = type("R", (), {"status_code": code, "headers": {}, "request": None})()
        return APIStatusError(f"HTTP {code}", response=response, body={})

    assert _is_timeout_error(status_error(408))
    assert _is_timeout_error(status_error(504))
    assert _is_timeout_error(APITimeoutError(httpx.Request("POST", wire.base_url)))
    # Negative controls: "the endpoint is not there" and "the request was wrong"
    # must keep failing fast, and 429/529 keep their own budget.
    for code in (400, 401, 403, 404, 422, 429, 500, 503, 529):
        assert not _is_timeout_error(status_error(code))
    assert not _is_timeout_error(ConnectionError("connection refused"))


# ── Provider 408 / 504 answers ────────────────────────────────────────────────

@pytest.mark.parametrize("status", [408, 504])
def test_timeout_answer_is_retried_and_the_turn_completes(wire, client, no_sleep, status):
    wire.queue((status, TIMEOUT_BODY), (200, completion(content="recovered")))
    events = collect_auto(client.chat([{"role": "user", "content": "go"}]))

    notices = retrying_events(events)
    assert len(notices) == 1
    assert notices[0]["reason"] == "timeout"
    assert notices[0]["status_code"] == status
    assert notices[0]["attempt"] == 1
    assert notices[0]["max_attempts"] == _MAX_TIMEOUT_RETRIES
    assert events[-1]["type"] == "final_response"
    assert events[-1]["content"] == "recovered"
    # The retry re-sent the same request; nothing was lost.
    assert len(wire.requests) == 2
    assert wire.requests[0]["messages"] == wire.requests[1]["messages"]


def test_timeout_budget_is_spent_then_the_usual_timeout_message(wire, client, no_sleep):
    wire.repeat(408, TIMEOUT_BODY)
    events: list = []
    with pytest.raises(APIStatusError) as raised:
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)

    notices = retrying_events(events)
    assert len(notices) == _MAX_TIMEOUT_RETRIES
    assert [n["attempt"] for n in notices] == [1, 2, 3]
    assert raised.value.status_code == 408
    # 1 request + 3 retries, and no silent sixth attempt.
    assert len(wire.requests) == _MAX_TIMEOUT_RETRIES + 1


def test_backoff_ladder_and_retry_after_are_honoured(wire, client, no_sleep):
    wire.queue((408, TIMEOUT_BODY, {"retry-after": "7"}),
               (408, TIMEOUT_BODY), (408, TIMEOUT_BODY), (408, TIMEOUT_BODY))
    events: list = []
    with pytest.raises(APIStatusError):
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)

    # First wait obeys the provider's Retry-After; the rest follow the ladder
    # (each ±25% jitter, so assert the band, not the exact number).
    assert len(no_sleep) == 3
    assert 7.0 * 0.75 <= no_sleep[0] <= 7.0 * 1.25
    for index, base in enumerate((2.0, 4.0)):
        assert base * 0.75 <= no_sleep[index + 1] <= base * 1.25


def test_429_keeps_its_own_overload_budget(wire, client, no_sleep):
    """The pre-existing 429 path must not be swept into the timeout budget."""
    wire.repeat(429, {"error": {"message": "Too Many Requests"}})
    events: list = []
    with pytest.raises(APIStatusError) as raised:
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)

    notices = retrying_events(events)
    assert len(notices) == 5
    assert all("reason" not in n for n in notices)
    assert all(n["max_attempts"] == 5 for n in notices)
    assert raised.value.status_code == 429


# ── Our own llm_timeout deadline (no HTTP status at all) ──────────────────────

def test_client_side_timeout_is_retried_then_succeeds(wire, client, no_sleep, monkeypatch):
    calls = raise_timeouts(wire, monkeypatch, times=2)
    wire.queue((200, completion(content="answered")))
    events = collect_auto(client.chat([{"role": "user", "content": "go"}]))

    notices = retrying_events(events)
    assert len(notices) == 2
    assert all(n["reason"] == "timeout" for n in notices)
    # A client-side deadline has no status code to report.
    assert all(n["status_code"] == 0 for n in notices)
    assert events[-1]["content"] == "answered"
    assert len(calls) == 3


def test_client_side_timeout_exhausted_shows_the_timeout_message(wire, client, no_sleep, monkeypatch):
    raise_timeouts(wire, monkeypatch, times=99)
    events: list = []
    with pytest.raises(RuntimeError) as raised:
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)

    assert len(retrying_events(events)) == _MAX_TIMEOUT_RETRIES
    # The give-up text is Pengy's existing unreachable/timeout help, unchanged.
    message = str(raised.value)
    assert "timed out" in message.lower()
    assert "Nothing answered" in message  # 127.0.0.1 is a local endpoint


def test_cancel_during_timeout_backoff_stops_the_turn(wire, client):
    """Cancel is polled during the sleep, so a long timeout chain is escapable."""
    wire.repeat(408, TIMEOUT_BODY)
    state = {"cancel": False}
    events: list = []
    for event in client.chat([{"role": "user", "content": "go"}],
                             cancel_fn=lambda: state["cancel"]):
        events.append(event)
        if event["type"] == "retrying":
            state["cancel"] = True
        if event["type"] == "final_response":
            break

    assert len(retrying_events(events)) == 1
    assert events[-1]["content"] == "Request cancelled during backoff."
    # Cancel lands inside the backoff sleep, so the chain stops before the
    # next request is issued.
    assert len(wire.requests) == 1


# ── Negative controls: failures that must stay fast ───────────────────────────

def test_connection_refused_is_not_retried(monkeypatch):
    """'Is your server running?' help must arrive immediately, not after 3 retries."""
    dead = LLMClient(base_url="http://127.0.0.1:1/v1", api_key="k", model="m")
    monkeypatch.setattr("pengy.core.llm_client._sleep_interruptible",
                        lambda seconds, is_cancelled: pytest.fail("must not sleep"))
    events: list = []
    with pytest.raises(RuntimeError) as raised:
        for event in dead.chat([{"role": "user", "content": "go"}]):
            events.append(event)
    assert not retrying_events(events)
    assert "Nothing answered" in str(raised.value)


@pytest.mark.parametrize("status,body", [
    (400, {"error": {"message": "Invalid request"}}),
    (401, {"error": {"message": "Incorrect API key provided"}}),
])
def test_bad_request_and_bad_key_are_not_retried(wire, client, no_sleep, status, body):
    wire.queue((status, body))
    events: list = []
    with pytest.raises(Exception):
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)
    assert not retrying_events(events)
    assert not no_sleep
    assert len(wire.requests) == 1


# ── The hidden second request: context-recovery summarization ─────────────────

def history() -> list:
    messages = [{"role": "system", "content": "Never modify production."}]
    for n in range(6):
        messages += [
            {"role": "user", "content": f"Requirement {n}: exact path /tmp/project-{n}. " + "old discussion " * 200},
            {"role": "assistant", "content": f"Decision {n}: done."},
        ]
    messages.append({"role": "user", "content": "Current task: report requirements."})
    return messages


def test_summary_request_timeout_is_retried(wire, client, no_sleep):
    """A timeout inside auto-recovery must not kill the turn.

    Sequence: turn request -> 413 overflow -> summary request -> 408 -> summary
    retry -> 200 -> turn retry -> 200.
    """
    wire.queue((413, OVERFLOW_BODY),
               (408, TIMEOUT_BODY),
               (200, completion(content="Requirement 0-5 listed; task reported.")),
               (200, completion(content="reported")))
    events = collect_auto(client.chat(history(), tool_confirmation="all"))

    assert events[-1]["type"] == "final_response"
    assert events[-1]["content"] == "reported"
    # 1 turn + 1 failed summary + 1 summary retry + 1 turn retry.
    assert len(wire.requests) == 4


def test_summary_timeout_budget_is_finite(wire, client, no_sleep):
    wire.queue((413, OVERFLOW_BODY))
    wire.repeat(408, TIMEOUT_BODY)
    events: list = []
    with pytest.raises(Exception):
        for event in client.chat(history(), tool_confirmation="all"):
            events.append(event)
    # The summary gets the same bounded budget: 1 turn request + 1 initial
    # summary + _MAX_TIMEOUT_RETRIES summary retries, then it gives up.
    assert len(wire.requests) == _MAX_TIMEOUT_RETRIES + 2
