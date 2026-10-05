"""Black-box native CLI recovery against Strata; scratch configs, no tools executed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

parser=argparse.ArgumentParser()
parser.add_argument('--url',required=True)
parser.add_argument('--output-dir',required=True)
parser.add_argument('--rust-cli',required=True)
parser.add_argument('--cpp-cli',required=True)
args=parser.parse_args()
root=Path(args.output_dir);root.mkdir(exist_ok=True)
report={}
for name,binary in [('rust',args.rust_cli),('cpp',args.cpp_cli)]:
    path=root/name;path.mkdir(exist_ok=True)
    (path/'chats').mkdir(exist_ok=True)
    settings={'base_url':args.url,'api_key':'anything','model':'anything','system_message':'Do not call tools. Answer with only the requested marker value.','tool_confirmation':'none','output_token_limit':128000,'auto_context_recovery':True}
    (path/'settings.json').write_text(json.dumps(settings))
    chat_id=str(uuid.uuid4())
    text='VERIFIED_TOKEN=NATIVE_RECOVERY_OK\n'+'padding '*145000+'\nVERIFIED_TOKEN=NATIVE_RECOVERY_OK'
    messages=[{'role':'user','content':'Read the synthetic report.'},{'role':'assistant','content':'','tool_calls':[{'id':'complete','type':'function','function':{'name':'read_file','arguments':'{"path":"synthetic-report"}'}}]},{'role':'tool','tool_call_id':'complete','content':text}]
    chat={'id':chat_id,'title':'Native recovery validation','created_at':'2999-01-01T00:00:00','messages':messages}
    (path/'chats'/f'{chat_id}.json').write_text(json.dumps(chat))
    env={**os.environ,'PENGY_CONFIG_DIR':str(path),'HOME':str(path/'home')}
    start=time.monotonic()
    if name=='rust':
        # Rust one-shot starts a new chat; interactive mode resumes seeded history.
        result=subprocess.run([binary],input='What is VERIFIED_TOKEN? Reply with only its value.\n/quit\n',text=True,capture_output=True,env=env,timeout=300)
        assert result.returncode==0,result.stderr
        assert 'NATIVE_RECOVERY_OK' in result.stdout,result.stdout[-1000:]
        assert 'Context recovery' in result.stderr,result.stderr
    else:
        result=subprocess.run([binary,'--output','json','What is VERIFIED_TOKEN? Reply with only its value.'],text=True,capture_output=True,env=env,timeout=300)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['content'].strip()=='NATIVE_RECOVERY_OK',result.stdout
        assert 'Context recovery' in result.stderr,result.stderr
    saved=json.loads((path/'chats'/f'{chat_id}.json').read_text())
    assert saved['messages'][2]['content']==text
    assert (path/'context_recovery').is_dir()
    report[name]={'seconds':time.monotonic()-start,'recovery_reported':True,'full_tool_result_retained':True,'answer':'NATIVE_RECOVERY_OK','exit_code':result.returncode}
    print(name,'LIVE_NATIVE_PASS',report[name],flush=True)
(root/'report.json').write_text(json.dumps(report,indent=2))
