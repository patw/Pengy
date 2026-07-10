"""Tests for Pengy Web UI — pure functions, templates, and Flask routes.

Run with:  python -m pytest tests/test_web.py -v
"""

import json
import tempfile
from pathlib import Path

import pytest

from pengy.web.app import (
    _safe_id,
    _render_md,
    _pygments_css,
    _build_messages,
    _group_messages,
    app,
    WebWorker,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dirs():
    """Temporarily redirect pengy config/chats to a temp directory.

    Uses ``set_config_dir`` (the correct API since the config.py refactor)
    instead of monkey-patching module-level constants that no longer exist.
    """
    from pengy.core.config import set_config_dir, get_config_dir
    from pathlib import Path as _Path
    from pengy.core import config as cfg_mod

    with tempfile.TemporaryDirectory(prefix="pengy-webtest-") as cfg_dir:
        cfg_dir = _Path(cfg_dir)
        cfg_dir.mkdir(exist_ok=True)
        set_config_dir(str(cfg_dir))

        # Safety assertion — fail loudly rather than corrupt live data
        resolved = get_config_dir()
        real = _Path.home() / ".config" / "pengy"
        assert resolved != real, (
            f"Config dir not redirected! get_config_dir()={resolved} "
            f"matches real user config {real}. Aborting to protect live data."
        )

        yield cfg_dir, cfg_dir  # both config and chats live in the same dir now

    # Reset config dir after test
    set_config_dir(None)


@pytest.fixture
def client(tmp_dirs):
    """Flask test client with isolated config/chats dirs."""
    app.config["SERVER_NAME"] = "localhost"
    app.config["APPLICATION_ROOT"] = "/"
    app.config["PREFERRED_URL_SCHEME"] = "http"
    return app.test_client()


# ────────────────────────────────────────────────────────────────────
# Pure function tests
# ────────────────────────────────────────────────────────────────────

class TestSafeId:
    def test_alphanumeric_passthrough(self):
        assert _safe_id("abc123") == "tc_abc123"

    def test_special_chars_stripped(self):
        assert _safe_id("call-1_foo:bar") == "tc_call1foobar"

    def test_empty(self):
        assert _safe_id("") == "tc_"

    def test_all_special(self):
        assert _safe_id("!@#$%^&*()") == "tc_"

    def test_uuid(self):
        assert _safe_id("a1b2c3d4-e5f6-7890") == "tc_a1b2c3d4e5f67890"


class TestRenderMd:
    def test_plain_text(self):
        html = _render_md("hello world")
        assert "<p>hello world</p>" in html

    def test_empty(self):
        assert _render_md("") == ""

    def test_code_block(self):
        html = _render_md("```python\nprint('hi')\n```")
        assert "highlight" in html  # codehilite CSS class
        assert "print" in html

    def test_inline_code(self):
        html = _render_md("use `foo()` here")
        assert "<code>" in html

    def test_markdown_headers(self):
        html = _render_md("# Title\n## Subtitle")
        assert "<h1>" in html
        assert "<h2>" in html


class TestPygmentsCSS:
    def test_returns_css_string(self):
        css = _pygments_css()
        assert ".highlight" in css
        assert len(css) > 100


class TestBuildMessages:
    def test_empty_chat_no_system(self):
        chat = {"messages": []}
        config = {"system_message": "", "context_keep_turns": 0}
        result = _build_messages(chat, config)
        assert result == []

    def test_with_system_message(self):
        chat = {"messages": []}
        config = {"system_message": "You are {username}", "context_keep_turns": 0}
        result = _build_messages(chat, config)
        assert len(result) >= 1
        assert result[0]["role"] == "system"
        # Should have interpolated {username}
        assert "{username}" not in result[0]["content"]

    def test_messages_preserved(self):
        chat = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        }
        config = {"system_message": "", "context_keep_turns": 0}
        result = _build_messages(chat, config)
        assert result == chat["messages"]

    def test_dangling_tool_calls_cleaned(self):
        """_build_messages calls clean_dangling_tool_calls before returning."""
        chat = {
            "messages": [
                {"role": "user", "content": "read a file"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
            ]
        }
        config = {"system_message": "", "context_keep_turns": 0}
        result = _build_messages(chat, config)
        # Should have synthesized a cancelled tool result
        assert len(result) == 3
        assert result[2]["role"] == "tool"
        assert "cancelled" in result[2]["content"].lower()


class TestGroupMessages:
    """The _group_messages function converts raw message lists to
    display-ready turn groups for the templates."""

    def test_empty(self):
        assert _group_messages([]) == []

    def test_simple_user_assistant(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        turns = _group_messages(messages)
        assert len(turns) == 2
        assert turns[0] == {"type": "user", "content": "hello"}
        assert turns[1] == {"type": "assistant", "html": _render_md("hi there")}

    def test_tool_use_with_result(self):
        messages = [
            {"role": "user", "content": "read a file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/test.txt"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents here"},
            {"role": "assistant", "content": "I read: file contents here"},
        ]
        turns = _group_messages(messages)
        assert len(turns) == 3
        assert turns[0]["type"] == "user"
        assert turns[1]["type"] == "tool_use"
        assert len(turns[1]["events"]) == 1
        ev = turns[1]["events"][0]
        assert ev["name"] == "read_file"
        assert ev["args"] == {"path": "/tmp/test.txt"}
        assert ev["tool_call_id"] == "call_1"
        assert ev["result"] == "file contents here"
        assert ev["declined"] is False
        assert turns[2]["type"] == "assistant"
        assert "file contents here" in turns[2]["html"]

    def test_tool_declined(self):
        messages = [
            {"role": "user", "content": "delete something"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "run_bash",
                            "arguments": '{"command": "rm -rf /"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "Tool execution was declined by user.",
            },
        ]
        turns = _group_messages(messages)
        ev = turns[1]["events"][0]
        assert ev["declined"] is True
        assert ev["result"] is not None

    def test_tool_cancelled(self):
        messages = [
            {"role": "user", "content": "run something"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "run_bash", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_x",
                "content": "Tool execution was cancelled by user.",
            },
        ]
        turns = _group_messages(messages)
        ev = turns[1]["events"][0]
        assert ev["declined"] is True

    def test_multiple_tool_calls(self):
        messages = [
            {"role": "user", "content": "read two files"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/a.txt"}',
                        },
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/b.txt"}',
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "content A"},
            {"role": "tool", "tool_call_id": "call_b", "content": "content B"},
            {"role": "assistant", "content": "got both"},
        ]
        turns = _group_messages(messages)
        assert len(turns) == 3
        events = turns[1]["events"]
        assert len(events) == 2
        assert events[0]["tool_call_id"] == "call_a"
        assert events[0]["result"] == "content A"
        assert events[1]["tool_call_id"] == "call_b"
        assert events[1]["result"] == "content B"

    def test_assistant_with_content_and_tool_calls(self):
        """Assistant message with both text content and tool_calls."""
        messages = [
            {"role": "user", "content": "read the file"},
            {
                "role": "assistant",
                "content": "Let me read that file for you.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/x.txt"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "hello world"},
            {"role": "assistant", "content": "The file says: hello world"},
        ]
        turns = _group_messages(messages)
        # Should produce: user, assistant (text only), tool_use, assistant
        assert len(turns) == 4
        assert turns[0]["type"] == "user"
        assert turns[1]["type"] == "assistant"
        assert "Let me read" in turns[1]["html"]
        assert turns[2]["type"] == "tool_use"
        assert turns[3]["type"] == "assistant"

    def test_system_messages_skipped(self):
        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        turns = _group_messages(messages)
        assert len(turns) == 2
        assert turns[0]["type"] == "user"
        assert turns[1]["type"] == "assistant"

    def test_tool_role_without_assistant_preceding_is_skipped(self):
        """A stray tool message without preceding tool_calls is skipped."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "orphan", "content": "orphan result"},
            {"role": "assistant", "content": "hello"},
        ]
        turns = _group_messages(messages)
        assert len(turns) == 2
        assert turns[0]["type"] == "user"
        assert turns[1]["type"] == "assistant"

    def test_safe_id_in_turn_events(self):
        messages = [
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1_foo:bar",
                        "type": "function",
                        "function": {"name": "run", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1_foo:bar", "content": "done"},
        ]
        turns = _group_messages(messages)
        ev = turns[1]["events"][0]
        assert ev["safe_id"] == "tc_call1foobar"

    def test_malformed_tool_arguments(self):
        """Tools with unparseable arguments should default to empty dict."""
        messages = [
            {"role": "user", "content": "do thing"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "bad",
                        "type": "function",
                        "function": {
                            "name": "do_thing",
                            "arguments": "not valid json {{{",
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "bad", "content": "ok"},
        ]
        turns = _group_messages(messages)
        ev = turns[1]["events"][0]
        assert ev["args"] == {}


# ────────────────────────────────────────────────────────────────────
# WebWorker tests (no LLM needed)
# ────────────────────────────────────────────────────────────────────

class TestWebWorker:
    def test_creation(self, tmp_dirs):
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        assert w._chat["id"] == "test-id"
        assert not w._done
        assert not w._cancelled

    def test_cancel_sets_flag_and_events(self, tmp_dirs):
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        w.cancel()
        assert w._cancelled is True
        # Events should be set so blocked waits wake up
        assert w._confirm_event.is_set()
        assert w._sudo_event.is_set()

    def test_send_confirmation(self, tmp_dirs):
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        w.send_confirmation(True, "call-1", yolo_turn=False)
        assert w._confirm_event.is_set()
        assert w._confirm_result == {
            "confirmed": True,
            "tool_call_id": "call-1",
            "yolo_turn": False,
        }

    def test_send_confirmation_yolo(self, tmp_dirs):
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        w.send_confirmation(True, "call-x", yolo_turn=True)
        assert w._confirm_result["yolo_turn"] is True

    def test_send_sudo_password(self, tmp_dirs):
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        w.send_sudo_password("hunter2")
        assert w._sudo_event.is_set()
        assert w._sudo_result == "hunter2"

    def test_send_sudo_password_none(self, tmp_dirs):
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        w.send_sudo_password(None)
        assert w._sudo_result is None

    def test_iter_events_empty_queue_done(self, tmp_dirs):
        """If worker is marked done with empty queue, iter_events finishes."""
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        w._done = True
        events = list(w.iter_events(timeout=0.5))
        assert events == []

    def test_iter_events_keepalives(self, tmp_dirs):
        """iter_events yields keepalives when queue is empty and not done."""
        chat = {"id": "test-id", "title": "test", "messages": []}
        config = {"model": "gpt-4o", "tool_confirmation": "none"}
        w = WebWorker(chat, config)
        # Start iterating with a short timeout; queue is empty, not done
        events = list(w.iter_events(timeout=0.1))
        # Should get keepalives until timeout
        keepalives = [e for e in events if e.get("type") == "keepalive"]
        assert len(keepalives) >= 1
        # Should get timeout error
        errors = [e for e in events if e.get("type") == "error"]
        assert len(errors) == 1
        assert "timeout" in errors[0]["message"].lower()


# ────────────────────────────────────────────────────────────────────
# Flask route tests
# ────────────────────────────────────────────────────────────────────

class TestRoutes:
    def test_index_no_chats_creates_one(self, client):
        """Index with no chats should create a chat and redirect."""
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/chat/" in resp.location

    def test_index_with_chats_redirects_to_first(self, tmp_dirs):
        """Index with existing chats redirects to the first (newest) chat."""
        from pengy.core.chat_manager import create_chat

        chat1 = create_chat()
        chat2 = create_chat()  # chat2 is newer, insert(0) so it's first

        with app.test_client() as c:
            resp = c.get("/")
            assert resp.status_code == 302
            # load_chats returns newest first, so chat2 should be first
            assert chat2["id"] in resp.location

    def test_new_chat_creates_and_redirects(self, client):
        resp = client.post("/chat/new")
        assert resp.status_code == 302
        assert "/chat/" in resp.location

    def test_chat_view_existing(self, client):
        from pengy.core.chat_manager import create_chat, save_chat

        chat = create_chat()
        chat["title"] = "Test Chat"
        save_chat(chat)

        resp = client.get(f"/chat/{chat['id']}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Test Chat" in html
        assert "🐧 Pengy" in html

    def test_chat_view_nonexistent(self, client):
        resp = client.get("/chat/nonexistent-id")
        assert resp.status_code == 302  # redirects to index

    def test_send_empty_message(self, client):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        resp = client.post(
            f"/chat/{chat['id']}/send",
            data=json.dumps({"content": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "Empty" in data["error"]

    def test_send_nonexistent_chat(self, client):
        resp = client.post(
            "/chat/nonexistent/send",
            data=json.dumps({"content": "hello"}),
            content_type="application/json",
        )
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert "not found" in data["error"].lower()

    def test_send_starts_worker(self, client):
        from pengy.core.chat_manager import create_chat
        from pengy.web.app import _workers_lock, _workers

        chat = create_chat()
        resp = client.post(
            f"/chat/{chat['id']}/send",
            data=json.dumps({"content": "hello world"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"

        # Worker may have been created and already cleaned up (no API key),
        # but at least the send returned OK and the title was set.
        assert data["title"] is not None
        assert len(data["title"]) > 0

    def test_send_updates_title(self, client):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        assert chat["title"] == "New Chat"

        resp = client.post(
            f"/chat/{chat['id']}/send",
            data=json.dumps({"content": "This is a test message"}),
            content_type="application/json",
        )
        data = json.loads(resp.data)
        assert data["title"] == "This is a test message"

    def test_confirm_no_worker(self, client):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        resp = client.post(
            f"/chat/{chat['id']}/confirm",
            data=json.dumps({"confirmed": True, "tool_call_id": "tc1"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_sudo_no_worker(self, client):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        resp = client.post(
            f"/chat/{chat['id']}/sudo",
            data=json.dumps({"password": "hunter2"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_delete_chat(self, client):
        from pengy.core.chat_manager import create_chat, get_chat

        chat = create_chat()
        resp = client.post(f"/chat/{chat['id']}/delete")
        assert resp.status_code == 302
        # Chat should be gone
        assert get_chat(chat["id"]) is None

    def test_settings_get_renders(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Settings" in html
        assert "Base URL" in html

    def test_settings_post_saves(self, client):
        resp = client.post(
            "/settings",
            data={
                "base_url": "http://custom:1234/v1",
                "model": "custom-model",
                "tool_confirmation": "all",
                "tool_timeout": "120",
                "context_keep_turns": "5",
                "user_agent": "TestBot/1.0",
                "system_message": "Be helpful",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200
        html = resp.data.decode()
        # Should show "saved" alert
        assert "Settings saved" in html

        # Verify config was actually saved
        from pengy.core.config import load_config

        cfg = load_config()
        assert cfg["base_url"] == "http://custom:1234/v1"
        assert cfg["model"] == "custom-model"
        assert cfg["tool_confirmation"] == "all"
        assert cfg["tool_timeout"] == 120
        assert cfg["context_keep_turns"] == 5
        assert cfg["user_agent"] == "TestBot/1.0"

    def test_settings_api_key_not_revealed(self, client):
        """API key field should be empty in rendered HTML even when set."""
        client.post(
            "/settings",
            data={
                "api_key": "sk-secret-key-123",
            },
        )
        resp = client.get("/settings")
        html = resp.data.decode()
        assert "sk-secret-key-123" not in html

    def test_stream_no_worker(self, client):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        resp = client.get(f"/chat/{chat['id']}/stream")
        assert resp.status_code == 200
        # Should get an SSE error event
        data = resp.data.decode()
        assert "No active task" in data


# ────────────────────────────────────────────────────────────────────
# Template / HTML rendering tests
# ────────────────────────────────────────────────────────────────────

class TestTemplateRendering:
    def test_chat_template_renders(self, tmp_dirs):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        chat["title"] = "TplTest"

        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            assert resp.status_code == 200
            html = resp.data.decode()

    def test_settings_template_renders(self, tmp_dirs):
        with app.test_client() as c:
            resp = c.get("/settings")
            assert resp.status_code == 200
            html = resp.data.decode()
            assert "Settings" in html

    def test_mobile_viewport_meta(self, tmp_dirs):
        """The viewport meta tag includes viewport-fit=cover for notched phones."""
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            html = resp.data.decode()
            assert 'viewport-fit=cover' in html

    def test_ios_zoom_prevention(self, tmp_dirs):
        """Message input should have font-size: 16px to prevent iOS zoom on focus."""
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            html = resp.data.decode()
            assert "font-size: 16px" in html

    def test_safe_area_padding(self, tmp_dirs):
        """Input area should use safe-area-inset-bottom for notched phones."""
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            html = resp.data.decode()
            assert "env(safe-area-inset-bottom" in html

    def test_tap_target_size(self, tmp_dirs):
        """Chat list items should have min-height 44px for touch accessibility."""
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            html = resp.data.decode()
            assert "min-height: 44px" in html

    def test_offcanvas_before_app_shell(self, tmp_dirs):
        """The offcanvas must be a direct child of body, NOT nested in app-shell."""
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            html = resp.data.decode()

            offcanvas_pos = html.find('class="offcanvas offcanvas-start"')
            app_shell_pos = html.find('class="app-shell"')
            assert offcanvas_pos < app_shell_pos, (
                f"Offcanvas at {offcanvas_pos} should come BEFORE "
                f"app-shell at {app_shell_pos}"
            )

    def test_chat_links_no_data_bs_dismiss(self, tmp_dirs):
        """Chat list <a> tags must NOT have data-bs-dismiss (it blocks navigation).
        Dismissal is handled by a separate JS delegated click handler instead."""
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            html = resp.data.decode()

            import re
            # Find all chat-list-item <a> tags
            links = re.findall(r'<a[^>]*chat-list-item[^>]*>', html)
            for link in links:
                assert 'data-bs-dismiss' not in link, (
                    f"chat-list-item link must not have data-bs-dismiss: {link[:100]}"
                )

    def test_offcanvas_dismiss_handler_present(self, tmp_dirs):
        """The JS handler for dismissing offcanvas on chat clicks must be present."""
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        with app.test_client() as c:
            resp = c.get(f"/chat/{chat['id']}")
            html = resp.data.decode()
            assert "bootstrap.Offcanvas.getInstance(offcanvas)" in html

    def test_settings_has_chat_sidebar(self, tmp_dirs):
        """Settings page should render the chat list sidebar."""
        from pengy.core.chat_manager import create_chat, save_chat

        chat = create_chat()
        chat["title"] = "SidebarTest"
        save_chat(chat)

        with app.test_client() as c:
            resp = c.get("/settings")
            html = resp.data.decode()
            assert "SidebarTest" in html
            assert "chat-list-item" in html


# ────────────────────────────────────────────────────────────────────
# SSE stream format tests
# ────────────────────────────────────────────────────────────────────

class TestSSEStream:
    def test_no_worker_yields_error(self, client):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        resp = client.get(f"/chat/{chat['id']}/stream")
        # SSE data lines start with "data: "
        lines = resp.data.decode().strip().split("\n")
        data_line = [l for l in lines if l.startswith("data: ")]
        assert len(data_line) >= 1
        payload = json.loads(data_line[0][6:])  # strip "data: " prefix
        assert payload["type"] == "error"
        assert "No active task" in payload["message"]

    def test_stream_content_type(self, client):
        from pengy.core.chat_manager import create_chat

        chat = create_chat()
        resp = client.get(f"/chat/{chat['id']}/stream")
        assert "text/event-stream" in resp.content_type


# ────────────────────────────────────────────────────────────────────
# pytest marker
# ────────────────────────────────────────────────────────────────────
pytestmark = pytest.mark.web
