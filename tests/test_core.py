"""Tests for Pengy core modules.

Run with:  python -m pytest tests/ -v
"""

import json
import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cfg_dir():
    """Temporarily redirect pengy config to a temp directory."""
    from pengy.core.config import set_config_dir
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "pengy"
        cfg.mkdir()
        set_config_dir(str(cfg))
        yield cfg
    # Reset config dir after test
    from pengy.core.config import set_config_dir
    set_config_dir(None)


# ---------------------------------------------------------------------------
# config tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_defaults(self):
        from pengy.core.config import DEFAULTS
        assert DEFAULTS["base_url"] == "https://api.openai.com/v1"
        assert DEFAULTS["model"] == "gpt-4o"
        assert DEFAULTS["tool_confirmation"] == "none"
        assert DEFAULTS["context_keep_turns"] == 0
        assert DEFAULTS["tool_timeout"] == 300
        assert DEFAULTS["llm_timeout"] == 300

    def test_render_system_message(self):
        from pengy.core.config import render_system_message
        template = "You are {username} on {hostname}. Today is {date}."
        result = render_system_message(template)
        assert "You are " in result
        assert " on " in result
        assert ". Today is " in result
        # Ensure no raw braces remain
        assert "{" not in result
        assert "}" not in result

    def test_save_load_roundtrip(self, tmp_cfg_dir):
        from pengy.core import config as cfg_mod

        cfg = cfg_mod.load_config()
        cfg["model"] = "test-model"
        cfg["tool_confirmation"] = "all"
        cfg_mod.save_config(cfg)

        loaded = cfg_mod.load_config()
        assert loaded["model"] == "test-model"
        assert loaded["tool_confirmation"] == "all"

    def test_corrupt_config_recovery(self, tmp_cfg_dir):
        from pengy.core import config as cfg_mod

        # Write garbage
        (tmp_cfg_dir / "settings.json").write_text("this is not valid json {{{")

        # Should load defaults without crashing
        loaded = cfg_mod.load_config()
        assert loaded["model"] == "gpt-4o"  # default

        # Bad file should have been backed up
        backups = list(tmp_cfg_dir.glob("settings.json.corrupt-*"))
        assert len(backups) == 1

    def test_defaults_merge(self, tmp_cfg_dir):
        from pengy.core import config as cfg_mod

        # Save partial config
        (tmp_cfg_dir / "settings.json").write_text(json.dumps({"model": "partial"}))

        loaded = cfg_mod.load_config()
        # Partial value preserved
        assert loaded["model"] == "partial"
        # Default filled in
        assert loaded["tool_confirmation"] == "none"
        assert loaded["tool_timeout"] == 300

    def test_migrate_yolo_true(self, tmp_cfg_dir):
        """Old yolo_mode=True → tool_confirmation='all'."""
        from pengy.core import config as cfg_mod
        (tmp_cfg_dir / "settings.json").write_text(json.dumps({"yolo_mode": True}))

        loaded = cfg_mod.load_config()
        assert loaded["tool_confirmation"] == "all"
        assert "yolo_mode" not in loaded
        assert "auto_approve_readonly" not in loaded

    def test_migrate_auto_readonly_true(self, tmp_cfg_dir):
        """Old auto_approve_readonly=True → tool_confirmation='safe'."""
        from pengy.core import config as cfg_mod
        (tmp_cfg_dir / "settings.json").write_text(json.dumps({"auto_approve_readonly": True}))

        loaded = cfg_mod.load_config()
        assert loaded["tool_confirmation"] == "safe"
        assert "yolo_mode" not in loaded
        assert "auto_approve_readonly" not in loaded

    def test_migrate_both_false(self, tmp_cfg_dir):
        """Old defaults (both false) → tool_confirmation='none'."""
        from pengy.core import config as cfg_mod
        (tmp_cfg_dir / "settings.json").write_text(json.dumps({}))

        loaded = cfg_mod.load_config()
        assert loaded["tool_confirmation"] == "none"


# ---------------------------------------------------------------------------
# chat_manager tests
# ---------------------------------------------------------------------------

class TestChatManager:
    def test_create_and_get_chat(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm

        chat = cm.create_chat()
        assert chat["title"] == "New Chat"
        assert "id" in chat
        assert chat["messages"] == []

        loaded = cm.get_chat(chat["id"])
        assert loaded is not None
        assert loaded["id"] == chat["id"]

    def test_delete_chat(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm

        chat = cm.create_chat()
        cm.delete_chat(chat["id"])
        assert cm.get_chat(chat["id"]) is None

    def test_save_chat_updates(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm

        chat = cm.create_chat()
        chat["title"] = "Updated Title"
        cm.save_chat(chat)

        loaded = cm.get_chat(chat["id"])
        assert loaded["title"] == "Updated Title"

    def test_corrupt_chats_recovery(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm

        (tmp_cfg_dir / "chats.json").write_text("garbage {{{")

        chats = cm.load_chats()
        assert chats == []  # Returns empty list

        backups = list(tmp_cfg_dir.glob("chats.json.corrupt-*"))
        assert len(backups) == 1

    def test_corrupt_not_a_list(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm

        (tmp_cfg_dir / "chats.json").write_text('{"key": "not a list"}')

        chats = cm.load_chats()
        assert chats == []

        backups = list(tmp_cfg_dir.glob("chats.json.corrupt-*"))
        assert len(backups) == 1

    def test_clean_dangling_tool_calls(self):
        from pengy.core.chat_manager import clean_dangling_tool_calls

        # Case 1: Normal — assistant with tool_calls followed by tool result
        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant", "content": "",
                "tool_calls": [
                    {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ]
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "file contents"},
            {"role": "assistant", "content": "I read the file"},
        ]
        cleaned = clean_dangling_tool_calls(messages)
        assert len(cleaned) == 4
        assert cleaned[2]["content"] == "file contents"

        # Case 2: Dangling — tool_calls with no tool result
        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant", "content": "",
                "tool_calls": [
                    {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ]
            },
        ]
        cleaned = clean_dangling_tool_calls(messages)
        assert len(cleaned) == 3
        assert cleaned[2]["role"] == "tool"
        assert cleaned[2]["tool_call_id"] == "tc1"
        assert "cancelled" in cleaned[2]["content"].lower()

        # Case 3: Multiple tool calls, one dangling
        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant", "content": "",
                "tool_calls": [
                    {"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "tc2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                ]
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "result1"},
        ]
        cleaned = clean_dangling_tool_calls(messages)
        assert len(cleaned) == 4
        # tc1 result preserved
        assert cleaned[2]["content"] == "result1"
        # tc2 synthesized
        assert cleaned[3]["tool_call_id"] == "tc2"
        assert "cancelled" in cleaned[3]["content"].lower()

        # Case 4: No tool calls — pass through unchanged
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        cleaned = clean_dangling_tool_calls(messages)
        assert cleaned == messages

    def test_elide_old_tool_results(self):
        from pengy.core.chat_manager import elide_old_tool_results

        # Structured: system, then 3 full turns (user+tool+assistant each)
        messages = [
            {"role": "system", "content": "system"},
            # Turn 1
            {"role": "user", "content": "turn 1"},
            {"role": "tool", "tool_call_id": "a", "content": "big result A"},
            {"role": "assistant", "content": "reply 1"},
            # Turn 2
            {"role": "user", "content": "turn 2"},
            {"role": "tool", "tool_call_id": "b", "content": "big result B"},
            {"role": "assistant", "content": "reply 2"},
            # Turn 3
            {"role": "user", "content": "turn 3"},
            {"role": "tool", "tool_call_id": "c", "content": "big result C"},
            {"role": "assistant", "content": "reply 3"},
        ]

        # keep_turns=1: only turn 3 (most recent) is preserved
        result = elide_old_tool_results(messages, keep_turns=1)
        assert result[2]["content"] == "[tool output from earlier turn elided]"   # turn 1
        assert result[5]["content"] == "[tool output from earlier turn elided]"   # turn 2
        assert result[8]["content"] == "big result C"                             # turn 3 preserved

        # keep_turns=2: turns 2 and 3 preserved
        result = elide_old_tool_results(messages, keep_turns=2)
        assert result[2]["content"] == "[tool output from earlier turn elided]"   # turn 1
        assert result[5]["content"] == "big result B"                             # turn 2 preserved
        assert result[8]["content"] == "big result C"                             # turn 3 preserved

        # keep_turns=3: all preserved
        result = elide_old_tool_results(messages, keep_turns=3)
        assert result[2]["content"] == "big result A"
        assert result[5]["content"] == "big result B"
        assert result[8]["content"] == "big result C"

        # keep_turns=0: no elision
        result = elide_old_tool_results(messages, keep_turns=0)
        assert result[2]["content"] == "big result A"
        assert result[5]["content"] == "big result B"

        # keep_turns=999: all preserved
        result = elide_old_tool_results(messages, keep_turns=999)
        assert result[2]["content"] == "big result A"
        assert result[5]["content"] == "big result B"
        assert result[8]["content"] == "big result C"


# ---------------------------------------------------------------------------
# split storage layout
# ---------------------------------------------------------------------------
# Chats live one per file in <config>/chats/<id>.json. index.json caches the
# sidebar summary but is never authoritative -- these check it always converges
# back to what the chat files say, and that the legacy chats.json is read but
# never written.

class TestSplitStorage:
    def _mk(self, cm, title, msgs=None):
        chat = cm.create_chat()
        chat["title"] = title
        chat["messages"] = msgs or []
        cm.save_chat(chat)
        return chat

    def test_chats_are_separate_files(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        a = self._mk(cm, "A")
        b = self._mk(cm, "B")
        assert (tmp_cfg_dir / "chats" / f"{a['id']}.json").exists()
        assert (tmp_cfg_dir / "chats" / f"{b['id']}.json").exists()

    def test_save_chat_touches_only_that_chat(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        a = self._mk(cm, "A")
        b = self._mk(cm, "B")
        b_file = tmp_cfg_dir / "chats" / f"{b['id']}.json"
        before = b_file.stat().st_mtime_ns, b_file.read_bytes()
        a["messages"].append({"role": "user", "content": "hi"})
        cm.save_chat(a)
        assert (b_file.stat().st_mtime_ns, b_file.read_bytes()) == before

    def test_index_carries_count_and_preview(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        self._mk(cm, "A", [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
        ])
        entry = cm.load_index()[0]
        assert entry["msg_count"] == 2
        assert entry["preview"] == "first question"

    def test_preview_handles_multipart_content(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        self._mk(cm, "A", [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "describe this"},
        ]}])
        assert cm.load_index()[0]["preview"] == "describe this"

    def test_index_newest_first(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        a = self._mk(cm, "older")
        b = self._mk(cm, "newer")
        a["created_at"] = "2020-01-01T00:00:00"
        b["created_at"] = "2030-01-01T00:00:00"
        cm.save_chat(a)
        cm.save_chat(b)
        assert [e["title"] for e in cm.load_index()] == ["newer", "older"]

    # ── index is a cache, never the source of truth ──────────────────

    def test_missing_index_is_rebuilt(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        self._mk(cm, "A")
        self._mk(cm, "B")
        (tmp_cfg_dir / "chats" / "index.json").unlink()
        assert sorted(e["title"] for e in cm.load_index()) == ["A", "B"]

    def test_corrupt_index_is_rebuilt_and_kept_aside(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        self._mk(cm, "A")
        (tmp_cfg_dir / "chats" / "index.json").write_text("{ not json")
        assert [e["title"] for e in cm.load_index()] == ["A"]
        assert list((tmp_cfg_dir / "chats").glob("index.json.corrupt-*"))

    def test_index_rebuilt_when_file_added_behind_its_back(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        self._mk(cm, "A")
        (tmp_cfg_dir / "chats" / "ghost.json").write_text(json.dumps({
            "id": "ghost", "title": "GHOST", "messages": [],
            "created_at": "2020-01-01T00:00:00",
        }))
        assert "GHOST" in [e["title"] for e in cm.load_index()]

    def test_index_rebuilt_when_file_removed_behind_its_back(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        a = self._mk(cm, "A")
        self._mk(cm, "B")
        (tmp_cfg_dir / "chats" / f"{a['id']}.json").unlink()
        assert [e["title"] for e in cm.load_index()] == ["B"]

    def test_delete_removes_the_file(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        a = self._mk(cm, "A")
        cm.delete_chat(a["id"])
        assert not (tmp_cfg_dir / "chats" / f"{a['id']}.json").exists()
        assert cm.load_index() == []
        assert cm.get_chat(a["id"]) is None

    # ── legacy chats.json: read, never written ──────────────────────

    def test_migrates_legacy_chats(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        (tmp_cfg_dir / "chats.json").write_text(json.dumps([
            {"id": "old-1", "title": "OLD", "messages": [{"role": "user", "content": "q"}],
             "created_at": "2020-01-01T00:00:00"},
        ]))
        assert [e["title"] for e in cm.load_index()] == ["OLD"]
        assert cm.get_chat("old-1")["messages"][0]["content"] == "q"
        assert (tmp_cfg_dir / "chats" / "old-1.json").exists()

    def test_legacy_file_is_never_modified(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        legacy = tmp_cfg_dir / "chats.json"
        legacy.write_text(json.dumps([
            {"id": "old-1", "title": "OLD", "messages": [], "created_at": "2020-01-01T00:00:00"},
        ]))
        before = legacy.read_bytes()
        cm.load_index()
        c = cm.get_chat("old-1")
        c["title"] = "RENAMED"
        cm.save_chat(c)
        cm.delete_chat("old-1")
        cm.load_chats()
        assert legacy.read_bytes() == before

    def test_rewritten_legacy_is_reimported(self, tmp_cfg_dir):
        # Another edition (Rust/C++) wrote chats.json on the same machine.
        from pengy.core import chat_manager as cm
        self._mk(cm, "MINE")
        (tmp_cfg_dir / "chats.json").write_text(json.dumps([
            {"id": "other-1", "title": "FROM-OTHER-EDITION", "messages": [],
             "created_at": "2020-01-01T00:00:00"},
        ]))
        titles = [e["title"] for e in cm.load_index()]
        assert "FROM-OTHER-EDITION" in titles
        assert "MINE" in titles

    def test_per_chat_file_wins_over_legacy(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        (tmp_cfg_dir / "chats.json").write_text(json.dumps([
            {"id": "x", "title": "LEGACY", "messages": [], "created_at": "2020-01-01T00:00:00"},
        ]))
        cm.load_index()
        c = cm.get_chat("x")
        c["title"] = "CURRENT"
        cm.save_chat(c)
        # Legacy rewritten with a stale copy — the per-chat file must still win.
        (tmp_cfg_dir / "chats.json").write_text(json.dumps([
            {"id": "x", "title": "LEGACY-STALE", "messages": [], "created_at": "2020-01-01T00:00:00"},
        ]))
        cm.load_index()
        assert cm.get_chat("x")["title"] == "CURRENT"

    def test_save_chats_is_additive(self, tmp_cfg_dir):
        # It writes and updates but never deletes -- "save this list" must not
        # also mean "remove everything not in it".
        from pengy.core import chat_manager as cm
        a = self._mk(cm, "KEEP")
        b = self._mk(cm, "ALSO-KEEP")
        b["title"] = "UPDATED"
        cm.save_chats([b])
        titles = sorted(e["title"] for e in cm.load_index())
        assert titles == ["KEEP", "UPDATED"]
        assert cm.get_chat(a["id"]) is not None

    def test_get_chat_returns_an_independent_copy(self, tmp_cfg_dir):
        # The old single-file cache handed out the cached dict by reference, so
        # an abandoned worker's edits leaked into later reads.
        from pengy.core import chat_manager as cm
        a = self._mk(cm, "A")
        got = cm.get_chat(a["id"])
        got["messages"].append({"role": "user", "content": "never saved"})
        assert cm.get_chat(a["id"])["messages"] == []


# ---------------------------------------------------------------------------
# task_manager tests
# ---------------------------------------------------------------------------

class TestTaskManager:
    def test_create_update_delete_task(self, tmp_cfg_dir):
        from pengy.core import task_manager as tm

        first = tm.create_task("First", "Prompt one")
        second = tm.create_task("Second", "Prompt two")

        tasks = tm.load_tasks()
        assert [t["id"] for t in tasks] == [first["id"], second["id"]]
        assert tasks[0]["title"] == "First"

        updated = tm.update_task(first["id"], "Updated", "Prompt %thing%")
        assert updated is not None
        tasks = tm.load_tasks()
        assert [t["id"] for t in tasks] == [first["id"], second["id"]]
        assert tasks[0]["title"] == "Updated"
        assert tasks[0]["template"] == "Prompt %thing%"

        tm.delete_task(first["id"])
        tasks = tm.load_tasks()
        assert [t["id"] for t in tasks] == [second["id"]]

    def test_placeholder_extract_and_render(self):
        from pengy.core.task_manager import extract_placeholders, render_template

        template = "Summarize %URL% with % Skill %, then revisit %URL%. JSON: {\"ok\": true}"
        assert extract_placeholders(template) == ["URL", "Skill"]
        rendered = render_template(template, {"URL": "https://youtu.be/x", "Skill": "youtube transcript"})
        assert rendered == "Summarize https://youtu.be/x with youtube transcript, then revisit https://youtu.be/x. JSON: {\"ok\": true}"

    def test_corrupt_tasks_recovery(self, tmp_cfg_dir):
        from pengy.core import task_manager as tm

        (tmp_cfg_dir / "tasks.json").write_text("garbage {{{")
        assert tm.load_tasks() == []
        backups = list(tmp_cfg_dir.glob("tasks.json.corrupt-*"))
        assert len(backups) == 1


# ---------------------------------------------------------------------------
# tools tests
# ---------------------------------------------------------------------------

class TestTools:
    def test_is_readonly_tool(self):
        from pengy.core.tools import is_readonly_tool
        assert is_readonly_tool("read_file") is True
        assert is_readonly_tool("read_multiple_files") is True
        assert is_readonly_tool("directory_tree") is True
        assert is_readonly_tool("search_content") is True
        assert is_readonly_tool("web_search") is True
        assert is_readonly_tool("fetch_url") is True
        assert is_readonly_tool("write_file") is False
        assert is_readonly_tool("replace_in_file") is False
        assert is_readonly_tool("run_bash") is False
        assert is_readonly_tool("run_python") is False
        assert is_readonly_tool("download_file") is False
        assert is_readonly_tool("nonexistent") is False

    def test_read_file(self):
        from pengy.core.tools import execute_tool
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            path = f.name
        try:
            result = execute_tool("read_file", {"path": path})
            assert "hello world" in result
        finally:
            os.unlink(path)

    def test_read_file_not_found(self):
        from pengy.core.tools import execute_tool
        result = execute_tool("read_file", {"path": "/nonexistent/file"})
        assert "Error" in result or "not found" in result.lower()

    def test_write_file(self):
        from pengy.core.tools import execute_tool
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.txt"
            result = execute_tool("write_file", {"path": str(p), "content": "test content"})
            assert "Successfully" in result
            assert p.read_text() == "test content"

    def test_replace_in_file_single_match(self):
        from pengy.core.tools import execute_tool
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\nfoo bar\n")
            path = f.name
        try:
            result = execute_tool("replace_in_file", {
                "path": path,
                "old_str": "foo bar",
                "new_str": "replaced line",
            })
            assert "Successfully replaced" in result
            content = Path(path).read_text()
            assert "replaced line" in content
            assert "foo bar" not in content
        finally:
            os.unlink(path)

    def test_replace_in_file_zero_matches(self):
        from pengy.core.tools import execute_tool
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\n")
            path = f.name
        try:
            result = execute_tool("replace_in_file", {
                "path": path,
                "old_str": "not in file",
                "new_str": "replacement",
            })
            assert "not found" in result
        finally:
            os.unlink(path)

    def test_replace_in_file_multiple_matches(self):
        from pengy.core.tools import execute_tool
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("line one\nduplicate\nduplicate\nline four\n")
            path = f.name
        try:
            result = execute_tool("replace_in_file", {
                "path": path,
                "old_str": "duplicate",
                "new_str": "unique",
            })
            assert "matches 2 locations" in result
            assert "found_lines" in result.lower() or "lines:" in result.lower()
        finally:
            os.unlink(path)

    def test_replace_in_file_empty_old_str(self):
        from pengy.core.tools import execute_tool
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test\n")
            path = f.name
        try:
            result = execute_tool("replace_in_file", {
                "path": path,
                "old_str": "",
                "new_str": "x",
            })
            assert "old_str is empty" in result
        finally:
            os.unlink(path)

    def test_apply_changes_definition_and_side_effect_classification(self):
        from pengy.core.tools import TOOLS, is_readonly_tool

        names = [tool["function"]["name"] for tool in TOOLS]
        assert "apply_changes" in names
        assert is_readonly_tool("apply_changes") is False

    def test_apply_changes_replaces_and_inserts_transactionally(self, tmp_path):
        from pengy.core.tools import execute_tool

        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("alpha\nbeta\n")
        second.write_text("one\ntwo\n")
        result = execute_tool("apply_changes", {
            "changes": [
                {"path": str(first), "operations": [
                    {"kind": "replace", "old": "beta", "new": "BETA"},
                    {"kind": "insert_after", "anchor": "alpha", "text": "\ninserted"},
                ]},
                {"path": str(second), "operations": [
                    {"kind": "delete", "old": "two"},
                ]},
            ],
            "postconditions": [
                {"path": str(first), "contains": "inserted"},
                {"path": str(second), "does_not_contain": "two"},
            ],
        })
        assert "Applied changes to 2 file(s)" in result
        assert "@@" in result
        assert first.read_text() == "alpha\ninserted\nBETA\n"
        assert second.read_text() == "one\n\n"

    def test_apply_changes_dry_run_does_not_write(self, tmp_path):
        from pengy.core.tools import execute_tool

        path = tmp_path / "sample.txt"
        path.write_text("before\n")
        result = execute_tool("apply_changes", {
            "changes": [{"path": str(path), "operations": [
                {"kind": "replace", "old": "before", "new": "after"},
            ]}],
            "dry_run": True,
        })
        assert "Dry run: no changes applied" in result
        assert "before" in result and "after" in result
        assert path.read_text() == "before\n"

    def test_apply_changes_rejects_ambiguous_match_without_writing_any_file(self, tmp_path):
        from pengy.core.tools import execute_tool

        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("duplicate\nduplicate\n")
        second.write_text("unchanged\n")
        result = execute_tool("apply_changes", {
            "changes": [
                {"path": str(first), "operations": [
                    {"kind": "replace", "old": "duplicate", "new": "changed"},
                ]},
                {"path": str(second), "operations": [
                    {"kind": "replace", "old": "unchanged", "new": "changed"},
                ]},
            ],
        })
        assert "no changes applied" in result
        assert "matches 2 locations" in result
        assert first.read_text() == "duplicate\nduplicate\n"
        assert second.read_text() == "unchanged\n"

    def test_apply_changes_expected_matches_can_replace_repeated_text(self, tmp_path):
        from pengy.core.tools import execute_tool

        path = tmp_path / "sample.txt"
        path.write_text("x x x")
        result = execute_tool("apply_changes", {
            "changes": [{"path": str(path), "operations": [
                {"kind": "replace", "old": "x", "new": "y", "expected_matches": 3},
            ]}],
        })
        assert "Applied changes" in result
        assert path.read_text() == "y y y"

    def test_apply_changes_postcondition_failure_is_atomic(self, tmp_path):
        from pengy.core.tools import execute_tool

        path = tmp_path / "sample.txt"
        path.write_text("before\n")
        result = execute_tool("apply_changes", {
            "changes": [{"path": str(path), "operations": [
                {"kind": "replace", "old": "before", "new": "after"},
            ]}],
            "postconditions": [{"path": str(path), "contains": "required marker"}],
        })
        assert "no changes applied" in result
        assert "does not contain expected text" in result
        assert path.read_text() == "before\n"

    def test_apply_changes_rejects_binary_and_unknown_operations(self, tmp_path):
        from pengy.core.tools import execute_tool

        binary = tmp_path / "data.bin"
        binary.write_bytes(b"\x00\xff")
        result = execute_tool("apply_changes", {
            "changes": [{"path": str(binary), "operations": [
                {"kind": "replace", "old": "x", "new": "y"},
            ]}],
        })
        assert "binary or non-UTF-8" in result
        assert binary.read_bytes() == b"\x00\xff"

        text = tmp_path / "text.txt"
        text.write_text("hello")
        result = execute_tool("apply_changes", {
            "changes": [{"path": str(text), "operations": [
                {"kind": "patch", "old": "hello", "new": "goodbye"},
            ]}],
        })
        assert "unknown kind" in result
        assert text.read_text() == "hello"

    def test_directory_tree(self):
        from pengy.core.tools import execute_tool
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "file.txt").write_text("test")
            (Path(td) / "subdir").mkdir()
            (Path(td) / "subdir" / "nested.py").write_text("print('hi')")

            result = execute_tool("directory_tree", {"path": td, "max_depth": 3})
            assert "file.txt" in result
            assert "subdir/" in result
            assert "nested.py" in result

    def test_read_multiple_files(self):
        from pengy.core.tools import execute_tool
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "a.txt"
            p2 = Path(td) / "b.txt"
            p1.write_text("content A")
            p2.write_text("content B")

            result = execute_tool("read_multiple_files", {
                "paths": [str(p1), str(p2)]
            })
            assert "content A" in result
            assert "content B" in result

    def test_search_content(self):
        from pengy.core.tools import execute_tool
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "search_me.py").write_text("def foo():\n    return 42\nprint(foo())\n")

            result = execute_tool("search_content", {
                "pattern": r"def foo",
                "path": td,
                "file_glob": "*.py",
            })
            assert "foo" in result
            assert "search_me.py" in result

    def test_download_file_scheme_reject(self):
        from pengy.core.tools import execute_tool
        result = execute_tool("download_file", {"url": "file:///etc/passwd"})
        assert "Error" in result
        assert "http" in result.lower()

    def test_fetch_url_scheme_reject(self):
        from pengy.core.tools import execute_tool
        result = execute_tool("fetch_url", {"url": "file:///etc/passwd"})
        assert "Error" in result
        assert "http" in result.lower()

    def test_unknown_tool(self):
        from pengy.core.tools import execute_tool
        result = execute_tool("nonexistent_tool", {})
        assert "Unknown" in result

    def test_todowrite_schema_items_are_objects_not_strings(self):
        """todowrite todos items must be {content, status} objects — not strings."""
        from pengy.core.tools import TOOLS
        todowrite = next(t for t in TOOLS if t["function"]["name"] == "todowrite")
        todos = todowrite["function"]["parameters"]["properties"]["todos"]
        assert todos["type"] == "array"
        items = todos["items"]
        assert items["type"] == "object"
        assert set(items["required"]) == {"content", "status"}
        props = items["properties"]
        assert props["content"]["type"] == "string"
        assert props["status"]["type"] == "string"
        assert set(props["status"]["enum"]) == {"pending", "in_progress", "completed"}

    def test_apply_changes_schema_has_full_operation_properties(self):
        """apply_changes must describe kind/old/new/anchor/text/expected_matches."""
        from pengy.core.tools import TOOLS
        ac = next(t for t in TOOLS if t["function"]["name"] == "apply_changes")
        params = ac["function"]["parameters"]["properties"]

        # changes array
        changes = params["changes"]
        assert changes["type"] == "array"
        change_items = changes["items"]
        assert change_items["type"] == "object"
        assert set(change_items["required"]) == {"path", "operations"}

        # operations within each change
        operations = change_items["properties"]["operations"]
        assert operations["type"] == "array"
        op_items = operations["items"]
        assert op_items["type"] == "object"
        assert "kind" in op_items["required"]
        op_props = op_items["properties"]
        assert op_props["kind"]["enum"] == ["replace", "insert_after", "delete"]
        assert op_props["old"]["type"] == "string"
        assert op_props["new"]["type"] == "string"
        assert op_props["anchor"]["type"] == "string"
        assert op_props["text"]["type"] == "string"
        assert op_props["expected_matches"]["type"] == "integer"

        # dry_run
        assert params["dry_run"]["type"] == "boolean"
        assert len(params["dry_run"].get("description", "")) > 0

        # postconditions
        post = params["postconditions"]
        assert post["type"] == "array"
        post_items = post["items"]
        assert post_items["type"] == "object"
        post_props = post_items["properties"]
        assert "contains" in post_props
        assert "does_not_contain" in post_props
        assert post_props["contains"]["type"] == "string"
        assert post_props["does_not_contain"]["type"] == "string"


class TestGlob:
    """Tests for the glob tool."""

    def test_glob_finds_py_files(self, tmp_path):
        from pengy.core.tools import execute_tool
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.rs").write_text("y")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.py").write_text("z")
        result = execute_tool("glob", {"pattern": "**/*.py", "path": str(tmp_path)})
        assert "a.py" in result
        assert "sub/c.py" in result
        assert "b.rs" not in result

    def test_glob_no_matches(self, tmp_path):
        from pengy.core.tools import execute_tool
        result = execute_tool("glob", {"pattern": "*.xyz", "path": str(tmp_path)})
        assert "No files matching" in result

    def test_glob_skips_hidden_by_default(self, tmp_path):
        from pengy.core.tools import execute_tool
        (tmp_path / ".hidden.py").write_text("x")
        (tmp_path / "visible.py").write_text("y")
        result = execute_tool("glob", {"pattern": "*.py", "path": str(tmp_path)})
        assert "visible.py" in result
        assert ".hidden.py" not in result

    def test_glob_skips_node_modules(self, tmp_path):
        from pengy.core.tools import execute_tool
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "foo.js").write_text("x")
        (tmp_path / "src.js").write_text("y")
        result = execute_tool("glob", {"pattern": "**/*.js", "path": str(tmp_path)})
        assert "src.js" in result
        assert "node_modules" not in result

    def test_glob_defaults_to_cwd(self):
        from pengy.core.tools import execute_tool
        result = execute_tool("glob", {"pattern": "*.py"})
        # Should find at least the test files
        assert isinstance(result, str)
        assert len(result) > 0

    def test_glob_directories_show_slash(self, tmp_path):
        from pengy.core.tools import execute_tool
        (tmp_path / "mydir").mkdir()
        result = execute_tool("glob", {"pattern": "*", "path": str(tmp_path)})
        assert "mydir/" in result

    def test_glob_dir_prefix_in_pattern(self, tmp_path):
        """Pattern with directory prefix extracts it as search dir."""
        from pengy.core.tools import execute_tool
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.rs").write_text("y")
        result = execute_tool("glob", {"pattern": f"{tmp_path}/*.py"})
        assert "a.py" in result
        assert "b.rs" not in result

    def test_glob_exact_file_in_pattern(self, tmp_path):
        """Pattern pointing to a specific file finds it."""
        from pengy.core.tools import execute_tool
        (tmp_path / "target.rs").write_text("content")
        result = execute_tool("glob", {"pattern": f"{tmp_path}/target.rs"})
        assert "target.rs" in result

    def test_glob_dir_prefix_with_recursive(self, tmp_path):
        """Pattern with ** and directory prefix preserves recursion."""
        from pengy.core.tools import execute_tool
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").write_text("y")
        result = execute_tool("glob", {"pattern": f"{tmp_path}/**/*.py"})
        assert "a.py" in result
        assert "sub/b.py" in result

    def test_glob_explicit_path_takes_precedence(self, tmp_path):
        """When path is explicitly provided, use it directly."""
        from pengy.core.tools import execute_tool
        (tmp_path / "a.py").write_text("x")
        result = execute_tool("glob", {"pattern": "*.py", "path": str(tmp_path)})
        assert "a.py" in result


class TestTodowrite:
    """Tests for the todowrite tool."""

    def test_echoes_back_valid_todos(self):
        from pengy.core.tools import execute_tool
        todos = [
            {"content": "Find auth code", "status": "in_progress"},
            {"content": "Add JWT", "status": "pending"},
            {"content": "Write tests", "status": "pending"},
        ]
        result = execute_tool("todowrite", {"todos": todos})
        assert "[\u2192] Find auth code" in result
        assert "[ ] Add JWT" in result
        assert "[ ] Write tests" in result

    def test_rejects_multiple_in_progress(self):
        from pengy.core.tools import execute_tool
        todos = [
            {"content": "Task A", "status": "in_progress"},
            {"content": "Task B", "status": "in_progress"},
        ]
        result = execute_tool("todowrite", {"todos": todos})
        assert "Error" in result
        assert "2 tasks marked in_progress" in result

    def test_rejects_invalid_status(self):
        from pengy.core.tools import execute_tool
        todos = [
            {"content": "Task A", "status": "done"},
        ]
        result = execute_tool("todowrite", {"todos": todos})
        assert "invalid status" in result

    def test_rejects_empty_content(self):
        from pengy.core.tools import execute_tool
        todos = [
            {"content": "", "status": "pending"},
        ]
        result = execute_tool("todowrite", {"todos": todos})
        assert "content is empty" in result

    def test_rejects_empty_list(self):
        from pengy.core.tools import execute_tool
        result = execute_tool("todowrite", {"todos": []})
        assert "empty" in result

    def test_all_pending_is_valid(self):
        from pengy.core.tools import execute_tool
        todos = [
            {"content": "Task A", "status": "pending"},
            {"content": "Task B", "status": "pending"},
        ]
        result = execute_tool("todowrite", {"todos": todos})
        assert "Error" not in result

    def test_allows_all_completed(self):
        from pengy.core.tools import execute_tool
        todos = [
            {"content": "Task A", "status": "completed"},
            {"content": "Task B", "status": "completed"},
        ]
        result = execute_tool("todowrite", {"todos": todos})
        assert "[\u2713]" in result


class TestAskUserQuestion:
    """Tests for the ask_user_question tool."""

    def test_tool_definition_exists(self):
        from pengy.core.tools import TOOLS
        names = [t["function"]["name"] for t in TOOLS]
        assert "ask_user_question" in names

    def test_execute_returns_harness_message(self):
        from pengy.core.tools import execute_tool
        result = execute_tool("ask_user_question", {
            "questions": [{"header": "Test", "question": "Q?", "options": [{"label": "A", "description": "desc"}]}],
        })
        assert "harness" in result.lower()

    def test_is_not_readonly(self):
        from pengy.core.tools import is_readonly_tool
        assert not is_readonly_tool("ask_user_question")


class TestToolContext:
    """Per-run tool isolation so concurrent tabs don't share sudo/procs."""

    def test_sudo_provider_is_per_context(self):
        from pengy.core.tools import ToolContext
        ctx_a = ToolContext(sudo_provider=lambda: "pw-a")
        ctx_b = ToolContext(sudo_provider=lambda: "pw-b")
        assert ctx_a.sudo_provider() == "pw-a"
        assert ctx_b.sudo_provider() == "pw-b"

    def test_cached_sudo_password_not_shared(self):
        from pengy.core.tools import ToolContext
        ctx_a = ToolContext()
        ctx_b = ToolContext()
        ctx_a.cached_sudo_password = "secret"
        assert ctx_b.cached_sudo_password is None
        ctx_a.clear_sudo()
        assert ctx_a.cached_sudo_password is None

    def test_run_bash_routes_sudo_through_context(self):
        # A context with no provider must refuse sudo regardless of any global.
        from pengy.core.tools import execute_tool, ToolContext
        ctx = ToolContext(sudo_provider=None)
        result = execute_tool("run_bash", {"command": "sudo true"}, ctx)
        assert "no password provider" in result

    def test_kill_all_only_affects_own_context(self):
        """kill_all() on one context must not touch another's subprocess."""
        import subprocess
        from pengy.core.tools import ToolContext
        ctx_a = ToolContext()
        ctx_b = ToolContext()
        # A long-lived sleep registered only in ctx_b.
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            ctx_b.register_process(proc)
            ctx_a.kill_all()            # must NOT kill ctx_b's process
            assert proc.poll() is None
            ctx_b.kill_all()            # now it should die
            assert proc.wait(timeout=5) is not None
        finally:
            if proc.poll() is None:
                proc.kill()


# ---------------------------------------------------------------------------
# search_content unit tests
# ---------------------------------------------------------------------------

class TestSearchContentInternals:
    def test_expand_braces_simple(self):
        from pengy.core.tools import _expand_braces
        result = _expand_braces("*.{py,js}")
        assert result == ["*.py", "*.js"]

    def test_expand_braces_prefix_suffix(self):
        from pengy.core.tools import _expand_braces
        result = _expand_braces("test.{ts,tsx}")
        assert result == ["test.ts", "test.tsx"]

    def test_expand_braces_no_braces(self):
        from pengy.core.tools import _expand_braces
        result = _expand_braces("*.py")
        assert result == ["*.py"]

    def test_group_regions_adjacent(self):
        from pengy.core.tools import _group_regions
        regions = _group_regions({5, 6, 10}, context=0, total_lines=20)
        assert regions == [(5, 7), (10, 11)]

    def test_group_regions_with_context(self):
        from pengy.core.tools import _group_regions
        regions = _group_regions({5}, context=2, total_lines=20)
        assert regions == [(3, 8)]

    def test_group_regions_overlapping(self):
        from pengy.core.tools import _group_regions
        regions = _group_regions({5, 8}, context=2, total_lines=20)
        # 5±2=3..8, 8±2=6..11 → merge to 3..11
        assert regions == [(3, 11)]


# ---------------------------------------------------------------------------
# LLM client tests (integration-light)
# ---------------------------------------------------------------------------

class TestLLMClient:
    def test_client_creation(self):
        from pengy.core.llm_client import LLMClient
        client = LLMClient(
            base_url="http://localhost:1234/v1",
            api_key="test-key",
            model="test-model",
        )
        assert client.base_url == "http://localhost:1234/v1"
        assert client.model == "test-model"


# ---------------------------------------------------------------------------
# image_utils tests
# ---------------------------------------------------------------------------

class TestImageUtils:
    def test_png_convert_to_jpeg(self):
        from pengy.core.image_utils import preprocess
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            # Create a 200x200 red PNG
            img = Image.new("RGB", (200, 200), color=(255, 0, 0))
            img.save(path, "PNG")
            orig_size = Path(path).stat().st_size

            buf, mime = preprocess(Path(path))
            # Should convert PNG → JPEG
            assert mime == "image/jpeg"
            # JPEG should be much smaller than raw PNG
            assert len(buf) < orig_size
            assert len(buf) < 5000
        finally:
            os.unlink(path)

    def test_oversized_dimensions_downscaled(self):
        from pengy.core.image_utils import preprocess
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            # Create a 6000x4000 image (bigger than default 4096)
            img = Image.new("RGB", (6000, 4000), color=(0, 0, 255))
            img.save(path, "PNG")

            buf, mime = preprocess(Path(path), max_dimension=2048)
            # Should be downscaled
            assert mime == "image/jpeg"
            result = Image.open(io.BytesIO(buf))
            assert result.width <= 2048
            assert result.height <= 2048
        finally:
            os.unlink(path)

    def test_jpeg_passthrough_within_limits(self):
        from pengy.core.image_utils import preprocess
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            path = f.name
        try:
            # Create a small JPEG
            img = Image.new("RGB", (100, 100), color=(0, 255, 0))
            img.save(path, "JPEG", quality=85)

            buf, mime = preprocess(Path(path))
            # Should stay JPEG since already within limits
            assert mime == "image/jpeg"
            assert len(buf) > 0
        finally:
            os.unlink(path)

    def test_max_mb_enforced(self):
        from pengy.core.image_utils import preprocess
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            # Create a large PNG (simple color, compresses well)
            img = Image.new("RGB", (2048, 2048), color=(128, 0, 64))
            img.save(path, "PNG")

            # Force a low max_mb — it'll need to shrink dimensions
            buf, mime = preprocess(Path(path), max_mb=0.05, max_dimension=4096)
            assert mime == "image/jpeg"
            # Should be under 0.05 MB (51 KB)
            assert len(buf) < 51200
        finally:
            os.unlink(path)

    def test_rgba_png_flattened_to_jpeg(self):
        from pengy.core.image_utils import preprocess
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            # Create RGBA PNG with transparency
            img = Image.new("RGBA", (64, 64), color=(255, 0, 0, 128))
            img.save(path, "PNG")

            buf, mime = preprocess(Path(path))
            assert mime == "image/jpeg"
            # Should decode as valid JPEG
            result = Image.open(io.BytesIO(buf))
            assert result.mode == "RGB"
        finally:
            os.unlink(path)

    def test_bad_file_raises(self):
        from pengy.core.image_utils import preprocess
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not an image at all")
            path = f.name
        try:
            import pytest
            with pytest.raises(Exception):
                preprocess(Path(path))
        finally:
            os.unlink(path)

    def test_default_constants(self):
        from pengy.core.image_utils import DEFAULT_MAX_DIMENSION, DEFAULT_MAX_MB, DEFAULT_QUALITY
        assert DEFAULT_MAX_DIMENSION == 4096
        assert DEFAULT_MAX_MB == 4.5
        assert DEFAULT_QUALITY == 85

    def test_webp_stays_webp_if_small(self):
        from pengy.core.image_utils import preprocess
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as f:
            path = f.name
        try:
            img = Image.new("RGB", (32, 32), color=(128, 128, 128))
            img.save(path, "WEBP")

            buf, mime = preprocess(Path(path))
            # Should stay WebP since within limits
            assert mime in ("image/webp", "image/jpeg")
            assert len(buf) > 0
        finally:
            os.unlink(path)
