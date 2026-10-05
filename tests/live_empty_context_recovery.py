"""Controlled empty-length reproduction at the actual Strata context wall."""
import argparse
import copy
import json
import re
import time
from pathlib import Path
from openai import BadRequestError
from pengy.core.config import set_config_dir
from pengy.core.llm_client import LLMClient, GenerationLimitError
from pengy.core import tools
from tests.test_llm_loop import tool_call

parser = argparse.ArgumentParser()
parser.add_argument('--url', required=True)
parser.add_argument('--output-dir', required=True)
args = parser.parse_args()
root=Path(args.output_dir);root.mkdir(parents=True,exist_ok=True)
set_config_dir(str(root/'config'))
client=LLMClient(args.url,'anything','anything',llm_timeout=300)

def history(n):
    return [{'role':'system','content':'Do not call tools. Use the completed report. Follow the current user task.'},
        {'role':'user','content':'Read a synthetic report.'},
        {'role':'assistant','content':'','tool_calls':[tool_call('completed','read_file',{'path':'synthetic-report'})]},
        {'role':'tool','tool_call_id':'completed','content':'VERIFIED_TOKEN=EMPTY_RECOVERY_OK\n'+'padding '*n+'\nVERIFIED_TOKEN=EMPTY_RECOVERY_OK'},
        {'role':'user','content':'Consider carefully the reliability of extracting a marker from a long report, potential duplicated markers, and consistency between start and end. Then answer with only the exact VERIFIED_TOKEN value. Do not call tools.'}]

n=250000; calibration=[]
for attempt in range(8):
    messages=history(n)
    try:
        client.client.chat.completions.create(model='anything',messages=messages,tools=tools.TOOLS,tool_choice='auto',max_tokens=999999)
        raise AssertionError('Expected context rejection')
    except BadRequestError as error:
        match=re.search(r'prompt \((\d+) tokens\)',str(error))
        if not match:raise
        tokens=int(match.group(1));room=262144-8-tokens
        calibration.append({'padding_words':n,'prompt_tokens':tokens,'room':room})
        print('calibrate',calibration[-1],flush=True)
        if room==24:break
        n += room-24
else:raise AssertionError('Could not calibrate exact context wall')
messages=history(n);original=copy.deepcopy(messages)
start=time.monotonic()
blank=[]
try:
    blank=list(client.chat(messages,auto_context_recovery=False))
    raise AssertionError('Expected no answer at 24-token room: '+repr(blank[-1]))
except GenerationLimitError as error:
    print('baseline failed as expected',str(error),flush=True)
events=[]
for e in client.chat(messages,chat_id='empty-length-live'):
    events.append(e);print(e['type'],e.get('message','') if e['type']=='context_compacted' else e.get('content',''),flush=True)
    if e['type'] in ('tool_request','question_request'):raise AssertionError('No tools allowed')
assert events[-1]['type']=='final_response'
assert events[-1]['content'].strip()=='EMPTY_RECOVERY_OK',events[-1]
assert any(e['type']=='context_compacted' for e in events)
assert messages==original
report={'calibration':calibration,'baseline':'GenerationLimitError (length before answer)','recovery_events':[e['type'] for e in events],
    'strategies':[e.get('strategy') for e in events if e['type']=='context_compacted'],'answer':events[-1]['content'],'usage':events[-1]['usage'],'seconds':time.monotonic()-start,'history_preserved':True}
(root/'report.json').write_text(json.dumps(report,indent=2))
print('EMPTY_LENGTH_LIVE_PASS',flush=True)
