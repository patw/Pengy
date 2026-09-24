"""Provider-only context overflow recovery through the real LLM tool loop."""
import json
from unittest.mock import patch

import pytest
from openai import BadRequestError

from pengy.core.llm_client import (
    LLMClient, _compact_tool_result, _is_context_limit_error, _MAX_CONTEXT_RETRIES,
)
from tests.test_llm_loop import completion, tool_call, collect_auto, StubLLMServer


@pytest.fixture
def stub():
    server = StubLLMServer()
    yield server
    server.close()


@pytest.fixture
def client(stub):
    return LLMClient(base_url=stub.base_url, api_key="test-key", model="stub-model")


def overflow(message="This model's maximum context length is 1024 tokens", code=None):
    import httpx
    body = {"error": {"message": message, "code": code}}
    response = httpx.Response(400, json=body, request=httpx.Request("POST", "http://localhost/v1/chat/completions"))
    return BadRequestError(message, response=response, body=body)


def test_error_classification():
    assert _is_context_limit_error(overflow())
    assert _is_context_limit_error(overflow("request rejected", "context_length_exceeded"))
    assert not _is_context_limit_error(overflow("Invalid model; images unsupported"))


def test_compaction_is_copy_and_preserves_tool_structure():
    history = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "tool_calls": [tool_call("a", "run_python", {"code": "print(1)"}),
                                              tool_call("b", "run_python", {"code": "print(2)"})]},
        {"role": "tool", "tool_call_id": "a", "content": "a" * 12000},
        {"role": "tool", "tool_call_id": "b", "content": "b" * 12000},
    ]
    original = json.dumps(history)
    preview, saved = _compact_tool_result(history, 1)
    assert saved > 8000
    assert len(preview[3]["content"]) < 4000
    assert preview[4] == history[4]  # newest result protected
    assert preview[2] == history[2]
    stubbed, _ = _compact_tool_result(preview, 2)
    assert "omitted" in stubbed[3]["content"]
    assert json.dumps(history) == original


def test_real_tool_loop_recovers_without_reexecuting(stub, client):
    """An overflow after a tool call must not rerun that tool or lose its full output."""
    # The existing stub server accepts only successful responses. Intercept
    # the API at its boundary to inject one provider failure between two calls.
    long_result = "HEAD\n" + "DATA-" * 10000 + "\nTAIL"
    with patch("pengy.core.llm_client._run_tool", return_value=long_result) as run:
        stub.queue(completion(tool_calls=[tool_call("first", "run_python", {"code": "print(1)"})]))
        sdk = client.client.chat.completions.create
        calls = 0
        requests = []

        def create(**kwargs):
            nonlocal calls
            calls += 1
            requests.append(kwargs["messages"])
            if calls == 2:
                raise overflow()
            if calls == 3:
                return _completion_obj(completion(content="recovered"))
            return sdk(**kwargs)

        with patch.object(client.client.chat.completions, "create", side_effect=create):
            events = collect_auto(client.chat([{"role": "user", "content": "test"}], tool_confirmation="all"))
        assert run.call_count == 1
        assert [e["type"] for e in events] == [
            "assistant_tool_calls", "tool_request", "tool_result", "context_compacted", "final_response"]
        assert events[2]["content"] == long_result
        assert requests[1][-1]["content"] == long_result
        assert len(requests[2][-1]["content"]) < 4000
        assert requests[2][-1]["tool_call_id"] == "first"
        assert events[-1]["content"] == "recovered"


def _completion_obj(data):
    from openai.types.chat import ChatCompletion
    return ChatCompletion.model_validate({**data, "created": 0})


def test_no_recovery_without_tool_content(client):
    with patch.object(client.client.chat.completions, "create", side_effect=overflow()):
        with pytest.raises(RuntimeError, match="Model context limit reached"):
            collect_auto(client.chat([{"role": "user", "content": "x" * 10000}]))


def test_unrelated_bad_request_not_retried(client):
    error = overflow("Invalid model")
    with patch.object(client.client.chat.completions, "create", side_effect=error) as create:
        with pytest.raises(BadRequestError):
            collect_auto(client.chat([{"role": "user", "content": "hello"}]))
    assert create.call_count == 1


def test_bounded_context_retries_and_full_history_preserved(client):
    first = "X" * 40000
    second = "Y" * 40000
    history = [
        {"role": "user", "content": "describe"},
        {"role": "assistant", "content": "", "tool_calls": [tool_call("a", "read_file", {"path": "a"}),
                                                        tool_call("b", "read_file", {"path": "b"})]},
        {"role": "tool", "tool_call_id": "a", "content": first},
        {"role": "tool", "tool_call_id": "b", "content": second},
    ]
    with patch.object(client.client.chat.completions, "create", side_effect=overflow()) as create:
        gen = client.chat(history)
        events = []
        with pytest.raises(RuntimeError, match="could not fit"):
            events = list(gen)
    assert create.call_count <= _MAX_CONTEXT_RETRIES + 1
    assert history[-2]["content"] == first and history[-1]["content"] == second


def test_image_error_not_confused_with_context(client):
    image = {"role": "user", "content": [
        {"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}
    stub_client = client.client
    client._reset_client = lambda: None  # keep the mocked transport across image retry
    with patch.object(stub_client.chat.completions, "create", side_effect=[
        overflow("model does not support image inputs"), _completion_obj(completion(content="text only"))]) as create:
        events = collect_auto(client.chat([image]))
    assert events[-1]["content"] == "text only"
    assert create.call_args_list[-1].kwargs["messages"][0]["content"] == "look"
    assert isinstance(image["content"], list)
