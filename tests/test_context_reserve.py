"""Final retry must reach summary without replaying completed tool calls."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pengy.core.context_recovery import Recovery, MAX_RETRIES, STUB
from pengy.core.chat_manager import clean_dangling_tool_calls
from tests.test_context_recovery import client, stub, _completion_obj, overflow
from tests.test_llm_loop import completion, collect_auto
from tests.test_image_recovery import wire


def cases():
    return json.loads((Path(__file__).parent / 'fixtures/context_recovery_reserve.json').read_text())


@pytest.mark.parametrize('case', cases(), ids=lambda c: c['name'])
def test_shared_reservation_fixtures(case):
    messages = copy.deepcopy(case['messages'])
    original = copy.deepcopy(messages)
    recovery = Recovery(messages, 'test', 'm', keep_turns=case['keep_turns'])
    recovery.attempts = case['initial_attempts']
    recovery.summary_calls = case['summary_calls']
    events = []
    for _ in range(MAX_RETRIES - recovery.attempts):
        def summarize(source):
            if case['failure'] == 'failure': raise RuntimeError('incomplete summary')
            return source if case['failure'] == 'nonreducing' else case['summary']
        before = copy.deepcopy(recovery.state)
        try:
            event = recovery.reduce(messages, summarize)
        except RuntimeError:
            assert recovery.state == before
            break
        if event is None:
            assert recovery.state == before
            break
        events.append(event['strategy'])
    assert events == case['strategies']
    assert recovery.attempts == case['expected_attempts']
    assert recovery.apply(messages) == case['expected']
    assert messages == original
    if not case['failure']:
        assert recovery.reduce(messages, lambda _: pytest.fail('over-budget summary')) is None


@pytest.mark.parametrize('signal', ['context_error', 'empty_length'])
def test_real_loop_reserves_fourth_attempt_for_summary(client, signal):
    case = cases()[0]
    messages = case['messages']
    original = copy.deepcopy(messages)
    requests = []
    blank = completion(content=None); blank['choices'][0]['finish_reason'] = 'length'
    task_calls = 0
    def create(**kwargs):
        nonlocal task_calls
        requests.append(copy.deepcopy(kwargs))
        if 'tools' not in kwargs:
            return _completion_obj(completion(content=case['summary']))
        task_calls += 1
        if task_calls <= 4:
            if signal == 'context_error': raise overflow()
            return _completion_obj(blank)
        return _completion_obj(completion(content='HARBOR_17 /tmp/harbor-17 audit pending'))
    with patch.object(client.client.chat.completions, 'create', side_effect=create), patch('pengy.core.llm_client._run_tool') as run:
        events = collect_auto(client.chat(messages))
    assert [e['strategy'] for e in events if e['type'] == 'context_compacted'] == [
        'tool_previews', 'tool_previews', 'tool_stubs', 'history_summary']
    assert events[-2]['attempt'] == 4
    assert task_calls == 5 and len(requests) == 6
    assert len([r for r in requests if 'tools' not in r]) == 1
    run.assert_not_called()
    outgoing = requests[-1]['messages']
    assert 'HARBOR_17' in outgoing[1]['content']
    # Latest active result keeps its preview rather than using the last retry
    # to stub it; matching call/result structure and active request survive.
    assert outgoing[-1]['role'] == 'tool' and outgoing[-1]['content'] != STUB
    assert outgoing[-2]['tool_calls'][0]['id'] == 'newest'
    assert outgoing == clean_dangling_tool_calls(outgoing)
    assert messages == original


def test_reserved_summary_failure_keeps_checkpoint_and_never_falls_back(client, tmp_path, monkeypatch):
    monkeypatch.setattr('pengy.core.config._config_dir_override', str(tmp_path))
    case = cases()[0]
    task_calls = 0
    def create(**kwargs):
        nonlocal task_calls
        if 'tools' not in kwargs:
            reply = completion(content='partial summary'); reply['choices'][0]['finish_reason'] = 'length'
            return _completion_obj(reply)
        task_calls += 1
        raise overflow()
    with patch.object(client.client.chat.completions, 'create', side_effect=create):
        with pytest.raises(RuntimeError, match='summary was incomplete'):
            collect_auto(client.chat(case['messages'], chat_id='reserved-failure'))
    assert task_calls == 4  # no 5th task call with discarded/fallback history
    resumed = Recovery(case['messages'], client.base_url, client.model, chat_id='reserved-failure')
    assert resumed.state['drop'] == 0 and not resumed.state['summary']
    assert resumed.state['tools']  # three successful prior reductions persist


def test_web_worker_reports_reserved_summary_and_keeps_full_transcript(wire, tmp_path, monkeypatch):
    from pengy.web.app import WebWorker
    from pengy.core.config import DEFAULTS
    from pengy.core.chat_manager import create_chat, save_chat, get_chat
    monkeypatch.setattr('pengy.core.config._config_dir_override', str(tmp_path))
    server, llm = wire
    case = cases()[0]
    chat = create_chat()
    chat['messages'] = copy.deepcopy(case['messages'])
    save_chat(chat)
    server.replies = [(400, {'error': {'code': 'context_length_exceeded', 'message': 'context length exceeded'}})] * 4
    server.replies += [(200, completion(content=case['summary'])), (200, completion(content='HARBOR_17 /tmp/harbor-17 audit pending'))]
    config = {**DEFAULTS, 'base_url': llm.base_url, 'api_key': 'test-key', 'model': 'stub-model'}
    worker = WebWorker(chat, config)
    worker.start(); worker._thread.join(timeout=10)
    assert not worker._thread.is_alive()
    events = list(worker.iter_events())
    retries = [e for e in events if e['type'] == 'context_compacted']
    assert [e['strategy'] for e in retries] == case['strategies']
    assert retries[-1]['attempt'] == 4 and retries[-1]['turns_summarized'] > 0
    assert events[-1]['type'] == 'final_response'
    saved = get_chat(chat['id'])
    assert saved['messages'][:-1] == case['messages']
    assert len(server.requests) == 6
