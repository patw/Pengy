"""The provider payload is the budget: tool-call arguments, not just text.

Regression cover for the 2026-11-08 behoserver incident: a 1 MB history whose
text was 75 K characters, where the size metric ignored ``tool_calls`` and every
reduction therefore looked bigger than the history it replaced. Recovery could
never commit, so a hard-wall engine (Basalt, 262 144) rejected every turn.
"""
import copy
import json

import pytest
from unittest.mock import Mock, patch
from openai import BadRequestError

from pengy.core.context_pressure import ANSWER_RESERVE_TOKENS, budgets
from pengy.core.context_recovery import (
    Recovery, payload_chars, stub_arguments, arguments_chars, CALL_MIN_CHARS,
)
from pengy.core.llm_client import (
    _answer_room, _is_context_limit_error, _is_output_parameter_error,
    _MAX_AUTO_OUTPUT_TOKENS, _SUMMARY_MAX_TOKENS,
)
from tests.test_auto_context_recovery import history
from tests.test_context_recovery import client, stub, overflow, _completion_obj
from tests.test_llm_loop import completion, tool_call, collect_auto


def unsupported_parameter(parameter="max_tokens"):
    """The 400 an OpenAI reasoning model answers to max_tokens."""
    message = (f"Unsupported parameter: '{parameter}' is not supported with this model. "
               "Use 'max_completion_tokens' instead.")
    body = {"error": {"message": message, "code": "unsupported_parameter",
                      "type": "invalid_request_error"}}
    response = Mock(status_code=400, headers={}, request=Mock(), json=lambda: body)
    return BadRequestError(message, response=response, body=body)


def test_parameter_rejection_is_not_a_context_error():
    assert _is_output_parameter_error(unsupported_parameter(), "max_tokens")
    assert not _is_context_limit_error(unsupported_parameter())
    assert not _is_output_parameter_error(overflow(), "max_tokens")
    # A rejection of the *other* parameter is not a rejection of this one.
    assert not _is_output_parameter_error(unsupported_parameter("max_completion_tokens"), "max_tokens")


def test_derived_allowance_is_recomputed_after_a_reduction(client):
    """A stale allowance would clip the answer exactly when room just came free."""
    from tests.test_proactive_context import seed
    messages = tool_heavy()
    seed(client, messages, "recompute", 65536, 40000)
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise overflow()
        return _completion_obj(completion(content="answer"))

    with patch.object(client.client.chat.completions, "create", side_effect=create):
        events = collect_auto(client.chat(messages, chat_id="recompute", context_window=65536))
    assert events[0]["strategy"] == "tool_call_arguments"
    assert events[-1]["content"] == "answer"
    first, second = requests[0]["max_tokens"], requests[1]["max_tokens"]
    assert 0 < first < second == _MAX_AUTO_OUTPUT_TOKENS


def test_route_that_rejects_the_allowance_parameter_still_answers(client):
    from tests.test_proactive_context import seed
    messages = [{"role": "user", "content": "hello"}]
    seed(client, messages, "param", 65536, 40000)
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise unsupported_parameter()
        return _completion_obj(completion(content="ok" if len(requests) == 2 else "again"))

    with patch.object(client.client.chat.completions, "create", side_effect=create):
        events = collect_auto(client.chat(messages, chat_id="param", context_window=65536))
        assert events[-1]["content"] == "ok"
        assert requests[0]["max_tokens"] > 0
        assert "max_tokens" not in requests[1]
        # Same calibrated route, next turn: the rejection is remembered.
        events = collect_auto(client.chat(messages, chat_id="param", context_window=65536))
    assert events[-1]["content"] == "again"
    assert "max_tokens" not in requests[2]


def big_call(tc_id, body_chars, name="write_file"):
    return tool_call(tc_id, name, {"path": f"/tmp/{tc_id}.md", "content": "x" * body_chars})


def tool_heavy(body_chars=20000, calls=4):
    """Small text, huge arguments -- the shape that beat the old metric."""
    messages = [{"role": "system", "content": "policy"},
                {"role": "user", "content": "build the site"}]
    for n in range(calls):
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [big_call(f"c{n}", body_chars)]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": "ok"},
            {"role": "user", "content": f"continue {n}"},
        ]
    messages.append({"role": "assistant", "content": "working"})
    messages.append({"role": "user", "content": "finish it"})
    return messages


def test_tool_call_handle_is_small_stable_and_leaves_tiny_calls_alone():
    big = {"path": "/tmp/x", "content": "y" * 6000}
    message = {"role": "assistant", "content": "",
               "tool_calls": [tool_call("a", "run_bash", big), tool_call("b", "read_file", {"path": "/tmp/x"})]}
    once, twice = stub_arguments(message), stub_arguments(message)
    assert once == twice                                  # deterministic: prompt cache stays valid
    assert [c["id"] for c in once["tool_calls"]] == ["a", "b"]   # pairing survives
    assert [c["function"]["name"] for c in once["tool_calls"]] == ["run_bash", "read_file"]
    handle = once["tool_calls"][0]["function"]["arguments"]
    assert len(handle) < CALL_MIN_CHARS and len(handle) < arguments_chars(message)
    assert once["tool_calls"][1] == message["tool_calls"][1]     # tiny arguments untouched
    assert message["tool_calls"][0]["function"]["arguments"] == json.dumps(big)


def test_tool_heavy_history_commits_without_spending_a_summary_call():
    messages = tool_heavy()
    original = copy.deepcopy(messages)
    recovery = Recovery(messages, "test", "m", keep_turns=3)
    summaries = []
    event = recovery.reduce(messages, lambda source: summaries.append(source) or "checkpoint")
    assert event is not None and event["strategy"] == "tool_call_arguments"
    assert summaries == []                                # free elision, no model call
    assert event["turns_summarized"] == 0
    after = recovery.apply(messages)
    assert payload_chars(after) < payload_chars(messages) * 0.2
    assert json.dumps(messages) == json.dumps(original)    # surviving transcript untouched
    assert [c["tool_call_id"] for c in after if c.get("role") == "tool"] == ["c0", "c1", "c2", "c3"]


def test_reduction_is_measured_on_the_wire_payload():
    messages = tool_heavy()
    recovery = Recovery(messages, "test", "m", keep_turns=3)
    event = recovery.reduce(messages, lambda _: pytest.fail("no summary needed"))
    assert event["chars_removed"] > 60000                 # 4 x 20 000-char bodies
    assert event["message"].startswith("Context recovery — tool call arguments")


def test_summary_that_hits_the_cap_still_commits(client):
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise overflow()
        if "tools" not in kwargs:                         # the summary call
            reply = completion(content="Checkpoint: " + "c" * 400)
            reply["choices"][0]["finish_reason"] = "length"
            return _completion_obj(reply)
        return _completion_obj(completion(content="answer"))

    with patch.object(client.client.chat.completions, "create", side_effect=create):
        events = collect_auto(client.chat(history()))
    assert events[0]["strategy"] == "history_summary"
    assert events[-1]["content"] == "answer"
    assert requests[1]["max_tokens"] == _SUMMARY_MAX_TOKENS


def test_empty_truncated_summary_is_still_a_failure(client):
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise overflow()
        reply = completion(content="")
        reply["choices"][0]["finish_reason"] = "length"
        return _completion_obj(reply)

    with patch.object(client.client.chat.completions, "create", side_effect=create):
        with pytest.raises(RuntimeError, match="incomplete"):
            collect_auto(client.chat(history()))


def test_budgets_reserves_an_answer_without_a_configured_limit():
    at, hard = budgets(262144, 75, 0)
    assert at == 196608 and hard == 262144 - ANSWER_RESERVE_TOKENS
    assert budgets(262144, 75, 100000) == (162144, 162144)
    assert budgets(262144, 75, 0, True) == (196608, 262144)
    assert _answer_room(262144, 200000) == 32768
    assert _answer_room(262144, 261000) == 1144
    assert _answer_room(262144, None) == 0               # uncalibrated: no invented cap


def test_request_carries_the_derived_allowance(client, stub):
    from tests.test_proactive_context import seed
    messages = [{"role": "user", "content": "hello"}]
    seed(client, messages, "cap", 262144, 200000)
    stub.queue(completion(content="ok"))
    collect_auto(client.chat(messages, chat_id="cap", context_window=262144))
    assert stub.requests[-1]["body"]["max_tokens"] == _MAX_AUTO_OUTPUT_TOKENS

    stub.queue(completion(content="ok"))
    collect_auto(client.chat(messages, chat_id="fresh", context_window=262144))
    assert "max_tokens" not in stub.requests[-1]["body"]  # nothing calibrated yet
