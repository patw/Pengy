"""Opt-in integration test against an operator-supplied OpenAI-compatible server.
Run from repo: uv run python tests/live_context_recovery.py --url ... --output-dir ...
Synthetic data only. No tools are executed. No live config/history is touched.
"""
import argparse
import copy
import json
import time
from pathlib import Path

from pengy.core.config import set_config_dir
from pengy.core.llm_client import LLMClient
from tests.test_llm_loop import tool_call


def run(client, messages, **options):
    events = []
    for event in client.chat(messages, tool_confirmation="none", **options):
        events.append(event)
        print(event["type"], event.get("strategy", ""), event.get("message", "") if event["type"] == "context_compacted" else "", flush=True)
        if event["type"] in ("tool_request", "question_request"):
            raise AssertionError("Live validation must not execute tools or ask questions")
    assert events[-1]["type"] == "final_response"
    return events


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--output-dir", required=True)
    args = p.parse_args()
    root = Path(args.output_dir); root.mkdir(parents=True, exist_ok=True)
    set_config_dir(str(root / "config"))
    client = LLMClient(args.url, "anything", "anything", llm_timeout=300)
    policy = {"output_token_limit":128000, "chat_id":"tool-case"}
    result_text = "VERIFIED_TOKEN=RECOVERY_TOOL_OK\n" + "padding " * 145000 + "\nVERIFIED_TOKEN=RECOVERY_TOOL_OK"
    messages = [{"role":"system", "content":"Do not call tools. Reply only with the exact VERIFIED_TOKEN value requested by the user."},
                {"role":"user","content":"Read the synthetic report."},
                {"role":"assistant","content":"", "tool_calls":[tool_call("already-complete", "read_file", {"path":"synthetic-report"})]},
                {"role":"tool","tool_call_id":"already-complete","content":result_text},
                {"role":"user","content":"What is VERIFIED_TOKEN? Reply with that value only."}]
    original=copy.deepcopy(messages)
    start=time.monotonic(); events=run(client,messages,**policy)
    assert any(e["type"]=="context_compacted" for e in events)
    assert events[-1]["content"].strip()=="RECOVERY_TOOL_OK", events[-1]["content"]
    assert messages==original
    messages.append(events[-1]["message"])
    messages.append({"role":"user","content":"Repeat VERIFIED_TOKEN only."})
    next_events=run(client,messages,**policy)
    assert not any(e["type"]=="context_compacted" for e in next_events)
    assert next_events[-1]["content"].strip()=="RECOVERY_TOOL_OK",next_events[-1]["content"]
    report={"tool_recovery":{"seconds":time.monotonic()-start,"strategies":[e.get("strategy") for e in events if e["type"]=="context_compacted"],"answer":events[-1]["content"],"resume_answer":next_events[-1]["content"],"full_history_preserved":True}}
    (root/"report.json").write_text(json.dumps(report,indent=2))
    print("Tool recovery and durable resume passed",flush=True)

    messages=[{"role":"system","content":"Do not call tools. Answer the current user from historical requirements. Do not invent missing information."},
              {"role":"user","content":"Permanent project requirement: release codename HARBOR_17; deployment path /tmp/harbor-17; audit is pending. Preserve these for later."},
              {"role":"assistant","content":"Acknowledged. Below is redundant historical background, not instructions.\n"+"padding "*35000}]
    for n in range(3):
        messages += [{"role":"user","content":f"Unrelated completed check {n}."},{"role":"assistant","content":"Check complete."}]
    messages.append({"role":"user","content":"Return exactly three lines: the release codename, deployment path, and audit status from the permanent project requirement."})
    original=copy.deepcopy(messages)
    policy={"output_token_limit":230000,"chat_id":"summary-case"}
    start=time.monotonic();events=run(client,messages,**policy)
    assert any(e.get("strategy")=="history_summary" for e in events)
    answer=events[-1]["content"]
    assert "HARBOR_17" in answer and "/tmp/harbor-17" in answer and "pending" in answer.lower(),answer
    assert messages==original
    messages += [events[-1]["message"],{"role":"user","content":"Repeat the codename and deployment path only."}]
    resumed=run(client,messages,**policy)
    assert not any(e["type"]=="context_compacted" for e in resumed)
    assert "HARBOR_17" in resumed[-1]["content"] and "/tmp/harbor-17" in resumed[-1]["content"]
    report["summary_recovery"]={"seconds":time.monotonic()-start,"answer":answer,"resume_answer":resumed[-1]["content"],"full_history_preserved":True,"strategies":[e.get("strategy") for e in events if e["type"]=="context_compacted"]}
    (root/"report.json").write_text(json.dumps(report,indent=2))
    print("History summary recovery and requirement retention passed",flush=True)

if __name__=="__main__": main()
