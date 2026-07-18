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
