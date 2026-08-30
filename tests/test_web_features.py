"""Route tests for the newer web features: export, rename, slash commands,
/models fetch, and file attachments on /send.

These cover the endpoints added after test_web.py was written; keep scenarios
in sync with PengyR's web tests and PengyCPP's tests.cpp web section.
"""
from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pengy.web.app import app
from pengy.core.config import set_config_dir, get_config_dir, load_config, save_config
from pengy.core.chat_manager import create_chat, get_chat, save_chat


@pytest.fixture
def tmp_cfg():
    with tempfile.TemporaryDirectory(prefix="pengy-webfeat-") as cfg_dir:
        set_config_dir(cfg_dir)
        assert get_config_dir() != Path.home() / ".config" / "pengy"
        # Point the LLM at a dead port so background workers fail fast
        # instead of attempting real network calls.
        config = load_config()
        config["base_url"] = "http://127.0.0.1:9/v1"
        config["api_key"] = "test"
        save_config(config)
        yield Path(cfg_dir)
    set_config_dir(None)


@pytest.fixture
def client(tmp_cfg):
    return app.test_client()


def make_chat(messages=None, title="Feature Test"):
    chat = create_chat()
    chat["title"] = title
    chat["messages"] = messages or []
    save_chat(chat)
    return chat


# ── Cumulative token usage ──────────────────────────────────────────────────────

class TestCumulativeTokens:
    def test_chat_view_shows_stored_cumulative_usage(self, client):
        chat = make_chat()
        chat["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        save_chat(chat)
        resp = client.get(f"/chat/{chat['id']}")
        assert "150 tokens" in resp.get_data(as_text=True)

    def test_chat_view_without_usage_does_not_crash(self, client):
        chat = make_chat()
        resp = client.get(f"/chat/{chat['id']}")
        assert resp.status_code == 200
        assert 'id="navTokens"' in resp.get_data(as_text=True)

    def test_add_usage_accumulates_across_worker_turns(self, client):
        """WebWorker's final_response handler must accumulate into
        chat['usage'], not just report the last turn's numbers."""
        from pengy.core.chat_manager import add_usage

        chat = make_chat()
        add_usage(chat, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        add_usage(chat, {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28})
        save_chat(chat)
        assert get_chat(chat["id"])["usage"] == {
            "prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43,
        }


# ── Export ─────────────────────────────────────────────────────────────────────

class TestExport:
    def test_export_returns_markdown_attachment(self, client):
        chat = make_chat([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there",
             "tool_calls": [{"id": "tc1", "type": "function",
                             "function": {"name": "read_file",
                                          "arguments": '{"path": "/tmp/x"}'}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file data"},
            {"role": "assistant", "content": "done"},
        ])
        resp = client.get(f"/chat/{chat['id']}/export")
        assert resp.status_code == 200
        assert "markdown" in resp.mimetype
        assert "attachment" in resp.headers["Content-Disposition"]
        body = resp.get_data(as_text=True)
        assert "# Feature Test" in body
        assert "### 🧑 You" in body
        assert "hello" in body
        assert "read_file" in body
        assert "file data" in body

    def test_export_unknown_chat_404(self, client):
        resp = client.get("/chat/nope/export")
        assert resp.status_code == 404

    def test_export_filename_sanitized(self, client):
        chat = make_chat(title='Weird/Name: <with> "chars"')
        resp = client.get(f"/chat/{chat['id']}/export")
        cd = resp.headers["Content-Disposition"]
        assert "/" not in cd.split("filename=")[1]
        assert "<" not in cd


# ── Rename ─────────────────────────────────────────────────────────────────────

class TestRename:
    def test_rename_persists(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/rename", json={"title": "New Name"})
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "New Name"
        assert get_chat(chat["id"])["title"] == "New Name"

    def test_rename_empty_title_400(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/rename", json={"title": "  "})
        assert resp.status_code == 400

    def test_rename_unknown_chat_404(self, client):
        resp = client.post("/chat/nope/rename", json={"title": "x"})
        assert resp.status_code == 404


# ── Redact ─────────────────────────────────────────────────────────────────────

class TestRedact:
    def test_redact_default_removes_one_message(self, client):
        chat = make_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        resp = client.post(f"/chat/{chat['id']}/redact")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"status": "ok", "removed": 1, "message_count": 1}
        assert get_chat(chat["id"])["messages"] == [{"role": "user", "content": "hi"}]

    def test_redact_count_removes_n(self, client):
        chat = make_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "two"},
        ])
        resp = client.post(f"/chat/{chat['id']}/redact", json={"count": 3})
        assert resp.get_json()["removed"] == 3
        assert get_chat(chat["id"])["messages"] == [{"role": "user", "content": "hi"}]

    def test_redact_more_than_available_empties_without_erroring(self, client):
        chat = make_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        resp = client.post(f"/chat/{chat['id']}/redact", json={"count": 50})
        assert resp.status_code == 200
        assert resp.get_json()["removed"] == 2
        assert get_chat(chat["id"])["messages"] == []

    def test_redact_repeatable_to_empty(self, client):
        chat = make_chat([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        client.post(f"/chat/{chat['id']}/redact")
        resp = client.post(f"/chat/{chat['id']}/redact")
        assert resp.get_json()["message_count"] == 0
        # A third redact against an already-empty chat must not error.
        resp = client.post(f"/chat/{chat['id']}/redact")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok", "removed": 0, "message_count": 0}

    def test_redact_unknown_chat_404(self, client):
        resp = client.post("/chat/nope/redact")
        assert resp.status_code == 404

    def test_redact_invalid_count_400(self, client):
        chat = make_chat([{"role": "user", "content": "hi"}])
        resp = client.post(f"/chat/{chat['id']}/redact", json={"count": 0})
        assert resp.status_code == 400
        resp = client.post(f"/chat/{chat['id']}/redact", json={"count": "abc"})
        assert resp.status_code == 400

    def test_redact_blocked_while_worker_active(self, client):
        chat = make_chat([{"role": "user", "content": "hi"}])
        from pengy.web import app as web_app

        class _FakeWorker:
            _done = False

        with web_app._workers_lock:
            web_app._workers[chat["id"]] = _FakeWorker()
        try:
            resp = client.post(f"/chat/{chat['id']}/redact")
            assert resp.status_code == 409
            assert get_chat(chat["id"])["messages"] == [{"role": "user", "content": "hi"}]
        finally:
            with web_app._workers_lock:
                web_app._workers.pop(chat["id"], None)


# ── Tasks ──────────────────────────────────────────────────────────────────────

class TestTasks:
    def test_list_tasks_empty(self, client):
        resp = client.get("/tasks")
        assert resp.status_code == 200
        assert resp.get_json() == {"tasks": []}

    def test_list_tasks_includes_placeholders(self, client):
        from pengy.core.task_manager import create_task

        task = create_task("Greet", "Say hello to %name% in %language%")
        resp = client.get("/tasks")
        data = resp.get_json()
        assert len(data["tasks"]) == 1
        entry = data["tasks"][0]
        assert entry["id"] == task["id"]
        assert entry["title"] == "Greet"
        assert entry["placeholders"] == ["name", "language"]

    def test_render_task_substitutes_values(self, client):
        from pengy.core.task_manager import create_task

        task = create_task("Greet", "Say hello to %name% in %language%")
        resp = client.post("/tasks/render", json={
            "id": task["id"],
            "values": {"name": "Ada", "language": "French"},
        })
        assert resp.status_code == 200
        assert resp.get_json() == {"prompt": "Say hello to Ada in French"}

    def test_render_task_no_placeholders(self, client):
        from pengy.core.task_manager import create_task

        task = create_task("Static", "Summarize the last file I read.")
        resp = client.post("/tasks/render", json={"id": task["id"], "values": {}})
        assert resp.get_json() == {"prompt": "Summarize the last file I read."}

    def test_render_unknown_task_404(self, client):
        resp = client.post("/tasks/render", json={"id": "nope", "values": {}})
        assert resp.status_code == 404

    def test_render_invalid_values_400(self, client):
        from pengy.core.task_manager import create_task

        task = create_task("Greet", "hi %name%")
        resp = client.post("/tasks/render", json={"id": task["id"], "values": "not-a-dict"})
        assert resp.status_code == 400


# ── Slash commands ─────────────────────────────────────────────────────────────

class TestCommands:
    def test_yolo_sets_mode_and_persists(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command",
                           json={"command": "/yolo safe"})
        data = resp.get_json()
        assert data["type"] == "config"
        assert "Safe" in data["message"]
        assert load_config()["tool_confirmation"] == "safe"

    def test_yolo_cycles_without_arg(self, client):
        chat = make_chat()
        config = load_config()
        config["tool_confirmation"] = "none"
        save_config(config)
        client.post(f"/chat/{chat['id']}/command", json={"command": "/yolo"})
        assert load_config()["tool_confirmation"] == "safe"

    def test_model_sets_and_persists(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command",
                           json={"command": "/model gpt-test-1"})
        assert resp.get_json()["type"] == "config"
        assert load_config()["model"] == "gpt-test-1"

    def test_new_redirects(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/new"})
        data = resp.get_json()
        assert data["type"] == "redirect"
        assert "/chat/" in data["url"]

    def test_export_command_redirects_to_export(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/export"})
        data = resp.get_json()
        assert data["type"] == "redirect"
        assert data["url"].endswith("/export")

    def test_rename_command(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command",
                           json={"command": "/rename My Renamed Chat"})
        data = resp.get_json()
        assert data["type"] == "rename"
        assert data["title"] == "My Renamed Chat"
        assert get_chat(chat["id"])["title"] == "My Renamed Chat"

    def test_help_lists_commands(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/help"})
        data = resp.get_json()
        assert data["type"] == "message"
        assert "/yolo" in data["message"]

    def test_unknown_command(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/wat"})
        data = resp.get_json()
        assert data["type"] == "message"
        assert "Unknown command" in data["message"]

    def test_non_command_400(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command",
                           json={"command": "just text"})
        assert resp.status_code == 400


class TestCommandConfigIsNarrowed:
    """Command responses must not ship the whole config — notably the API key.

    The browser only needs the two fields it renders in the navbar.
    """

    SECRET = "sk-must-never-reach-the-browser"

    def _with_key(self):
        config = load_config()
        config["api_key"] = self.SECRET
        save_config(config)

    @pytest.mark.parametrize("command", ["/yolo safe", "/model gpt-test-1"])
    def test_api_key_absent_from_response(self, client, command):
        self._with_key()
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": command})

        assert "api_key" not in resp.get_json()["config"]
        assert self.SECRET not in resp.get_data(as_text=True)

    @pytest.mark.parametrize("command", ["/yolo safe", "/model gpt-test-1"])
    def test_only_the_rendered_fields_are_exposed(self, client, command):
        self._with_key()
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": command})

        assert set(resp.get_json()["config"]) == {"model", "tool_confirmation"}

    def test_other_settings_stay_server_side(self, client):
        """base_url / system_message leak environment detail the UI never shows."""
        self._with_key()
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/yolo safe"})

        payload = resp.get_json()["config"]
        for field in ("base_url", "system_message", "user_agent"):
            assert field not in payload

    def test_narrowing_did_not_break_the_navbar_fields(self, client):
        """The two fields the JS reads must still be present and correct."""
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/yolo all"})

        payload = resp.get_json()["config"]
        assert payload["tool_confirmation"] == "all"
        assert payload["model"] == load_config()["model"]


class TestConfirmationLabels:
    """The safest mode ("none" = confirm every call) must not read as "None"."""

    @pytest.mark.parametrize("mode,expected", [
        ("all", "YOLO"),
        ("safe", "Safe"),
        ("none", "Confirm All"),
    ])
    def test_navbar_badge_label(self, client, mode, expected):
        config = load_config()
        config["tool_confirmation"] = mode
        save_config(config)

        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        badge = html[html.index('id="navConfirmBadge"'):]
        badge = badge[:badge.index("</span>")]
        assert expected in badge

    def test_safest_mode_badge_is_not_bare_none(self, client):
        config = load_config()
        config["tool_confirmation"] = "none"
        save_config(config)

        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        badge = html[html.index('id="navConfirmBadge"'):]
        badge = badge[:badge.index("</span>")]
        assert ">\n  None" not in badge and "> None" not in badge

    def test_yolo_command_reports_the_same_label(self, client):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/yolo none"})
        assert "Confirm All" in resp.get_json()["message"]


class TestFavicon:
    def test_served_as_png(self, client):
        resp = client.get("/favicon.ico")
        if resp.status_code == 404:
            pytest.skip("icon.png not present in this checkout")
        assert resp.mimetype == "image/png"

    def test_referenced_in_the_page_head(self, client):
        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert 'rel="icon"' in html


class TestScrollAffordances:
    """The message list must not yank the reader to the bottom mid-tool-run.

    The behaviour itself is client-side; these assert the pieces are wired up,
    which is as far as a template test can reach without a browser.
    """

    def test_jump_to_latest_button_present(self, client):
        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert 'id="jumpBottomBtn"' in html

    def test_sticky_append_helper_present(self, client):
        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert "function appendToArea(" in html
        assert "function isNearBottom(" in html

    def test_tool_card_shows_compact_summary(self, client):
        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert "function toolSummary(name, args)" in html
        assert "tool-summary" in html
        assert "behavior: 'instant'" not in html

    def test_streamed_appends_go_through_the_sticky_helper(self, client):
        """Direct appendChild + scrollToBottom in these paths is the old bug."""
        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()

        for fn in ("appendToolRequest", "appendAssistantMessage", "appendError"):
            body = html[html.index(f"function {fn}("):]
            body = body[:body.index("\n}")]
            assert "appendToArea(" in body, f"{fn} bypasses the sticky-scroll guard"

    def test_scroll_listener_keeps_the_button_in_sync(self, client):
        chat = make_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert "'scroll', updateJumpBtn" in html


# ── /models fetch ──────────────────────────────────────────────────────────────

class _ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/models"):
            payload = json.dumps(
                {"data": [{"id": "zeta-model"}, {"id": "alpha-model"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class TestModels:
    def test_models_sorted_from_endpoint(self, client):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            config = load_config()
            config["base_url"] = f"http://127.0.0.1:{httpd.server_port}/v1"
            save_config(config)

            resp = client.get("/models")
            assert resp.status_code == 200
            assert resp.get_json()["models"] == ["alpha-model", "zeta-model"]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_models_unreachable_502(self, client):
        # base_url points at the dead port from the fixture
        resp = client.get("/models")
        assert resp.status_code == 502
        assert "error" in resp.get_json()


# ── Attachments on /send ───────────────────────────────────────────────────────

class TestSendAttachments:
    def test_files_injected_as_fenced_blocks(self, client):
        chat = make_chat()
        payload = {
            "content": "what is in this file?",
            "files": [{"name": "note.txt",
                       "data": base64.b64encode(b"attachment body").decode()}],
        }
        resp = client.post(f"/chat/{chat['id']}/send", json=payload)
        assert resp.status_code == 200

        saved = get_chat(chat["id"])
        user_msg = saved["messages"][-1]
        assert user_msg["role"] == "user"
        assert "[File: note.txt]" in user_msg["content"]
        assert "attachment body" in user_msg["content"]
        assert user_msg["content"].rstrip().endswith("what is in this file?")

        # allow the doomed background worker to fail fast before teardown
        time.sleep(0.3)

    def test_files_only_no_text_is_accepted(self, client):
        chat = make_chat()
        payload = {
            "content": "",
            "files": [{"name": "a.txt",
                       "data": base64.b64encode(b"just the file").decode()}],
        }
        resp = client.post(f"/chat/{chat['id']}/send", json=payload)
        assert resp.status_code == 200
        assert "just the file" in get_chat(chat["id"])["messages"][-1]["content"]
        time.sleep(0.3)
