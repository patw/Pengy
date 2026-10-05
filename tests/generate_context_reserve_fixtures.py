"""Generate cross-edition final-attempt reservation cases from Python policy."""
import copy
import json
from pathlib import Path
from pengy.core.context_recovery import Recovery

root = Path(__file__).resolve().parents[1]

def tool_round(call_id, body):
    return [{"role":"assistant","content":"","tool_calls":[{"id":call_id,"type":"function","function":{"name":"read_file","arguments":"{\"path\":\"synthetic\"}"}}]},
            {"role":"tool","tool_call_id":call_id,"content":body}]

def history(old=True):
    messages = [{"role":"system","content":"Do not change production."},
                {"role":"user","content":"Requirement: project HARBOR_17 path /tmp/harbor-17; audit pending. " + "background " * 150}]
    messages += tool_round("older", "A" * 4000)
    if old:
        messages += [{"role":"assistant","content":"Read complete; audit still pending."}]
        for n in range(5):
            messages += [{"role":"user","content":f"Completed task {n}."}, {"role":"assistant","content":"Done."}]
        messages += [{"role":"user","content":"Current task: report codename, path, and audit status."}]
    messages += tool_round("newest", "B" * 4000)
    return messages

cases = []
for name in ["reserve_summary", "no_old_turns", "keep_all_turns", "summary_call_budget_exhausted", "summary_chunk_budget_exhausted", "reasoning_at_final", "failed_summary", "nonreducing_summary"]:
    messages = history(old=name != "no_old_turns")
    keep = 99 if name == "keep_all_turns" else 3
    initial_attempts = 3 if name in ("summary_call_budget_exhausted", "summary_chunk_budget_exhausted", "reasoning_at_final", "failed_summary", "nonreducing_summary") else 0
    summary_calls = 16 if name == "summary_call_budget_exhausted" else 15 if name == "summary_chunk_budget_exhausted" else 0
    if name == "summary_chunk_budget_exhausted": messages[1]["content"] += "x" * 65000
    if name == "reasoning_at_final": messages[4]["reasoning_content"] = "optional thought " * 300
    recovery = Recovery(messages, "test", "m", keep_turns=keep)
    recovery.attempts = initial_attempts
    recovery.summary_calls = summary_calls
    summary = "Project HARBOR_17, path /tmp/harbor-17; audit pending. Old read complete."
    failure = "failure" if name == "failed_summary" else "nonreducing" if name == "nonreducing_summary" else ""
    events = []
    for _ in range(4 - initial_attempts):
        def summarize(source):
            if failure == "failure": raise RuntimeError("incomplete summary")
            return source if failure == "nonreducing" else summary
        try:
            event = recovery.reduce(messages, summarize)
        except RuntimeError:
            break
        if event is None: break
        events.append(event["strategy"])
    cases.append({"name":name,"messages":messages,"keep_turns":keep,"initial_attempts":initial_attempts,
                  "summary_calls":summary_calls,"summary":summary,"failure":failure,
                  "strategies":events,"expected_attempts":recovery.attempts,"expected":recovery.apply(messages)})
for repo in ["Pengy", "PengyR", "PengyCPP"]:
    path = root.parent / repo / "tests/fixtures/context_recovery_reserve.json"
    path.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
print("Shared reservation fixtures written")
