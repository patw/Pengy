"""Opt-in, bounded per-route window discovery; cache format shared with native editions."""
from __future__ import annotations
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
from pengy.core.config import get_config_dir


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def window_fields(value):
    for field in ('context_window', 'context_length'):
        if n := number(value.get(field)): return {'tokens': n, 'input_only': False}
    if n := number((value.get('meta') or {}).get('n_ctx')): return {'tokens': n, 'input_only': False}
    if n := number(value.get('max_input_tokens')): return {'tokens': n, 'input_only': True}
    return None


def model_window(body, model):
    return next((window_fields(v) for v in body.get('data', []) if v.get('id') == model), None)


def cache_path(base, model):
    endpoint = base.strip().rstrip('/')
    key = json.dumps([endpoint, model], ensure_ascii=False, separators=(',', ':')).encode()
    return get_config_dir()/'context_windows'/(hashlib.sha256(key).hexdigest()+'.json')


def remember(base, model, window, source):
    path = cache_path(base, model)
    entry = {'v': 1, 'endpoint': base.strip().rstrip('/'), 'model': model, 'window': window,
             'source': source, 'expires': int(time.time())+(86400 if window else 300)}
    name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix='.window-')
        with os.fdopen(fd, 'w') as f:
            json.dump(entry, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    except OSError:
        if name:
            try: os.unlink(name)
            except OSError: pass


def error_window(message):
    match = re.search(r'(?:maximum|max) context (?:length|window|size)(?: is| of|:|=)?\s*([0-9][0-9,]*)', message, re.I)
    if not match:
        match = re.search(r'prompt \([0-9,]+ tokens\) leaves no room to answer in the context \(([0-9,]+)\)', message, re.I)
    n = number(int(match[1].replace(',', ''))) if match else None
    return {'tokens': n, 'input_only': False} if n else None


def remember_error(base, model, message):
    w = error_window(message)
    if w: remember(base, model, w, 'context_error')
    return w


def resolve(base, model, key, global_window=0, windows=None, auto=False, enabled=True, modes=None):
    windows = windows or {}
    mode = (modes or {}).get(model)
    if mode == 'off': return None
    if mode == 'auto': global_window, windows, auto = 0, {}, True
    if model in windows:
        n = number(windows[model]); return {'tokens': n, 'input_only': False} if n else None
    if number(global_window): return {'tokens': global_window, 'input_only': False}
    if not auto or not enabled: return None
    endpoint = base.strip().rstrip('/')
    try:
        entry = json.loads(cache_path(base, model).read_text())
        if (entry.get('v') == 1 and entry.get('endpoint') == endpoint and entry.get('model') == model
                and entry.get('expires', 0) > time.time()
                and (entry.get('window') is None or number(entry['window'].get('tokens')))):
            return entry.get('window')
    except (OSError, ValueError, TypeError, AttributeError): pass
    sibling = endpoint[:-3] if endpoint.endswith('/v1') else endpoint
    deadline = time.monotonic()+2
    single = False
    for source, url in [('models', endpoint+'/models'), ('props', sibling+'/props'), ('health', sibling+'/health')]:
        remaining = deadline-time.monotonic()
        if remaining <= 0: break
        try:
            request = urllib.request.Request(url, headers={'Authorization': 'Bearer '+key} if key else {})
            with urllib.request.urlopen(request, timeout=min(.7, remaining)) as response:
                chunks = []
                size = 0
                while time.monotonic() < deadline:
                    chunk = response.read1(65536)
                    if not chunk: break
                    chunks.append(chunk); size += len(chunk)
                    if size > 1024*1024: raise ValueError('discovery body too large')
                if time.monotonic() >= deadline: break
                body = json.loads(b''.join(chunks))
            w = None
            if source == 'models':
                data = body.get('data', [])
                single = len(data) == 1 and data[0].get('id') == model
                w = model_window(body, model)
            elif body.get('model', body.get('model_alias')) == model or single:
                n = number((body.get('default_generation_settings') or {}).get('n_ctx')) if source == 'props' else number(body.get('max_context'))
                if n: w = {'tokens': n, 'input_only': False}
            if w:
                remember(base, model, w, source); return w
        except (OSError, ValueError, TypeError, AttributeError): pass
    remember(base, model, None, 'unknown')
    return None
