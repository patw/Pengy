"""Parity contracts for measured proactive recovery; no real provider calls."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from pengy.core.context_pressure import Pressure, input_chars, budgets, identity
from pengy.core import tools, context_windows
from pengy.core.context_recovery import Recovery
from pengy.core.llm_client import GenerationLimitError, EmptyResponseError, ConfigError
from tests.test_context_recovery import client, stub, _completion_obj, overflow
from tests.test_auto_context_recovery import history, length_stop
from tests.test_llm_loop import completion, tool_call, collect_auto


def seed(client, messages, chat_id, window, tokens):
    recovery = Recovery(messages, client.base_url, client.model, chat_id=chat_id)
    key = json.dumps([client.base_url.rstrip('/'), client.model, chat_id])
    p = Pressure.load(key, messages, identity(recovery.state), window)
    p.record(input_chars(messages, tools.TOOLS), tokens)
    p.save(key, messages, identity(recovery.state))


def test_shared_arithmetic_and_hysteresis():
    fixture=json.loads((Path(__file__).parent/'fixtures/context_pressure.json').read_text())
    for case in fixture['cases']:
        p=Pressure(262144)
        if case['sample']:p.record(*case['sample'])
        assert p.estimate(case['next_chars'])==case['expected']
    p=Pressure(262144);at,hard=budgets(p.window,75,0)
    assert at==196608 and not p.should_compact(at-1,at,hard) and p.should_compact(at,at,hard)
    p.compacted(at-1,at);assert not p.should_compact(at,at,hard)
    assert not p.should_compact(at+13107,at,hard) and p.should_compact(at+13108,at,hard)
    assert p.should_compact(hard,at,hard)
    p.compacted(at,at);assert p.should_compact(at,at,hard)
    assert budgets(262144,75,100000)==(162144,162144)
    assert budgets(262144,75,100000,True)==(196608,262144)
    assert input_chars([{'image_url':{'url':'data:base64'}}],[]) is None
    assert input_chars([{'role':'user','content':'🐧\n'}],None)==17


@pytest.mark.parametrize('window,tokens,reduce',[(262144,196608,True),(262144,196607,False),(0,250000,False),(1048576,250000,False)])
def test_threshold_unknown_fast_and_protected_transcript(client,stub,tmp_path,monkeypatch,window,tokens,reduce):
    monkeypatch.setattr('pengy.core.config._config_dir_override',str(tmp_path))
    messages=history();original=copy.deepcopy(messages)
    seed(client,messages,'test',window,tokens)
    if reduce:stub.queue(completion(content='checkpoint'))
    stub.queue(completion(content='answer'))
    events=collect_auto(client.chat(messages,chat_id='test',context_window=window))
    compact=[e for e in events if e['type']=='context_compacted']
    assert len(compact)==int(reduce)
    if reduce:
        assert compact[0]['reason']=='proactive' and compact[0]['attempt']==1
        assert 'before the window fills' in compact[0]['message']
        assert 'Current task' not in stub.requests[0]['body']['messages'][1]['content']
        assert stub.requests[-1]['body']['messages'][-1]==messages[-1]
    assert messages==original and events[-1]['content']=='answer'
    if not reduce:
        from pengy.core.config import get_config_dir
        assert not (get_config_dir()/'context_recovery').exists()


def test_cold_start_then_tool_round_checks_before_sending(client,stub):
    output='HEAD\n'+'log id=1234567890 '*10000+'\nTAIL'
    stub.queue(completion(tool_calls=[tool_call('one','read_file',{'path':'synthetic'})],usage=(180000,5)),completion(content='answer'))
    with patch('pengy.core.llm_client._run_tool',return_value=output) as run:
        events=collect_auto(client.chat([{'role':'user','content':'read'}],tool_confirmation='all',context_window=262144))
    assert run.call_count==1
    assert [e['type'] for e in events]==['assistant_tool_calls','tool_request','tool_result','context_compacted','final_response']
    assert events[2]['content']==output and events[3]['reason']=='proactive'
    assert len(stub.requests[1]['body']['messages'][-1]['content'])<4000


def test_calibration_survives_user_turns(client,stub):
    messages=history()
    stub.queue(completion(content='first',usage=(200000,5)),completion(content='checkpoint'),completion(content='second'),completion(content='third',usage=(100,5)))
    first=collect_auto(client.chat(messages,chat_id='turns',context_window=262144))
    assert [e['type'] for e in first]==['final_response']
    messages += [first[-1]['message'],{'role':'user','content':'continue'}]
    second=collect_auto(client.chat(messages,chat_id='turns',context_window=262144))
    assert second[0]['reason']=='proactive'
    # Editing a prefix invalidates calibration, not an inferred cold estimate.
    messages[0]['content']='different instructions'
    third=collect_auto(client.chat(messages,chat_id='turns',context_window=262144))
    assert not any(e['type']=='context_compacted' for e in third)


def test_failed_proactive_summary_sends_original_and_usage_not_calibrated_from_summary(client):
    messages=history();seed(client,messages,'fail',262144,200000);requests=[]
    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        if 'tools' not in kwargs:raise overflow('Invalid summary request')
        return _completion_obj(completion(content='answer',usage=(200000,5)))
    with patch.object(client.client.chat.completions,'create',side_effect=create):
        events=collect_auto(client.chat(messages,chat_id='fail',context_window=262144))
    assert [e['type'] for e in events]==['final_response']
    assert requests[-1]['messages']==messages


def test_no_reducible_plan_sends_and_impossible_reserve_rejected(client,stub):
    messages=[{'role':'user','content':'protected'}];seed(client,messages,'protected',262144,200000)
    stub.queue(completion(content='answer'))
    assert collect_auto(client.chat(messages,chat_id='protected',context_window=262144))[-1]['content']=='answer'
    with pytest.raises(ConfigError,match='must be smaller'):
        collect_auto(client.chat(messages,context_window=262144,output_token_limit=262144))


@pytest.mark.parametrize('success',[True,False])
def test_long_length_one_reduction_and_no_truncated_tool_execution(client,stub,success):
    call=tool_call('unsafe','write_file',{'path':'must-not-exist','content':'no'})
    stub.queue(length_stop('partial',[call],393216),completion(content='checkpoint'),
               completion(content='answer') if success else length_stop('still partial',completion_tokens=393216))
    events=[]
    with patch('pengy.core.llm_client._run_tool') as run:
        if success:events=collect_auto(client.chat(history(),tool_confirmation='all'))
        else:
            with pytest.raises(GenerationLimitError):
                for e in client.chat(history(),tool_confirmation='all'):events.append(e)
    run.assert_not_called();assert len([e for e in events if e['type']=='context_compacted'])==1
    assert len(stub.requests)==3


@pytest.mark.parametrize('mechanism',['reasoning_effort','enable_thinking',''])
@pytest.mark.parametrize('success',[True,False])
def test_reasoning_only_empty_bounded_retry(client,stub,mechanism,success):
    empty=completion(content='',usage=(100,200));empty['choices'][0]['message']['reasoning_content']='thoughts'
    stub.queue(empty)
    if mechanism:stub.queue(completion(content='ok',usage=(100,2)) if success else empty)
    if not mechanism or not success:
        with pytest.raises(EmptyResponseError):collect_auto(client.chat([{'role':'user','content':'answer'}],empty_reply_thinking_off=mechanism,reasoning_effort='high'))
    else:
        events=collect_auto(client.chat([{'role':'user','content':'answer'}],empty_reply_thinking_off=mechanism,reasoning_effort='high'))
        assert events[-1]['usage']['completion_tokens']==202
    assert len(stub.requests)==(2 if mechanism else 1)
    if mechanism:
        body=stub.requests[-1]['body']
        if mechanism=='reasoning_effort':assert body['reasoning_effort']=='none'
        else:assert body['chat_template_kwargs']=={'enable_thinking':False} and 'reasoning_effort' not in body
        assert body['messages']==stub.requests[0]['body']['messages']


def test_discovery_formats_cache_and_override(tmp_path,monkeypatch):
    monkeypatch.setattr('pengy.core.config._config_dir_override',str(tmp_path))
    body={'data':[{'id':'fast','context_window':1048576},{'id':'local','meta':{'n_ctx':262144}},{'id':'limited','max_input_tokens':131072}]}
    assert context_windows.model_window(body,'fast')['tokens']==1048576
    assert context_windows.model_window(body,'limited')['input_only']
    assert context_windows.error_window('prompt (405835 tokens) leaves no room to answer in the context (262144)')['tokens']==262144
    assert context_windows.error_window('maximum output tokens 393216') is None
    context_windows.remember('http://host/v1','local',{'tokens':262144,'input_only':False},'models')
    assert context_windows.resolve('http://host/v1','local','',auto=True)['tokens']==262144
    assert context_windows.resolve('http://host/v1','local','',auto=False) is None
    assert context_windows.resolve('http://host/v1','local','',windows={'local':0},auto=True) is None


def test_summary_uses_only_configured_thinking_off(client,stub):
    for mechanism in ('','reasoning_effort','enable_thinking'):
        messages=history();chat_id='summary-'+mechanism
        seed(client,messages,chat_id,262144,200000)
        stub.queue(completion(content='checkpoint'),completion(content='answer'))
        events=collect_auto(client.chat(messages,chat_id=chat_id,context_window=262144,
                                       empty_reply_thinking_off=mechanism,reasoning_effort='high'))
        assert events[-1]['content']=='answer'
        summary=stub.requests[-2]['body'];normal=stub.requests[-1]['body']
        assert 'tools' not in summary and summary['max_tokens']==2048
        if mechanism=='reasoning_effort':assert summary['reasoning_effort']=='none'
        elif mechanism=='enable_thinking':assert summary['chat_template_kwargs']=={'enable_thinking':False}
        else:assert 'reasoning_effort' not in summary and 'chat_template_kwargs' not in summary
        assert normal['reasoning_effort']=='high'


def test_proactive_budget_preserves_reactive_attempts(client):
    fixture=json.loads((Path(__file__).parent/'fixtures/context_recovery_reserve.json').read_text())[0]
    messages=fixture['messages'];seed(client,messages,'budget',262144,260000)
    replies=[completion(tool_calls=[tool_call('one','read_file',{'path':'synthetic'})],usage=(260000,5)),
             completion(tool_calls=[tool_call('two','read_file',{'path':'synthetic'})],usage=(260000,5)),
             overflow(),overflow(),completion(content='checkpoint'),completion(content='answer')]
    requests=[]
    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs));value=replies.pop(0)
        if isinstance(value,Exception):raise value
        return _completion_obj(value)
    with patch.object(client.client.chat.completions,'create',side_effect=create),patch('pengy.core.llm_client._run_tool',return_value='small') as run:
        events=collect_auto(client.chat(messages,chat_id='budget',context_window=262144,tool_confirmation='all'))
    compact=[e for e in events if e['type']=='context_compacted']
    assert len(compact)==4 and [e['attempt'] for e in compact]==[1,2,3,4]
    assert sum(e.get('reason')=='proactive' for e in compact)==2
    assert run.call_count==2 and len(requests)==6


def test_missing_usage_does_not_calibrate(client,stub):
    messages=history();first=completion(content='first');first.pop('usage')
    stub.queue(first,completion(content='second'))
    events=collect_auto(client.chat(messages,chat_id='missing',context_window=262144))
    messages += [events[-1]['message'],{'role':'user','content':'continue'}]
    second=collect_auto(client.chat(messages,chat_id='missing',context_window=262144))
    assert [e['type'] for e in second]==['final_response']


def test_frontends_share_full_settings_contract():
    from pengy.core.config import DEFAULTS,recovery_options
    options=recovery_options({**DEFAULTS,'context_windows':{'fast':1048576,'local':262144}})
    assert options['context_windows']['local']==262144 and options['compact_at_percent']==75
    assert options['context_window']==0 and not options['context_window_auto']
    assert options['empty_reply_thinking_off']==''
    for file in ('pengy/cli/main.py','pengy/ui/main_window.py','pengy/web/app.py'):
        source=(Path(__file__).parent.parent/file).read_text()
        assert '**recovery_options(' in source


def test_discovery_probes_selected_model_once_and_negative_caches(tmp_path,monkeypatch):
    import io
    monkeypatch.setattr('pengy.core.config._config_dir_override',str(tmp_path))
    calls=[]
    class Response(io.BytesIO):
        def read1(self,n):return self.read(n)
    def get(req,timeout):
        calls.append(req.full_url)
        if req.full_url.endswith('/models'):
            return Response(json.dumps({'data':[{'id':'local','meta':{'n_ctx':262144}}]}).encode())
        return Response(b'{}')
    monkeypatch.setattr(context_windows.urllib.request,'urlopen',get)
    assert context_windows.resolve('http://host/prefix/v1','local','key',auto=True)['tokens']==262144
    assert calls==['http://host/prefix/v1/models']
    assert context_windows.resolve('http://host/prefix/v1','local','key',auto=True)['tokens']==262144
    assert len(calls)==1
    assert context_windows.resolve('http://host/prefix/v1','other','key',auto=True) is None
    assert calls[-2:]==['http://host/prefix/props','http://host/prefix/health']
    count=len(calls)
    assert context_windows.resolve('http://host/prefix/v1','other','key',auto=True) is None
    assert len(calls)==count
