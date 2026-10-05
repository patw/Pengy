"""HTTP 200 length completions must not become successful assistant answers."""
import copy
from unittest.mock import patch

import pytest

from pengy.core.llm_client import GenerationLimitError
from tests.test_llm_loop import completion, tool_call, collect_auto
from tests.test_context_recovery import client, stub  # shared isolated fixtures


def truncated(content, tool_calls=None):
    reply = completion(content=content, tool_calls=tool_calls)
    reply["choices"][0]["finish_reason"] = "length"
    return reply


@pytest.mark.parametrize("content", [None, "", " \n\t"])
def test_empty_length_is_failed_without_retry_or_history(client, stub, content):
    history = [{"role": "user", "content": "think hard"}]
    original = copy.deepcopy(history)
    stub.queue(truncated(content))
    events = []
    with pytest.raises(GenerationLimitError, match="before an answer was produced") as error:
        for event in client.chat(history):
            events.append(event)
    assert error.value.kind == "truncated"
    assert error.value.exit_code == 1
    assert "output-token cap or insufficient remaining context" in str(error.value)
    assert len(stub.requests) == 1
    assert events == []
    assert history == original


def test_partial_length_is_explicitly_incomplete_not_a_final_response(client, stub):
    text = "## Strategy\nOnly the first step… 🐧"
    stub.queue(truncated(text))
    with pytest.raises(GenerationLimitError, match="answer is incomplete") as error:
        collect_auto(client.chat([{"role": "user", "content": "plan"}]))
    assert text in str(error.value)
    assert "not saved as an answer" in str(error.value)
    assert len(stub.requests) == 1


@pytest.mark.parametrize("malformed", [False, True])
def test_length_tool_calls_never_execute_or_emit_assistant_history(client, stub, tmp_path, malformed):
    path = tmp_path / "must-not-exist"
    call = tool_call("tc1", "write_file", {"path": str(path), "content": "unsafe"})
    if malformed:
        call["function"]["arguments"] = '{"path":'
    stub.queue(truncated("About to write", [call]))
    history = [{"role": "user", "content": "write"}]
    original = copy.deepcopy(history)
    with patch("pengy.core.llm_client._run_tool") as run:
        with pytest.raises(GenerationLimitError, match="no tools from this response were executed"):
            collect_auto(client.chat(history, tool_confirmation="all"))
    run.assert_not_called()
    assert not path.exists()
    assert history == original
    assert len(stub.requests) == 1


@pytest.mark.parametrize("finish_reason", ["tool_calls", None])
def test_empty_tool_only_response_still_runs(client, stub, tmp_path, finish_reason):
    path = tmp_path / "safe.txt"
    first = completion(tool_calls=[tool_call("tc1", "write_file",
        {"path": str(path), "content": "done"})])
    if finish_reason is None:
        first["choices"][0].pop("finish_reason")
    else:
        first["choices"][0]["finish_reason"] = finish_reason
    stub.queue(first, completion(content="written"))
    events = collect_auto(client.chat([{"role": "user", "content": "write"}], tool_confirmation="all"))
    assert events[-1]["content"] == "written"
    assert path.read_text() == "done"


def test_truncated_turn_keeps_completed_tools_without_rerunning(client, stub, tmp_path):
    path = tmp_path / "already-done.txt"
    stub.queue(completion(tool_calls=[tool_call("tc1", "write_file",
        {"path": str(path), "content": "done"})]), truncated(None))
    events = []
    with pytest.raises(GenerationLimitError):
        for event in client.chat([{"role": "user", "content": "write"}], tool_confirmation="all"):
            events.append(event)
    assert [event["type"] for event in events] == ["assistant_tool_calls", "tool_request", "tool_result"]
    assert path.read_text() == "done"
    assert len(stub.requests) == 2


def test_cli_length_failure_exits_nonzero_with_json_and_no_bogus_history(stub, tmp_path, capsys, monkeypatch):
    from pengy.cli.main import PengyCLI
    from pengy.core.chat_manager import load_chats
    monkeypatch.setattr("pengy.core.config._config_dir_override", str(tmp_path))
    cli = PengyCLI()
    cli.config.update(base_url=stub.base_url, api_key="test-key", model="stub-model")
    cli._update_llm_client()
    cli._output_mode = "json"
    stub.queue(truncated(None))
    with pytest.raises(SystemExit) as error:
        cli.run_single_shot("think hard")
    assert error.value.code == 1
    import json
    output = capsys.readouterr()
    assert json.loads(output.out)["error"]["type"] == "truncated"
    assert "Generation limit reached" in output.err
    chats = load_chats()
    assert len(chats) == 1
    assert [msg["role"] for msg in chats[0]["messages"]] == ["user"]


@pytest.mark.parametrize("content", [None, "Partial answer… 🐧"])
def test_web_length_failure_emits_error_and_preserves_history(stub, tmp_path, monkeypatch, content):
    from pengy.web.app import WebWorker
    from pengy.core.config import DEFAULTS
    from pengy.core.chat_manager import create_chat, get_chat, save_chat
    monkeypatch.setattr("pengy.core.config._config_dir_override", str(tmp_path))
    chat = create_chat()
    chat["messages"] = [{"role": "user", "content": "think hard"}]
    save_chat(chat)
    config = {**DEFAULTS, "base_url": stub.base_url, "api_key": "test-key", "model": "stub-model"}
    stub.queue(truncated(content))
    worker = WebWorker(chat, config)
    worker.start()
    worker._thread.join(timeout=10)
    assert not worker._thread.is_alive()
    events = list(worker.iter_events())
    assert any(e["type"] == "error" and "Generation limit reached" in e["message"] for e in events)
    assert not any(e["type"] == "final_response" for e in events)
    assert get_chat(chat["id"])["messages"] == [{"role": "user", "content": "think hard"}]


def test_gui_worker_length_failure_emits_only_error(client, stub):
    pytest.importorskip("PySide6")
    from pengy.ui.chat_worker import ChatWorker
    stub.queue(truncated(None))
    worker = ChatWorker(client, [{"role": "user", "content": "think hard"}])
    events, errors, finished = [], [], []
    worker.response.connect(events.append)
    worker.error.connect(errors.append)
    worker.finished.connect(lambda: finished.append(True))
    worker.run()
    assert events == []
    assert len(errors) == 1 and "before an answer was produced" in errors[0]
    assert finished == [True]
