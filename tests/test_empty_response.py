"""Degenerate 200 responses: an endpoint that answers and produces nothing.

Two shapes, one failure:

- **no ``choices`` at all** -- the shape a proxy answers when it loses its
  upstream mid-generation.  Python used to raise ``IndexError: list index out of
  range`` here, while Rust and C++ already said "No choices in API response".
- **a message with neither content nor tool calls** -- counted as a completed
  answer: the CLI exited 0 and the chat stored a blank assistant message that
  the next turn reads back as something the model said.  Observed in the field
  (BotTalk ff4f1c1f): a reasoning model whose whole output allowance goes to
  reasoning tokens answers with empty content and finish_reason=stop.

Contract now: the first gets one retry (a blip is worth riding out, a
deterministic answer is not worth three full-cost attempts) and then a message
that says what happened; the second is reported as a provider anomaly, is never
stored as an assistant turn, and exits non-zero so an unattended run fails
loudly.
"""
from __future__ import annotations

import json
import pytest

from pengy.core.llm_client import (
    EmptyResponseError, LLMClient, _MAX_EMPTY_RETRIES,
)
from tests.test_llm_loop import collect_auto, completion, tool_call
from tests.test_timeout_retry import Wire, retrying_events


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
    monkeypatch.setattr("pengy.core.llm_client._sleep_interruptible",
                        lambda seconds, is_cancelled: None)


NO_CHOICES = {"choices": [], "usage": None}
NO_CHOICES_KEY = {"id": "cmpl-stub", "object": "chat.completions"}
EMPTY_MESSAGE = {"choices": [{"index": 0,
                              "message": {"role": "assistant", "content": None},
                              "finish_reason": "stop"}], "usage": None}


# ── F4: no choices ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [NO_CHOICES, NO_CHOICES_KEY],
                         ids=["empty list", "key missing"])
def test_no_choices_is_retried_then_reported_actionably(wire, client, no_sleep, payload):
    wire.repeat(200, payload)
    events: list = []
    with pytest.raises(EmptyResponseError) as raised:
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)

    notices = retrying_events(events)
    assert len(notices) == _MAX_EMPTY_RETRIES
    assert notices[0]["reason"] == "empty"
    # The user gets a sentence they can act on, not the SDK's index error.
    message = str(raised.value)
    assert "no choices" in message
    assert "index out of range" not in message
    assert raised.value.kind == "empty"
    assert raised.value.exit_code == 1


def test_no_choices_that_recovers_costs_the_user_nothing(wire, client, no_sleep):
    wire.queue((200, NO_CHOICES))
    wire.queue((200, completion(content="answered")))
    events = collect_auto(client.chat([{"role": "user", "content": "go"}]))

    assert len(retrying_events(events)) == 1
    assert events[-1]["content"] == "answered"
    assert len(wire.requests) == 2


# ── F5: an empty reply is not an answer ───────────────────────────────────────

@pytest.mark.parametrize("payload", [EMPTY_MESSAGE,
                                     {"choices": [{"index": 0,
                                                   "message": {"role": "assistant", "content": "   "},
                                                   "finish_reason": "stop"}], "usage": None}],
                         ids=["null content", "whitespace content"])
def test_empty_reply_is_reported_not_delivered(wire, client, payload):
    wire.queue((200, payload))
    events: list = []
    with pytest.raises(EmptyResponseError) as raised:
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)

    # No blank assistant turn reaches the caller, so nothing blank is persisted.
    assert not [e for e in events if e["type"] == "final_response"]
    assert raised.value.kind == "empty"
    assert raised.value.exit_code == 1
    assert "empty reply" in str(raised.value)


def test_empty_reply_with_reasoning_names_the_budget(wire, client):
    """The field signature (BotTalk ff4f1c1f): reasoning tokens, no answer."""
    wire.queue((200, {"choices": [{"index": 0, "message": {
        "role": "assistant", "content": "", "reasoning_content": "thinking" * 40},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 40, "completion_tokens": 1500,
                                             "total_tokens": 1540}}))
    with pytest.raises(EmptyResponseError) as raised:
        collect_auto(client.chat([{"role": "user", "content": "go"}]))

    message = str(raised.value)
    assert "reasoning" in message
    assert "output_token_limit" in message      # the knob the user can turn
    assert "1,500" in message                   # completion count, for diagnosis


def test_empty_reply_is_not_retried(wire, client, no_sleep):
    """An empty answer is the endpoint's shape, not a blip: no full-cost chain."""
    wire.repeat(200, EMPTY_MESSAGE)
    events: list = []
    with pytest.raises(EmptyResponseError):
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)
    assert not retrying_events(events)
    assert len(wire.requests) == 1


def test_an_empty_answer_with_tool_calls_is_still_normal(wire, client, tmp_path):
    """Negative control: content:"" plus tool calls is a normal round, not an anomaly."""
    wire.queue((200, completion(tool_calls=[
        tool_call("t1", "read_file", json.dumps({"path": "does-not-exist.txt"}))])))
    wire.queue((200, completion(content="after the tool")))
    events = collect_auto(client.chat([{"role": "user", "content": "go"}],
                                      tool_confirmation="all"))
    assert events[-1]["content"] == "after the tool"


def test_a_truncated_empty_answer_keeps_the_generation_limit_path(wire, client):
    """Negative control: finish_reason=length is already handled, and stays there."""
    payload = completion(content="")
    payload["choices"][0]["finish_reason"] = "length"
    wire.repeat(200, payload)
    events: list = []
    with pytest.raises(RuntimeError) as raised:
        for event in client.chat([{"role": "user", "content": "go"}]):
            events.append(event)
    assert getattr(raised.value, "kind", "") == "truncated"
    assert "finish_reason=length" in str(raised.value)


# ── The hidden second request: context-recovery summarization ─────────────────

def history() -> list:
    messages = [{"role": "system", "content": "policy"}]
    for n in range(6):
        messages += [
            {"role": "user", "content": f"Requirement {n}: " + "old discussion " * 200},
            {"role": "assistant", "content": f"Decision {n}"},
        ]
    messages.append({"role": "user", "content": "Current task"})
    return messages


def test_summary_with_no_choices_says_so(wire, client, no_sleep):
    wire.queue((413, {"error": {"message": "This model's maximum context length is 1024 tokens",
                              "code": "context_length_exceeded"}}))
    wire.repeat(200, NO_CHOICES)
    events: list = []
    with pytest.raises(Exception) as raised:
        for event in client.chat(history(), tool_confirmation="all"):
            events.append(event)

    message = str(raised.value)
    assert "no choices" in message.lower()
    assert "index out of range" not in message
    assert "history retained" in message
