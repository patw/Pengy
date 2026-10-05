"""Canonical provider-recovery fixtures shared by all three editions."""
import copy
import json
from pathlib import Path
from pengy.core.context_recovery import Recovery, fingerprint

root = Path(__file__).resolve().parents[1]
fixtures=[]
for name, kind in [('reasoning','reasoning'),('old_tool','tool'),('history_summary','summary'),('protected_only','none'),('synthetic_active_image','reasoning')]:
    if kind=='summary':
        messages=[{'role':'system','content':'Keep production unchanged.'}]
        for n in range(6):
            messages += [{'role':'user','content':f'Requirement {n}: /tmp/project-{n}; value {17*n}. '+ 'background '*100}, {'role':'assistant','content':f'Decision {n}: done; audit pending.'}]
        messages += [{'role':'user','content':'Current request: report requirement 0 and pending audit.'}]
    elif kind=='none':messages=[{'role':'system','content':'Policy'}, {'role':'user','content':'Current request '+ 'x'*9000}]
    elif kind=='tool':messages=[{'role':'system','content':'Policy'}, {'role':'user','content':'Read'}, {'role':'assistant','content':'','tool_calls':[{'id':'a','type':'function','function':{'name':'read_file','arguments':'{"path":"x"}'}}]}, {'role':'tool','tool_call_id':'a','content':'A'*12000}, {'role':'user','content':'Report'}]
    else:
        messages=[{'role':'system','content':'Policy'}, {'role':'user','content':'Old task'}, {'role':'assistant','content':'Done','reasoning_content':'thought '*1000}, {'role':'user','content':'New task'}, {'role':'assistant','content':'','reasoning_content':'ACTIVE_REASONING','reasoning_details':{'format':'openai-proxy/reasoning-v1','proxy_model':'m','blocks':['opaque']},'tool_calls':[{'id':'a','type':'function','function':{'name':'read_image','arguments':'{"path":"x"}'}}]}, {'role':'tool','tool_call_id':'a','content':'Image loaded'}]
        if name=='synthetic_active_image':messages += [{'role':'user','content':[{'type':'text','text':'Image loaded by read_image: x'},{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]}]
    summary='Requirement 0: /tmp/project-0; value 0. Decision 0: done; audit pending.'
    recovery=Recovery(messages,'test','m')
    event=recovery.reduce(messages,lambda _:summary)
    fixtures.append({'name':name,'messages':messages,'summary':summary,'strategy':event['strategy'] if event else None,'expected':recovery.apply(messages),'fingerprints':[fingerprint(m) for m in messages]})
for repo in ['Pengy','PengyR','PengyCPP']:
    path=root.parent/repo/'tests/fixtures/context_recovery.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(fixtures,indent=2,ensure_ascii=False)+'\n')
print('Shared recovery fixtures written')
