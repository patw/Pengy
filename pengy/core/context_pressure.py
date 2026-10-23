"""Provider-usage-calibrated text pressure. No model table or tokenizer guesses."""
from __future__ import annotations
import copy
import hashlib
import json
import threading

_SAMPLES = {}
_LOCK = threading.Lock()


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def input_chars(messages, tools):
    def count(value):
        if isinstance(value, str): return len(value)
        if isinstance(value, list):
            counts = [count(v) for v in value]
            return None if None in counts else sum(counts)
        if isinstance(value, dict):
            if any(k in value for k in ('image_url', 'input_audio', 'file')): return None
            counts = [count(v) for v in value.values()]
            return None if None in counts else sum(len(k) for k in value) + sum(counts)
        if value is None: return 0
        return len(json.dumps(value))
    a, b = count(messages), count(tools)
    return None if a is None or b is None else a + b


def budgets(window, percent, output, input_only=False):
    hard = window if input_only else max(0, window-output)
    return min(window*percent//100, hard), hard


class Pressure:
    def __init__(self, window=0):
        self.window, self.sample, self.rearm = window, None, None
        self.view, self.prefix = '', []

    @classmethod
    def load(cls, key, messages, view, window):
        with _LOCK:
            p = copy.deepcopy(_SAMPLES.get(key)) if key else None
        prefix = [identity(m) for m in messages]
        if p and p.view == view and p.window == window and prefix[:len(p.prefix)] == p.prefix:
            return p
        return cls(window)

    def record(self, chars, tokens):
        if isinstance(chars, int) and chars > 0 and isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
            self.sample = chars, tokens

    def estimate(self, chars):
        if self.sample is None or chars is None: return None
        last_chars, tokens = self.sample
        return (chars*tokens + last_chars-1)//last_chars

    def should_compact(self, estimate, threshold, hard):
        return (self.window > 0 and estimate is not None and estimate >= threshold
                and (estimate >= hard or self.rearm is None or estimate >= self.rearm))

    def compacted(self, after, threshold):
        self.rearm = threshold+(self.window+19)//20 if after is not None and after < threshold else None

    def set_window(self, window):
        if self.window != window: self.window, self.rearm = window, None

    def save(self, key, messages, view):
        self.view, self.prefix = view, [identity(m) for m in messages]
        if key:
            with _LOCK:
                if len(_SAMPLES) >= 256 and key not in _SAMPLES: _SAMPLES.clear()
                _SAMPLES[key] = copy.deepcopy(self)
