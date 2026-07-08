"""Tests for Pengy core modules.

Run with:  python -m pytest tests/ -v
"""

import json
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
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "pengy"
        cfg.mkdir()
        yield cfg


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
        assert DEFAULTS["tool_timeout"] == 60

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
        cfg_mod.CONFIG_DIR = tmp_cfg_dir
        cfg_mod.CONFIG_FILE = tmp_cfg_dir / "settings.json"

        cfg = cfg_mod.load_config()
        cfg["model"] = "test-model"
        cfg["tool_confirmation"] = "all"
        cfg_mod.save_config(cfg)

        loaded = cfg_mod.load_config()
        assert loaded["model"] == "test-model"
        assert loaded["tool_confirmation"] == "all"

    def test_corrupt_config_recovery(self, tmp_cfg_dir):
        from pengy.core import config as cfg_mod
        cfg_mod.CONFIG_DIR = tmp_cfg_dir
        cfg_mod.CONFIG_FILE = tmp_cfg_dir / "settings.json"

        # Write garbage
        cfg_mod.CONFIG_FILE.write_text("this is not valid json {{{")

        # Should load defaults without crashing
        loaded = cfg_mod.load_config()
        assert loaded["model"] == "gpt-4o"  # default

        # Bad file should have been backed up
        backups = list(tmp_cfg_dir.glob("settings.json.corrupt-*"))
        assert len(backups) == 1

    def test_defaults_merge(self, tmp_cfg_dir):
        from pengy.core import config as cfg_mod
        cfg_mod.CONFIG_DIR = tmp_cfg_dir
        cfg_mod.CONFIG_FILE = tmp_cfg_dir / "settings.json"

        # Save partial config
        cfg_mod.CONFIG_FILE.write_text(json.dumps({"model": "partial"}))

        loaded = cfg_mod.load_config()
        # Partial value preserved
        assert loaded["model"] == "partial"
        # Default filled in
        assert loaded["tool_confirmation"] == "none"
        assert loaded["tool_timeout"] == 60

    def test_migrate_yolo_true(self, tmp_cfg_dir):
        """Old yolo_mode=True → tool_confirmation='all'."""
        from pengy.core import config as cfg_mod
        cfg_mod.CONFIG_DIR = tmp_cfg_dir
        cfg_mod.CONFIG_FILE = tmp_cfg_dir / "settings.json"
        cfg_mod.CONFIG_FILE.write_text(json.dumps({"yolo_mode": True}))

        loaded = cfg_mod.load_config()
        assert loaded["tool_confirmation"] == "all"
        assert "yolo_mode" not in loaded
        assert "auto_approve_readonly" not in loaded

    def test_migrate_auto_readonly_true(self, tmp_cfg_dir):
        """Old auto_approve_readonly=True → tool_confirmation='safe'."""
        from pengy.core import config as cfg_mod
        cfg_mod.CONFIG_DIR = tmp_cfg_dir
        cfg_mod.CONFIG_FILE = tmp_cfg_dir / "settings.json"
        cfg_mod.CONFIG_FILE.write_text(json.dumps({"auto_approve_readonly": True}))

        loaded = cfg_mod.load_config()
        assert loaded["tool_confirmation"] == "safe"
        assert "yolo_mode" not in loaded
        assert "auto_approve_readonly" not in loaded

    def test_migrate_both_false(self, tmp_cfg_dir):
        """Old defaults (both false) → tool_confirmation='none'."""
        from pengy.core import config as cfg_mod
        cfg_mod.CONFIG_DIR = tmp_cfg_dir
        cfg_mod.CONFIG_FILE = tmp_cfg_dir / "settings.json"
        cfg_mod.CONFIG_FILE.write_text(json.dumps({}))

        loaded = cfg_mod.load_config()
        assert loaded["tool_confirmation"] == "none"


# ---------------------------------------------------------------------------
# chat_manager tests
# ---------------------------------------------------------------------------

class TestChatManager:
    def test_create_and_get_chat(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        cm.CHATS_DIR = tmp_cfg_dir
        cm.CHATS_FILE = tmp_cfg_dir / "chats.json"

        chat = cm.create_chat()
        assert chat["title"] == "New Chat"
        assert "id" in chat
        assert chat["messages"] == []

        loaded = cm.get_chat(chat["id"])
        assert loaded is not None
        assert loaded["id"] == chat["id"]

    def test_delete_chat(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        cm.CHATS_DIR = tmp_cfg_dir
        cm.CHATS_FILE = tmp_cfg_dir / "chats.json"

        chat = cm.create_chat()
        cm.delete_chat(chat["id"])
        assert cm.get_chat(chat["id"]) is None

    def test_save_chat_updates(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        cm.CHATS_DIR = tmp_cfg_dir
        cm.CHATS_FILE = tmp_cfg_dir / "chats.json"

        chat = cm.create_chat()
        chat["title"] = "Updated Title"
        cm.save_chat(chat)

        loaded = cm.get_chat(chat["id"])
        assert loaded["title"] == "Updated Title"

    def test_corrupt_chats_recovery(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        cm.CHATS_DIR = tmp_cfg_dir
        cm.CHATS_FILE = tmp_cfg_dir / "chats.json"

        cm.CHATS_FILE.write_text("garbage {{{")

        chats = cm.load_chats()
        assert chats == []  # Returns empty list

        backups = list(tmp_cfg_dir.glob("chats.json.corrupt-*"))
        assert len(backups) == 1

    def test_corrupt_not_a_list(self, tmp_cfg_dir):
        from pengy.core import chat_manager as cm
        cm.CHATS_DIR = tmp_cfg_dir
        cm.CHATS_FILE = tmp_cfg_dir / "chats.json"

        cm.CHATS_FILE.write_text('{"key": "not a list"}')

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
# task_manager tests
# ---------------------------------------------------------------------------

class TestTaskManager:
    def test_create_update_delete_task(self, tmp_cfg_dir):
        from pengy.core import task_manager as tm
        tm.TASKS_DIR = tmp_cfg_dir
        tm.TASKS_FILE = tmp_cfg_dir / "tasks.json"

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
        tm.TASKS_DIR = tmp_cfg_dir
        tm.TASKS_FILE = tmp_cfg_dir / "tasks.json"

        tm.TASKS_FILE.write_text("garbage {{{")
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
