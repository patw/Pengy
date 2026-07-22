#!/usr/bin/env python3
"""
Pengy Performance Benchmark — Synthetic before/after comparison.

Measures the performance impact of the T1 + T2 optimizations by running
both the OLD (un-optimized) and NEW (optimized) code paths side by side.

This is a synthetic benchmark — it isolates each optimization and measures
it in isolation, not through the full app pipeline. Real-world impact will
vary depending on chat length, tool usage patterns, etc.

Run: python3 benchmark_perf.py
"""

import json
import re
import time
import sys
from pathlib import Path
from collections import defaultdict

# ─── 1. Chat cache: load_chats with vs without cache ────────────────

def bench_chat_cache():
    """Measures the chats.json parse cache (the v1.4.1 fix)."""
    import tempfile, os

    # Create a synthetic chats.json with 50 chats, 100 messages each
    chats = []
    for i in range(50):
        msgs = []
        for j in range(100):
            msgs.append({"role": "user" if j % 2 == 0 else "assistant",
                        "content": f"Message {j} in chat {i} " + "x" * 200})
        chats.append({"id": f"chat-{i}", "title": f"Chat {i}", "messages": msgs,
                      "created_at": "2026-01-01T00:00:00"})

    tmpdir = tempfile.mkdtemp()
    chats_path = os.path.join(tmpdir, "chats.json")
    with open(chats_path, "w") as f:
        json.dump(chats, f)

    # OLD: re-parse on every call (simulating 10 reads — startup + get_chat + save_chat cycle)
    def load_old():
        with open(chats_path, "r") as f:
            return json.load(f)

    # NEW: cached with (mtime, size) key
    cache_state = {"key": None, "data": None}
    def load_new():
        st = os.stat(chats_path)
        key = (st.st_mtime_ns, st.st_size)
        if key == cache_state["key"] and cache_state["data"] is not None:
            return cache_state["data"]
        with open(chats_path, "r") as f:
            cache_state["data"] = json.load(f)
        cache_state["key"] = key
        return cache_state["data"]

    N = 50  # simulate 50 reads (startup + multiple get_chat/save_chat cycles)
    t0 = time.perf_counter()
    for _ in range(N):
        load_old()
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        load_new()
    new_ms = (time.perf_counter() - t0) * 1000

    os.unlink(chats_path)
    os.rmdir(tmpdir)
    return old_ms, new_ms, N, f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── 2. Render: batch vs per-message (the v1.4.1 fix) ───────────────

def bench_batch_render():
    """Measures building HTML string for 200 messages: per-message vs batch."""
    msgs = []
    for i in range(200):
        if i % 3 == 0:
            msgs.append({"role": "user", "content": f"User message {i}\nwith some text"})
        elif i % 3 == 1:
            msgs.append({"role": "assistant", "content": f"## Assistant {i}\n\n```python\nprint('hello')\n```\n\nSome **bold** text."})
        else:
            msgs.append({"role": "tool_block", "tool_call_id": f"tc-{i}", "name": "read_file",
                        "args": {"path": "/tmp/test"}, "result": "file contents", "declined": False})

    def render_msg(msg, idx):
        role = msg["role"]
        if role == "user":
            return f"<p>User</p><p>{msg['content']}</p>"
        if role == "assistant":
            return f"<p>Assistant</p><div>{msg['content']}</div>"
        if role == "tool_block":
            return f"<div class='tool'>Tool: {msg['name']}</div>"
        return ""

    # OLD: render per message (simulating setHtml per message)
    t0 = time.perf_counter()
    html_parts = []
    for msg in msgs:
        html_parts.append(render_msg(msg, 0))
        full = f"<html><body>{''.join(html_parts)}</body></html>"
        # In the real app, this calls setHtml(full) — we simulate the string build cost
    old_ms = (time.perf_counter() - t0) * 1000

    # NEW: batch append, single render
    t0 = time.perf_counter()
    html_parts = []
    for idx, msg in enumerate(msgs):
        html_parts.append(render_msg(msg, idx))
    full = f"<html><body>{''.join(html_parts)}</body></html>"
    new_ms = (time.perf_counter() - t0) * 1000

    return old_ms, new_ms, len(msgs), f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── 3. renderMessage O(n) linear scan vs index pass (T1.2) ─────────

def bench_rendermessage_linear_scan():
    """Measures the O(n) deep comparison scan in renderMessage (T1.2 fix)."""
    # Simulate 100 assistant messages with reasoning_content
    msgs = []
    for i in range(100):
        msgs.append({"role": "assistant", "content": f"Response {i}",
                    "reasoning_content": f"Thinking about {i}..."})

    # OLD: linear scan with deep comparison to find index
    def render_old():
        parts = []
        for msg in msgs:
            if "reasoning_content" in msg:
                # O(n) scan: find index by comparing all messages
                idx = -1
                for i, m in enumerate(msgs):
                    if m == msg:  # deep dict comparison
                        idx = i
                        break
                parts.append(f"<div>Reasoning {idx}</div>")
            parts.append(f"<div>{msg['content']}</div>")
        return "".join(parts)

    # NEW: index passed directly from loop
    def render_new():
        parts = []
        for idx, msg in enumerate(msgs):
            if "reasoning_content" in msg:
                parts.append(f"<div>Reasoning {idx}</div>")
            parts.append(f"<div>{msg['content']}</div>")
        return "".join(parts)

    N = 20
    t0 = time.perf_counter()
    for _ in range(N):
        render_old()
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        render_new()
    new_ms = (time.perf_counter() - t0) * 1000

    return old_ms, new_ms, len(msgs), f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── 4. Tool definitions: rebuild vs cache (T1.5) ───────────────────

def bench_tool_definitions():
    """Measures building 11 tool definitions struct + serialize vs cached."""
    # Simulate the tool definitions structure
    def build_tools_old():
        tools = []
        for name, desc, params, required in [
            ("read_file", "Read file", {"path": {"type": "string", "description": "path"}}, ["path"]),
            ("write_file", "Write file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
            ("replace_in_file", "Replace", {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, ["path", "old_str", "new_str"]),
            ("run_bash", "Run bash", {"command": {"type": "string"}}, ["command"]),
            ("web_search", "Search", {"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"]),
            ("download_file", "Download", {"url": {"type": "string"}, "filename": {"type": "string"}}, ["url"]),
            ("fetch_url", "Fetch", {"url": {"type": "string"}}, ["url"]),
            ("run_python", "Run Python", {"code": {"type": "string"}}, ["code"]),
            ("directory_tree", "Tree", {"path": {"type": "string"}, "max_depth": {"type": "integer"}, "show_hidden": {"type": "boolean"}}, ["path"]),
            ("read_multiple_files", "Read multiple", {"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"]),
            ("search_content", "Search content", {"pattern": {"type": "string"}, "path": {"type": "string"}, "file_glob": {"type": "string"}, "context_lines": {"type": "integer"}, "max_results": {"type": "integer"}}, ["pattern", "path"]),
        ]:
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": params,
                        "required": required,
                    }
                }
            })
        return json.dumps(tools)

    # NEW: cached JSON
    cached = [None]
    def build_tools_new():
        if cached[0] is None:
            cached[0] = build_tools_old()
        return cached[0]

    N = 1000
    t0 = time.perf_counter()
    for _ in range(N):
        build_tools_old()
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        build_tools_new()
    new_ms = (time.perf_counter() - t0) * 1000

    return old_ms, new_ms, N, f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── 5. Glob regex: per-file compile vs cache (T2.1) ────────────────

def bench_glob_regex():
    """Measures compiling a glob regex per file vs caching it."""
    filenames = [f"file_{i}.py" for i in range(500)] + [f"other_{i}.js" for i in range(500)]
    glob_pattern = "*.py"

    # OLD: compile regex per file
    def glob_old():
        results = []
        for name in filenames:
            pat = glob_pattern.replace('.', r'\.').replace('*', '.*').replace('?', '.')
            rx = re.compile(f"^{pat}$")
            if rx.match(name):
                results.append(name)
        return results

    # NEW: cache compiled regex
    _cache = {}
    def glob_new():
        results = []
        for name in filenames:
            if glob_pattern not in _cache:
                pat = glob_pattern.replace('.', r'\.').replace('*', '.*').replace('?', '.')
                _cache[glob_pattern] = re.compile(f"^{pat}$")
            if _cache[glob_pattern].match(name):
                results.append(name)
        return results

    N = 20
    t0 = time.perf_counter()
    for _ in range(N):
        glob_old()
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        glob_new()
    new_ms = (time.perf_counter() - t0) * 1000

    return old_ms, new_ms, len(filenames), f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── 6. Pygments formatter: per-block vs cached (T2.5) ──────────────

def bench_pygments_formatter():
    """Measures creating HtmlFormatter per code block vs caching it."""
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_by_name, TextLexer
        from pygments.formatters import HtmlFormatter
    except ImportError:
        return None, None, 0, "skipped (pygments not available)"

    code_blocks = []
    for i in range(50):
        lang = ["python", "rust", "cpp", "javascript", "bash"][i % 5]
        code = f"def func_{i}():\n    return {i}\n"
        code_blocks.append((lang, code))

    # OLD: create formatter per block
    def highlight_old():
        results = []
        for lang, code in code_blocks:
            try:
                lexer = get_lexer_by_name(lang, stripnl=False)
            except Exception:
                lexer = TextLexer()
            formatter = HtmlFormatter(style="friendly", noclasses=True, nobackground=True)
            results.append(highlight(code, lexer, formatter))
        return results

    # NEW: cache formatter + lexers
    _formatter = HtmlFormatter(style="friendly", noclasses=True, nobackground=True)
    _lexer_cache = {}
    def highlight_new():
        results = []
        for lang, code in code_blocks:
            if lang not in _lexer_cache:
                try:
                    _lexer_cache[lang] = get_lexer_by_name(lang, stripnl=False)
                except Exception:
                    _lexer_cache[lang] = TextLexer()
            results.append(highlight(code, _lexer_cache[lang], _formatter))
        return results

    N = 20
    t0 = time.perf_counter()
    for _ in range(N):
        highlight_old()
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        highlight_new()
    new_ms = (time.perf_counter() - t0) * 1000

    return old_ms, new_ms, len(code_blocks), f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── 7. CSS string: rebuild vs cache (T1.3) ──────────────────────────

def bench_css_cache():
    """Measures rebuilding a CSS string with ~20 .format() calls vs caching."""
    template = """
body {{ font-family: "{fixed}"; font-size: {body_pt}pt; background-color: {bg}; color: {fg}; }}
a {{ color: {link}; }}
pre {{ white-space: pre-wrap; }}
table {{ border: 1px solid {border}; }}
th, td {{ border: 1px solid {border}; padding: 4px; }}
.role-user {{ color: {user_label}; font-weight: bold; font-size: {label_pt}pt; }}
.role-assistant {{ color: {assistant_label}; font-weight: bold; font-size: {label_pt}pt; }}
.tool-card {{ border: 1px solid {border_soft}; padding: 4px; background-color: {tool_bg}; }}
.code-pre {{ background-color: {code_bg}; color: {code_fg}; padding: 10px; }}
.muted {{ color: {muted}; }}
.reasoning-card {{ border: 1px solid {reasoning_border}; background-color: {reasoning_bg}; }}
"""
    theme = {
        "fixed": "monospace", "body_pt": "10", "bg": "#1e1e2e", "fg": "#cdd6f4",
        "link": "#89b4fa", "border": "#45475a", "user_label": "#89b4fa",
        "label_pt": "9", "assistant_label": "#a6e3a1", "border_soft": "#313244",
        "tool_bg": "#181825", "code_bg": "#181825", "code_fg": "#cdd6f4",
        "muted": "#6c7086", "reasoning_border": "#45475a", "reasoning_bg": "#181825",
    }

    # OLD: rebuild every time
    def css_old():
        return template.format(**theme)

    # NEW: cache
    cached = [None]
    def css_new():
        if cached[0] is None:
            cached[0] = template.format(**theme)
        return cached[0]

    N = 1000
    t0 = time.perf_counter()
    for _ in range(N):
        css_old()
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        css_new()
    new_ms = (time.perf_counter() - t0) * 1000

    return old_ms, new_ms, N, f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── 8. loadChatList: rebuild sidebar vs skip (T1.1) ────────────────

def bench_loadchatlist():
    """Measures the cost of building sidebar item data for 50 chats (×2 for double)."""
    chats = [{"id": f"chat-{i}", "title": f"Chat Title {i} with a longer name"} for i in range(50)]

    # OLD: create widget data twice (T1.4 Python pattern)
    def old_load():
        items = []
        for chat in chats:
            # First call (for sizeHint) — discarded
            w1 = {"label": chat["title"], "buttons": 2}
            # Second call (for setItemWidget)
            w2 = {"label": chat["title"], "buttons": 2}
            items.append((w1, w2))
        return items

    # NEW: create once
    def new_load():
        items = []
        for chat in chats:
            w = {"label": chat["title"], "buttons": 2}
            items.append(w)
        return items

    N = 1000
    t0 = time.perf_counter()
    for _ in range(N):
        old_load()
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for _ in range(N):
        new_load()
    new_ms = (time.perf_counter() - t0) * 1000

    return old_ms, new_ms, len(chats), f"{old_ms/new_ms:.1f}x" if new_ms > 0 else "∞"


# ─── Main ────────────────────────────────────────────────────────────

def main():
    print("=" * 78)
    print("  Pengy Performance Benchmark — Synthetic before/after comparison")
    print("  Measuring isolated impact of each T1 + T2 optimization")
    print("=" * 78)
    print()

    benchmarks = [
        ("Chat cache (v1.4.1)",        bench_chat_cache,           "load_chats ×{n} (50 chats, 100 msgs each)"),
        ("Batch render (v1.4.1)",      bench_batch_render,         "build HTML for {n} messages"),
        ("renderMessage index (T1.2)", bench_rendermessage_linear_scan, "render {n} msgs with reasoning (×20)"),
        ("Tool defs cache (T1.5)",     bench_tool_definitions,     "build+serialize 11 tools ×{n}"),
        ("CSS cache (T1.3)",           bench_css_cache,            "build CSS string ×{n}"),
        ("Sidebar widget (T1.4)",      bench_loadchatlist,         "build 50 chat rows ×{n}"),
        ("Glob regex cache (T2.1)",    bench_glob_regex,           "glob match {n} filenames (×20)"),
        ("Pygments cache (T2.5)",      bench_pygments_formatter,   "highlight {n} code blocks (×20)"),
    ]

    print(f"{'Benchmark':<32} {'Items':<12} {'Before':>10} {'After':>10} {'Speedup':>10}")
    print("-" * 78)

    total_old = 0
    total_new = 0

    for name, fn, desc in benchmarks:
        try:
            old, new, n, speedup = fn()
            if old is None:
                print(f"{name:<32} {'—':<12} {'—':>10} {'—':>10} {speedup:>10}")
                continue
            total_old += old
            total_new += new
            print(f"{name:<32} {n:<12} {old:>8.1f}ms {new:>8.1f}ms {speedup:>10}")
        except Exception as e:
            print(f"{name:<32} ERROR: {e}")

    print("-" * 78)
    if total_new > 0:
        print(f"{'TOTAL':<32} {'':<12} {total_old:>8.1f}ms {total_new:>8.1f}ms {total_old/total_new:>9.1f}x")
    print()
    print("Note: These are synthetic micro-benchmarks isolating each optimization.")
    print("Real-world impact depends on chat length, tool usage, and render frequency.")
    print("The chat cache and batch render (v1.4.1) are the dominant wins for daily use;")
    print("T1+T2 fixes eliminate secondary hotspots that compound with long chats.")


if __name__ == "__main__":
    main()
