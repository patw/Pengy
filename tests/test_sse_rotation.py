"""SSE connection expiry must never terminate or replay an agent task."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from pengy.web import app as web


@pytest.fixture
def worker():
    return web.WebWorker({"id": "sse-test", "messages": [], "title": "test"}, {"model": "stub"})


def test_rotation_preserves_worker_and_next_real_event(worker):
    worker._put_event({"type": "tool_result", "content": "already ran"})
    events = list(worker.iter_events(timeout=0))
    assert events == [{"type": "tool_result", "content": "already ran"}, {"type": "stream_rotate"}]
    assert worker.event_count == 1 and not worker._done
    worker._put_event({"type": "final_response", "html": "done"})
    assert list(worker.iter_events(start_index=1, timeout=0)) == [{"type": "final_response", "html": "done"}]


def test_multiple_rotations_do_not_consume_log_ids(worker):
    for _ in range(3):
        assert list(worker.iter_events(timeout=0)) == [{"type": "stream_rotate"}]
    assert worker.event_count == 0 and not worker._cancelled and not worker._done
    worker._put_event({"type": "error", "message": "actual inference failure"})
    assert list(worker.iter_events(timeout=0)) == [{"type": "error", "message": "actual inference failure"}]


def test_sse_rotation_wire_frame_has_no_id_and_reconnect_does_not_skip(worker, monkeypatch):
    monkeypatch.setattr(web, "_workers", {"sse-test": worker})
    iterate = worker.iter_events
    monkeypatch.setattr(worker, "iter_events", lambda start_index=0: iterate(start_index, timeout=0))
    worker._put_event({"type": "tool_result", "content": "finished tool"})
    with web.app.test_client() as client:
        response = client.get('/chat/sse-test/stream')
        wire = response.data.decode()
        assert 'id: 0\n' in wire
        rotation = next(frame for frame in wire.split('\n\n') if 'event: stream_rotate' in frame)
        assert rotation == 'event: stream_rotate\ndata: {}'
        assert 'Stream timeout' not in wire
        assert web._workers['sse-test'] is worker and not worker._done
        worker._put_event({"type": "final_response", "html": "done"})
        resumed = client.get('/chat/sse-test/stream', headers={'Last-Event-ID': '0'}).data.decode()
        assert 'id: 1\n' in resumed and 'final_response' in resumed
        assert 'finished tool' not in resumed


def test_browser_rotation_backoff_and_cursor_contract():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for executing browser reconnect regression')
    template = (Path(__file__).parents[1] / 'pengy/web/templates/chat.html').read_text()
    functions = template[template.index('function closeSSE()'):template.index('function handleEvent(data)')]
    handlers = template[template.index('function handleEvent(data)'):template.index('// ── DOM builders')]
    processing = template[template.index('function setProcessing(val)'):template.index('// ── File Attachments')]
    # Execute actual production functions in a deterministic EventSource/timer
    # environment. No copied reconnect implementation and no real network.
    code = r'''
const vm = require('vm');
const assert = require('assert');
const sources = [], timers = new Map(), saved = new Map(), received = [];
let clock = 0, nextTimer = 0, reloads = 0;
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id,{disabled:false, classList:{toggle(){}}, focus(){}}); return elements.get(id); };
class EventSource {
  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
  constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; sources.push(this); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  close() { this.readyState = 2; }
}
const context = vm.createContext({ EventSource, Math: Object.assign(Object.create(Math), {random: () => 0.5}),
  sessionStorage: {setItem: (k,v) => saved.set(k,v), removeItem: k => saved.delete(k)},
  document: {getElementById: element},
  acquireWakeLock() {}, releaseWakeLock() {}, hideThinking() {}, appendAssistantMessage() {},
  updateCumulativeTokens() {}, appendError() {},
  setTimeout: (fn,delay) => {const id = ++nextTimer; timers.set(id,{fn,delay}); return id;},
  clearTimeout: id => timers.delete(id),
  handleEvent: data => received.push(data), reloadToSync: () => reloads++ });
vm.runInContext("const CHAT_ID = 'chat'; let isProcessing = true; let eventSource = null; let sseCursor = ''; let sseReconnectTimer = null; let sseReconnectAttempt = 0;", context);
vm.runInContext(FUNCTIONS, context);
const exec = code => vm.runInContext(code, context);
const fireTimer = () => { assert.equal(timers.size,1); const [id,t] = [...timers][0]; timers.delete(id); clock += t.delay; t.fn(); return t.delay; };
const message = (source, id, data) => source.onmessage({lastEventId: id, data: JSON.stringify(data)});
exec('openSSE()'); let s = sources.at(-1); s.readyState=1; s.onopen();
message(s,'0',{type:'tool_result',content:'already executed'});
assert.equal(saved.get('pengy_sse_cursor_chat'),'0');
s.listeners.stream_rotate({lastEventId:'0'});
assert.equal(exec('isProcessing'),true); assert.equal(exec('sseCursor'),'0');
exec('openSSE()'); assert.equal(sources.length,1); // pending retry respected
assert.equal(fireTimer(),100);
s = sources.at(-1); assert.equal(s.url,'/chat/chat/stream?after=0');
s.readyState=1; s.onopen();
message(s,'0',{type:'tool_result',content:'duplicate'}); assert.equal(received.length,1);
message(s,'1',{type:'tool_result',content:'next real event'}); assert.equal(received.length,2);
// Older server timeout has a spurious next ID; do not persist it.
message(s,'2',{type:'error',message:'Stream timeout'});
assert.equal(exec('sseCursor'),'1'); assert.equal(saved.get('pengy_sse_cursor_chat'),'1');
fireTimer(); s=sources.at(-1); assert.equal(s.url,'/chat/chat/stream?after=1');
// Consecutive failed opens: expo waits, capped at 30 seconds.
const delays=[];
for(let i=0;i<8;i++) { s.readyState=0; s.onerror(); delays.push(fireTimer()); s=sources.at(-1); }
assert.deepEqual(delays,[1000,2000,4000,8000,16000,30000,30000,30000]);
s.readyState=1; s.onopen(); s.readyState=0; s.onerror(); assert.equal(fireTimer(),1000); s=sources.at(-1);
// Stale callbacks cannot retire a replacement connection.
const old=sources[0]; old.onerror(); assert.equal(timers.size,0); assert.equal(exec('eventSource'),s);
// Stop/terminal state clears pending retry and never resubmits anything.
s.readyState=0; s.onerror(); assert.equal(timers.size,1);
exec('isProcessing = false; closeSSE()'); assert.equal(timers.size,0);
exec('openSSE()'); assert.equal(exec('eventSource'),null); assert.equal(reloads,0);
assert(sources.every(s => s.url.startsWith('/chat/chat/stream')));
// A nonretryable response resynchronizes history once, not the task.
exec('isProcessing = true; openSSE()'); s=sources.at(-1); s.readyState=2; s.onerror();
assert.equal(reloads,1); assert.equal(timers.size,0);
// Execute the real terminal handlers and processing-state cleanup too.
vm.runInContext(PROCESSING + '\n' + HANDLERS, context);
exec('setProcessing(true); openSSE()'); s=sources.at(-1); s.readyState=1; s.onopen();
message(s,'2',{type:'final_response',html:'done'});
assert.equal(exec('isProcessing'),false); assert.equal(exec('eventSource'),null); assert.equal(timers.size,0);
assert.equal(element('messageInput').disabled,false); assert(!saved.has('pengy_sse_cursor_chat'));
exec("sseCursor = ''; setProcessing(true); openSSE()"); s=sources.at(-1); s.readyState=1; s.onopen();
message(s,'0',{type:'error',message:'actual provider failure'});
assert.equal(exec('isProcessing'),false); assert.equal(exec('eventSource'),null); assert.equal(timers.size,0);
console.log('Browser rotation, replay cursor, bounded backoff, stop and stale callbacks PASS');
'''
    code = code.replace('FUNCTIONS', json.dumps(functions)).replace('PROCESSING', json.dumps(processing)).replace('HANDLERS', json.dumps(handlers))
    result = subprocess.run([node, '-e', code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
