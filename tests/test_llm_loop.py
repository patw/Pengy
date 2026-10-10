"""Conversation-loop tests for LLMClient.chat() against a canned stub server.

These tests exercise the heart of the agent — the tool-call loop — without a
real LLM: a local HTTP server replays queued /chat/completions responses and
records every request payload so tests can assert on what the client sent.

Covered here:
- final response with no tools
- tool loop in "all" / "safe" / "none" confirmation modes
- decline feeding "Tool execution was declined by user." back to the model
- reasoning_effort request field and preserve_reasoning message fields
- usage accumulation across tool round-trips
- malformed tool-call arguments JSON
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pengy.core.llm_client import LLMClient
from pengy.core import tools as tools_mod


# ── Stub server ────────────────────────────────────────────────────────────────

class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        server = self.server
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        server.requests.append({"path": self.path, "body": body})

        if not server.responses:
            payload = {"error": {"message": "stub exhausted"}}
            status = 500
        else:
            payload = server.responses.pop(0)
            status = 200

        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence request logging
        pass


class StubLLMServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.httpd.responses = []
        self.httpd.requests = []
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/v1"

    @property
    def requests(self) -> list:
        return self.httpd.requests

    def queue(self, *responses):
        self.httpd.responses.extend(responses)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    server = StubLLMServer()
    yield server
    server.close()


@pytest.fixture
def client(stub):
    return LLMClient(base_url=stub.base_url, api_key="test-key", model="stub-model")


# ── Response builders ──────────────────────────────────────────────────────────

def completion(content=None, tool_calls=None, usage=(10, 5), **msg_extra):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    message.update(msg_extra)
    return {
        "id": "cmpl-stub",
        "object": "chat.completion",
        "model": "stub-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        },
    }


def tool_call(tc_id, name, args) -> dict:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def collect_auto(gen) -> list:
    """Drive a generator that never pauses for confirmation."""
    events = []
    for ev in gen:
        events.append(ev)
        if ev["type"] == "final_response":
            break
    return events


# ── Tests: plain response ──────────────────────────────────────────────────────

class TestFinalResponse:
    def test_no_tools_final_response(self, stub, client):
        stub.queue(completion(content="hello there"))
        events = collect_auto(client.chat([{"role": "user", "content": "hi"}]))

        assert [e["type"] for e in events] == ["final_response"]
        assert events[0]["content"] == "hello there"
        assert events[0]["usage"]["total_tokens"] == 15

        req = stub.requests[0]["body"]
        assert req["model"] == "stub-model"
        assert req["tool_choice"] == "auto"
        assert any(t["function"]["name"] == "read_file" for t in req["tools"])
        assert "reasoning_effort" not in req

    def test_reasoning_effort_sent_when_set(self, stub, client):
        stub.queue(completion(content="ok"))
        collect_auto(client.chat(
            [{"role": "user", "content": "hi"}], reasoning_effort="high"))
        assert stub.requests[0]["body"]["reasoning_effort"] == "high"

    def test_model_override_sent(self, stub, client):
        """A per-call model override wins over the client's default model."""
        stub.queue(completion(content="ok"))
        collect_auto(client.chat(
            [{"role": "user", "content": "hi"}], model="custom-model"))
        assert stub.requests[0]["body"]["model"] == "custom-model"

    def test_preserve_reasoning_keeps_fields(self, stub, client, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("data")
        stub.queue(
            completion(content="", reasoning_content="thinking...",
                       tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})]),
            completion(content="done"),
        )
        collect_auto(client.chat(
            [{"role": "user", "content": "read it"}],
            tool_confirmation="all", preserve_reasoning=True))

        second_req_msgs = stub.requests[1]["body"]["messages"]
        assistant = next(m for m in second_req_msgs if m["role"] == "assistant")
        assert assistant["reasoning_content"] == "thinking..."

    def test_reasoning_dropped_by_default(self, stub, client, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("data")
        stub.queue(
            completion(content="", reasoning_content="thinking...",
                       tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})]),
            completion(content="done"),
        )
        collect_auto(client.chat(
            [{"role": "user", "content": "read it"}], tool_confirmation="all"))

        second_req_msgs = stub.requests[1]["body"]["messages"]
        assistant = next(m for m in second_req_msgs if m["role"] == "assistant")
        assert "reasoning_content" not in assistant


# ── Tests: tool loop, confirmation modes ───────────────────────────────────────

@pytest.mark.parametrize("provider", ["openai_responses", "anthropic_messages"])
def test_proxy_state_round_trip_and_resume(stub, client, tmp_path, provider):
    envelope = {"format": "openai-proxy/reasoning-v1", "provider": provider,
                "proxy_model": "stub-model", "model": "upstream-stub",
                "blocks": [{"type": "reasoning", "encrypted_content": "opaque-test"}]}
    f = tmp_path / "note.txt"
    f.write_text("hello")
    stub.queue(completion(tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})],
                          reasoning_details=envelope, reasoning_content="ordinary"),
               completion(content="done", reasoning_details=envelope))
    events = collect_auto(client.chat([{"role": "user", "content": "read"}],
                                      tool_confirmation="all", preserve_reasoning=False))
    assistant = stub.requests[1]["body"]["messages"][1]
    assert assistant["reasoning_details"] == envelope
    assert "reasoning_content" not in assistant
    assert events[0]["message"]["reasoning_details"] == envelope
    assert events[-1]["message"]["reasoning_details"] == envelope
    # Saved JSON history must retain the state and replay on the next user turn.
    from pengy.core.chat_manager import create_chat, save_chat, get_chat
    chat = create_chat()
    chat["messages"] = [events[-1]["message"], {"role": "user", "content": "again"}]
    save_chat(chat)
    history = get_chat(chat["id"])["messages"]
    stub.queue(completion(content="resumed"), completion(content="switched"))
    collect_auto(client.chat(history))
    assert stub.requests[2]["body"]["messages"][0]["reasoning_details"] == envelope
    collect_auto(client.chat(history, model="different-model"))
    assert "reasoning_details" not in stub.requests[3]["body"]["messages"][0]
    assert history[0]["reasoning_details"] == envelope


@pytest.mark.parametrize("preserve", [False, True])
def test_foreign_reasoning_respects_toggle(stub, client, preserve):
    details = [{"type": "other", "text": "ordinary"}]
    stub.queue(completion(content="ok", reasoning_details=details))
    final = collect_auto(client.chat([{"role": "user", "content": "hi"}],
                                     preserve_reasoning=preserve))[-1]["message"]
    assert ("reasoning_details" in final) == preserve
    if preserve:
        assert final["reasoning_details"] == details



class TestToolLoop:
    def test_all_mode_auto_executes(self, stub, client, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("file body here")
        stub.queue(
            completion(tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})]),
            completion(content="summary"),
        )
        events = collect_auto(client.chat(
            [{"role": "user", "content": "read it"}], tool_confirmation="all"))

        types = [e["type"] for e in events]
        assert types == ["assistant_tool_calls", "tool_request", "tool_result",
                         "final_response"]
        result = events[2]
        assert result["declined"] is False
        assert "file body here" in result["content"]

        # Second request must carry the assistant tool_calls + tool result
        msgs = stub.requests[1]["body"]["messages"]
        assert msgs[-2]["role"] == "assistant"
        assert msgs[-2]["tool_calls"][0]["id"] == "tc1"
        assert msgs[-1] == {"role": "tool", "tool_call_id": "tc1",
                            "content": result["content"]}

    def test_safe_mode_auto_approves_readonly(self, stub, client, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("safe read")
        stub.queue(
            completion(tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})]),
            completion(content="done"),
        )
        events = collect_auto(client.chat(
            [{"role": "user", "content": "read"}], tool_confirmation="safe"))
        assert "safe read" in events[2]["content"]
        assert events[2]["declined"] is False

    def test_safe_mode_pauses_for_write_tool(self, stub, client, tmp_path):
        target = tmp_path / "out.txt"
        stub.queue(
            completion(tool_calls=[tool_call(
                "tc1", "write_file", {"path": str(target), "content": "written!"})]),
            completion(content="done"),
        )
        gen = client.chat([{"role": "user", "content": "write"}],
                          tool_confirmation="safe")

        assert next(gen)["type"] == "assistant_tool_calls"
        req = next(gen)
        assert req["type"] == "tool_request"
        assert not target.exists(), "tool must not run before confirmation"

        result = gen.send({"confirmed": True})
        assert result["type"] == "tool_result"
        assert result["declined"] is False
        assert target.read_text() == "written!"

        final = next(gen)
        assert final["type"] == "final_response"

    def test_none_mode_prompts_even_for_readonly(self, stub, client, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("body")
        stub.queue(
            completion(tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})]),
            completion(content="done"),
        )
        gen = client.chat([{"role": "user", "content": "read"}],
                          tool_confirmation="none")
        assert next(gen)["type"] == "assistant_tool_calls"
        assert next(gen)["type"] == "tool_request"
        result = gen.send({"confirmed": True})
        assert result["type"] == "tool_result"
        assert "body" in result["content"]

    def test_decline_feeds_declined_message_to_model(self, stub, client, tmp_path):
        target = tmp_path / "out.txt"
        stub.queue(
            completion(tool_calls=[tool_call(
                "tc1", "write_file", {"path": str(target), "content": "x"})]),
            completion(content="understood"),
        )
        gen = client.chat([{"role": "user", "content": "write"}],
                          tool_confirmation="none")
        next(gen)                              # assistant_tool_calls
        next(gen)                              # tool_request
        result = gen.send(None)                # decline
        assert result["type"] == "tool_result"
        assert result["declined"] is True
        assert not target.exists()

        assert next(gen)["type"] == "final_response"
        msgs = stub.requests[1]["body"]["messages"]
        assert msgs[-1]["content"] == "Tool execution was declined by user."

    def test_multiple_tool_calls_in_one_round(self, stub, client, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("alpha")
        f2.write_text("beta")
        stub.queue(
            completion(tool_calls=[
                tool_call("tc1", "read_file", {"path": str(f1)}),
                tool_call("tc2", "read_file", {"path": str(f2)}),
            ]),
            completion(content="done"),
        )
        events = collect_auto(client.chat(
            [{"role": "user", "content": "read both"}], tool_confirmation="all"))

        results = [e for e in events if e["type"] == "tool_result"]
        assert len(results) == 2
        assert "alpha" in results[0]["content"]
        assert "beta" in results[1]["content"]
        # Both tool results present in the follow-up request, in order
        msgs = stub.requests[1]["body"]["messages"]
        assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["tc1", "tc2"]

    def test_usage_accumulates_across_round_trips(self, stub, client, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        stub.queue(
            completion(usage=(100, 20),
                       tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})]),
            completion(content="done", usage=(200, 30)),
        )
        events = collect_auto(client.chat(
            [{"role": "user", "content": "go"}], tool_confirmation="all"))
        usage = events[-1]["usage"]
        assert usage["prompt_tokens"] == 300
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 350

    def test_intermediate_events_carry_running_turn_usage(self, stub, client, tmp_path):
        """tool_request/question_request report usage so the UI can tick live.

        The count must not wait for final_response: the value on the first
        round's tool_request is that round's running total, and the second
        round's is the accumulated total -- matching the final_response total.
        """
        f = tmp_path / "a.txt"
        f.write_text("x")
        stub.queue(
            completion(usage=(100, 20),
                       tool_calls=[tool_call("tc1", "read_file", {"path": str(f)})]),
            completion(usage=(50, 10),
                       tool_calls=[tool_call("tc2", "read_file", {"path": str(f)})]),
            completion(content="done", usage=(200, 30)),
        )
        events = collect_auto(client.chat(
            [{"role": "user", "content": "go"}], tool_confirmation="all"))

        requests = [e for e in events if e["type"] == "tool_request"]
        assert len(requests) == 2
        # Round 1: just that round. Round 2: 100+50 / 20+10 accumulated.
        assert (requests[0]["usage"]["prompt_tokens"],
                requests[0]["usage"]["completion_tokens"]) == (100, 20)
        assert (requests[1]["usage"]["prompt_tokens"],
                requests[1]["usage"]["completion_tokens"]) == (150, 30)
        # final_response still reports the whole turn (350/60).
        final = events[-1]
        assert final["type"] == "final_response"
        assert final["usage"]["total_tokens"] == 410

    def test_question_request_carries_usage(self, stub, client):
        """The ask_user_question round has no tool_request, so it carries usage itself."""
        stub.queue(
            completion(usage=(70, 7), tool_calls=[
                tool_call("q1", "ask_user_question", {"questions": [
                    {"header": "H", "question": "Q?", "options": [
                        {"label": "A", "description": "a"}]}]})]),
            completion(content="done", usage=(30, 3)),
        )
        gen = client.chat([{"role": "user", "content": "go"}], tool_confirmation="all")
        events = []
        for ev in gen:
            events.append(ev)
            if ev["type"] == "question_request":
                gen.send({"answered": True, "answers": ["A"]})
            if ev["type"] == "final_response":
                break
        q = next(e for e in events if e["type"] == "question_request")
        assert q["usage"]["prompt_tokens"] == 70
        assert q["usage"]["completion_tokens"] == 7

    def test_malformed_arguments_fall_back_to_empty(self, stub, client):
        stub.queue(
            completion(tool_calls=[tool_call("tc1", "read_file", "{not json!")]),
            completion(content="done"),
        )
        events = collect_auto(client.chat(
            [{"role": "user", "content": "go"}], tool_confirmation="all"))
        req = next(e for e in events if e["type"] == "tool_request")
        assert req["args"] == {}
        result = next(e for e in events if e["type"] == "tool_result")
        # read_file with no path returns an error string, not an exception
        assert result["declined"] is False
        assert isinstance(result["content"], str)
        assert next(e for e in events if e["type"] == "final_response")

    def test_api_error_raises_and_resets_client(self, stub, client):
        # Empty queue → stub returns HTTP 500; client must raise, not hang
        gen = client.chat([{"role": "user", "content": "hi"}])
        with pytest.raises(Exception):
            next(gen)


# ── Tests: read_image attachment ───────────────────────────────────────────────

class TestReadImageAttachment:
    """read_image can't return a picture through a role:"tool" message, so the
    loop attaches it as a follow-up user message.  These tests pin the shape of
    the request the model actually receives."""

    @staticmethod
    def _png(tmp_path, name="shot.png", size=(48, 32)):
        from PIL import Image
        path = tmp_path / name
        Image.new("RGB", size, (10, 120, 200)).save(path)
        return path

    def test_image_attached_as_user_message_after_tool_result(
            self, stub, client, tmp_path):
        path = self._png(tmp_path)
        stub.queue(
            completion(tool_calls=[tool_call("c1", "read_image", {"path": str(path)})]),
            completion(content="It is a blue rectangle."),
        )
        events = collect_auto(client.chat(
            [{"role": "user", "content": "what is in the image?"}],
            tool_confirmation="all",
        ))

        result = [e for e in events if e["type"] == "tool_result"][0]
        assert "Loaded shot.png" in result["content"]
        assert "48×32" in result["content"]

        # Second request carries the picture the first one produced.
        sent = stub.requests[1]["body"]["messages"]
        tool_msg = [m for m in sent if m["role"] == "tool"][0]
        assert isinstance(tool_msg["content"], str), "tool content must stay a string"

        attached = sent[-1]
        assert attached["role"] == "user"
        images = [p for p in attached["content"] if p["type"] == "image_url"]
        assert len(images) == 1
        assert images[0]["image_url"]["url"].startswith("data:image/")
        assert ";base64," in images[0]["image_url"]["url"]

    def test_tool_message_directly_follows_assistant(self, stub, client, tmp_path):
        """The attachment must not be wedged between an assistant tool_calls
        message and its matching tool result — that is an API error."""
        path = self._png(tmp_path)
        stub.queue(
            completion(tool_calls=[
                tool_call("c1", "read_image", {"path": str(path)}),
                tool_call("c2", "read_file", {"path": str(tmp_path / "n.txt")}),
            ]),
            completion(content="done"),
        )
        (tmp_path / "n.txt").write_text("hi")
        collect_auto(client.chat(
            [{"role": "user", "content": "look"}], tool_confirmation="all"))

        roles = [m["role"] for m in stub.requests[1]["body"]["messages"]]
        first_tool = roles.index("tool")
        assert roles[first_tool - 1] == "assistant"
        assert roles[first_tool:first_tool + 2] == ["tool", "tool"]
        assert roles[-1] == "user"

    def test_declined_read_image_attaches_nothing(self, stub, client, tmp_path):
        path = self._png(tmp_path)
        stub.queue(
            completion(tool_calls=[tool_call("c1", "read_image", {"path": str(path)})]),
            completion(content="ok"),
        )
        gen = client.chat([{"role": "user", "content": "look"}],
                          tool_confirmation="none")
        events, pending = [], None
        try:
            ev = next(gen)
            while True:
                events.append(ev)
                if ev["type"] == "tool_request":
                    ev = gen.send({"confirmed": False})
                else:
                    ev = next(gen)
        except StopIteration:
            pass

        assert all(
            m["role"] != "user" or isinstance(m["content"], str)
            for m in stub.requests[1]["body"]["messages"]
        ), "nothing should be attached when the call was declined"

    def test_error_result_attaches_nothing(self, stub, client, tmp_path):
        stub.queue(
            completion(tool_calls=[
                tool_call("c1", "read_image", {"path": str(tmp_path / "nope.png")})]),
            completion(content="ok"),
        )
        collect_auto(client.chat([{"role": "user", "content": "look"}],
                                 tool_confirmation="all"))
        assert all(
            isinstance(m["content"], str)
            for m in stub.requests[1]["body"]["messages"] if m["role"] == "user"
        )


def test_rate_calculation_uses_output_not_prompt_or_total():
    from types import SimpleNamespace
    from pengy.core.llm_client import _response_tokens_per_second
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=30, total_tokens=1030)
    assert _response_tokens_per_second(usage, 2) == 15
    assert _response_tokens_per_second(usage, 0) is None
    assert _response_tokens_per_second(usage, float("nan")) is None
    assert _response_tokens_per_second(None, 2) is None
    assert _response_tokens_per_second(SimpleNamespace(completion_tokens=0), 2) == 0


def test_final_rate_is_not_accumulated_across_tool_calls(stub, client, monkeypatch, tmp_path):
    import pengy.core.llm_client as module
    path = tmp_path / "note.txt"
    path.write_text("ok")
    stub.queue(completion(tool_calls=[tool_call("tc", "read_file", {"path": str(path)})], usage=(100, 20)),
               completion(content="done", usage=(200, 30)))
    ticks = iter([0.0, 100.0, 200.0, 202.0])
    # Replace only the module time object; do not alter the SDK's clocks.
    from types import SimpleNamespace
    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    events = collect_auto(client.chat([{"role": "user", "content": "read"}], tool_confirmation="all"))
    final = events[-1]
    assert final["usage"]["completion_tokens"] == 50
    assert final["tokens_per_second"] == 15.0


def test_final_response_without_usage_has_no_rate(stub, client):
    body = completion(content="ok")
    body.pop("usage")
    stub.queue(body)
    assert collect_auto(client.chat([{"role": "user", "content": "hi"}]))[-1]["tokens_per_second"] is None
