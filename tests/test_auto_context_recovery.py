"""Deterministic recovery contracts; no real model required."""
import copy
import json

import pytest
from unittest.mock import patch

from pengy.core.context_recovery import Recovery, fingerprint, synthetic, SUMMARY_PROMPT
from tests.test_context_recovery import client, stub, overflow, _completion_obj
from tests.test_llm_loop import completion, tool_call, collect_auto


def history():
    result = [{"role": "system", "content": "Never modify production."}]
    for n in range(6):
        result += [{"role": "user", "content": f"Requirement {n}: exact path /tmp/project-{n}; value {n*17}. " + "old discussion " * 200},
                   {"role": "assistant", "content": f"Decision {n}: done; outstanding audit."}]
    result.append({"role": "user", "content": "Current task: report requirements and outstanding audit."})
    return result


def test_summary_keeps_recent_turns_and_is_request_only_and_durable(tmp_path, monkeypatch):
    monkeypatch.setattr("pengy.core.config._config_dir_override", str(tmp_path))
    messages = history()
    original = copy.deepcopy(messages)
    seen = []
    def summarize(source):
        seen.append(source)
        return "Requirement 0: exact path /tmp/project-0; value 0. Decision 0: done; outstanding audit."
    recovery = Recovery(messages, "http://test/v1", "m", chat_id="chat")
    event = recovery.reduce(messages, summarize)
    assert event["strategy"] == "history_summary"
    assert "Requirement 0" in seen[0]
    outgoing = recovery.apply(messages)
    assert outgoing[0] == messages[0]
    assert outgoing[-1] == messages[-1]
    assert "/tmp/project-0" in outgoing[1]["content"]
    assert not any(m["content"].startswith("Requirement 0:") for m in outgoing if m["role"] == "user")
    assert messages == original
    resumed = Recovery(messages + [{"role": "assistant", "content": "ok"}], "http://test/v1", "m", chat_id="chat")
    assert resumed.state["drop"] == recovery.state["drop"]
    assert resumed.apply(messages) == outgoing
    edited = copy.deepcopy(messages); edited[1]["content"] += " changed"
    assert Recovery(edited, "http://test/v1", "m", chat_id="chat").state["drop"] == 0
    assert Recovery(messages, "http://other/v1", "m", chat_id="chat").state["drop"] == 0
    assert Recovery(messages, "http://test/v1", "other", chat_id="chat").state["drop"] == 0


def test_reasoning_omitted_but_proxy_state_and_active_chain_protected():
    messages = history()
    messages[2].update(reasoning_content="think " * 1000, reasoning="trace", reasoning_details={"ordinary": "trace"})
    envelope = {"format": "openai-proxy/reasoning-v1", "proxy_model": "m", "blocks": ["opaque"]}
    messages[4]["reasoning_details"] = envelope
    messages += [{"role": "assistant", "content": "", "tool_calls": [tool_call("a", "read_file", {"path": "x"})], "reasoning_content": "active"},
                 {"role": "tool", "tool_call_id": "a", "content": "current result"},
                 {"role": "user", "content": [{"type": "text", "text": "Image loaded by read_image: /tmp/x"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
    original = copy.deepcopy(messages)
    recovery = Recovery(messages, "test", "m")
    assert recovery.reduce(messages, lambda _: "summary")["strategy"] == "historical_reasoning"
    outgoing = recovery.apply(messages)
    assert "reasoning_content" not in outgoing[2]
    assert outgoing[2]["reasoning_details"] == {"ordinary": "trace"}
    assert outgoing[4]["reasoning_details"] == envelope
    assert outgoing[-3:]==messages[-3:]
    assert synthetic(messages[-1])
    assert messages == original


def test_failure_rolls_back_and_no_drop_without_completed_history():
    messages = history()
    recovery = Recovery(messages, "test", "m")
    before = copy.deepcopy(recovery.state)
    with pytest.raises(RuntimeError):
        recovery.reduce(messages, lambda _: (_ for _ in ()).throw(RuntimeError("summary failed")))
    assert recovery.state == before
    assert recovery.apply(messages) == messages
    assert Recovery([{"role":"user", "content":"x"*100000}], "test", "m").reduce(messages[:1], lambda _: "unused") is None


def test_empty_length_recovers_without_final_empty_message(client, stub):
    messages = history()
    original = copy.deepcopy(messages)
    blank = completion(content=None); blank["choices"][0]["finish_reason"] = "length"
    stub.queue(blank, completion(content="Requirement 0: /tmp/project-0 value 0; audit pending"), completion(content="recovered"))
    events = collect_auto(client.chat(messages))
    assert [e["type"] for e in events] == ["context_compacted", "final_response"]
    assert len(stub.requests) == 3
    summary_req = stub.requests[1]["body"]
    assert "tools" not in summary_req
    assert summary_req["messages"][0]["content"] == SUMMARY_PROMPT
    assert messages == original
    assert events[-1]["usage"]["total_tokens"] == 45


def test_explicit_overflow_uses_configured_output_allowance_and_summaries(client):
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1: raise overflow()
        return _completion_obj(completion(content="short checkpoint" if "tools" not in kwargs else "done"))
    with patch.object(client.client.chat.completions, "create", side_effect=create):
        events = collect_auto(client.chat(history(), output_token_limit=4096, output_token_parameter="max_completion_tokens"))
    assert events[-1]["content"] == "done"
    assert requests[0]["max_completion_tokens"] == 4096
    assert requests[1]["max_completion_tokens"] == 2048
    assert events[0]["strategy"] == "history_summary"


def test_reduced_tool_bodies_persist_across_later_tool_rounds(client, stub):
    messages = [{"role": "user", "content": "inspect"},
                {"role": "assistant", "content": "", "tool_calls": [tool_call("old", "read_file", {"path": "x"})]},
                {"role": "tool", "tool_call_id": "old", "content": "DATA" * 10000}]
    blank = completion(content=None); blank["choices"][0]["finish_reason"] = "length"
    stub.queue(blank, completion(tool_calls=[tool_call("new", "read_file", {"path": "x"})]), completion(content="done"))
    with patch("pengy.core.llm_client._run_tool", return_value="current") as run:
        events = collect_auto(client.chat(messages, tool_confirmation="all"))
    assert run.call_count == 1
    assert len(stub.requests[1]["body"]["messages"][2]["content"]) < 4000
    assert stub.requests[2]["body"]["messages"][2]["content"] == stub.requests[1]["body"]["messages"][2]["content"]
    assert messages[2]["content"] == "DATA" * 10000
    assert events[-1]["content"] == "done"


def test_bounded_retries_even_if_summaries_keep_succeeding(client):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if "tools" not in kwargs: return _completion_obj(completion(content="requirements retained"))
        raise overflow()
    events = []
    with patch.object(client.client.chat.completions, "create", side_effect=create):
        with pytest.raises(RuntimeError, match="bounded recovery"):
            for event in client.chat(history(), recovery_keep_turns=0): events.append(event)
    assert len(events) <= 4
    assert len([c for c in calls if "tools" in c]) <= 5


def test_incomplete_tasks_and_text_mentions_are_not_summary_boundaries():
    messages = [{"role": "user", "content": "old request " * 1000},
                {"role": "assistant", "content": "", "tool_calls": [tool_call("a", "read_file", {"path": "x"})]},
                {"role": "tool", "tool_call_id": "a", "content": "small result"},
                {"role": "user", "content": "Image loaded by read_image: this is my actual text request"}]
    assert not synthetic(messages[-1])
    recovery = Recovery(messages, "test", "m", keep_turns=0)
    assert recovery.reduce(messages, lambda _: "must not summarize incomplete task") is None


def test_cancelled_summary_never_commits(client, stub):
    messages = history()
    blank = completion(content=None); blank["choices"][0]["finish_reason"] = "length"
    stub.queue(blank)
    cancelled = False
    def create(**kwargs):
        nonlocal cancelled
        if "tools" not in kwargs:
            cancelled = True
            return _completion_obj(completion(content="checkpoint"))
        return _completion_obj(blank)
    with patch.object(client.client.chat.completions, "create", side_effect=create):
        events = collect_auto(client.chat(messages, cancel_fn=lambda: cancelled))
    assert not any(e["type"] == "context_compacted" for e in events)
    assert events[-1]["message"] is None
    assert "cancelled" in events[-1]["content"].lower()
    assert messages == history()


def test_corrupt_boundary_checkpoint_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr("pengy.core.config._config_dir_override", str(tmp_path))
    messages = history()
    recovery = Recovery(messages, "test", "m", chat_id="boundary")
    recovery.reduce(messages, lambda _: "checkpoint")
    data = json.loads(recovery.path.read_text())
    data["drop"] = 1  # would retain an orphan assistant answer, not a user boundary
    recovery.path.write_text(json.dumps(data))
    assert Recovery(messages, "test", "m", chat_id="boundary").apply(messages) == messages


def test_summary_source_is_chunked_without_sampling_user_text():
    messages = history()
    messages[1]["content"] = "FIRST_REQUIREMENT\n" + "x" * 90000 + "\nLAST_REQUIREMENT"
    chunks = []
    recovery = Recovery(messages, "test", "m")
    event = recovery.reduce(messages, lambda source: chunks.append(source) or "checkpoint")
    assert event["strategy"] == "history_summary"
    assert len(chunks) > 1 and all(len(c) <= 32000 for c in chunks)
    assert "FIRST_REQUIREMENT" in "".join(chunks)
    assert "LAST_REQUIREMENT" in "".join(chunks)
