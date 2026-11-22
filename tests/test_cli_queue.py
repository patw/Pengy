"""Queued type-ahead in the interactive CLI, driven through a real PTY.

Three behaviours that used to be wrong and are easy to break again:

1. What you type mid-turn is *queued*, one message per finished turn, instead of
   being left in the terminal buffer for whatever reads next.
2. A prompt raised mid-turn (tool confirmation) never consumes it, and Enter
   alone there re-asks instead of meaning "Execute".
3. The headless path is untouched: a single-shot run imports no line editor.

Both need a terminal -- ``typing.start()`` is a no-op without one -- so these run
the real CLI with a pty as its controlling terminal, against a stub endpoint.
"""

from __future__ import annotations

import fcntl
import http.server
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="pty-based")

ROOT = Path(__file__).resolve().parent.parent


class _Stub:
    """Canned replies, each served after its own delay; records user messages."""

    def __init__(self, plan):
        self.plan = list(plan)          # [(delay_secs, response_dict)]
        self.seen: list[str] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    request = json.loads(body)
                    messages = request.get("messages") or []
                    last = next((m for m in reversed(messages) if m.get("role") == "user"), None)
                    if last is not None:
                        with stub._lock:
                            stub.seen.append(last.get("content") or "")
                except Exception:
                    pass
                with stub._lock:
                    delay, response = stub.plan.pop(0) if stub.plan else (0.05, _text_response("ok"))
                time.sleep(delay)
                payload = json.dumps(response).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the CLI was stopped mid-turn; nothing to send

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def wait_for(self, text: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if any(text == m for m in self.seen):
                    return True
            time.sleep(0.05)
        return False

    def messages(self) -> list[str]:
        with self._lock:
            return list(self.seen)


def _text_response(text: str) -> dict:
    return {
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_call_response(command: str) -> dict:
    return {
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": [{"id": "call_1", "type": "function",
                                                 "function": {"name": "run_bash",
                                                              "arguments": json.dumps({"command": command})}}]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _Session:
    """The CLI under a pty: keystrokes in, all output captured."""

    def __init__(self, tmp_path, base_url, tool_confirmation="all"):
        config = tmp_path / "config"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "base_url": base_url, "api_key": "test", "model": "stub-model",
            "tool_confirmation": tool_confirmation,
        }))
        self.output = ""
        self._lock = threading.Lock()
        self.master, slave = pty.openpty()
        # A real window size: prompt_toolkit asks the terminal for its cursor
        # position and, with a 0x0 pty, spends its timeout flushing input --
        # which ate keystrokes that had not been captured yet.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pengy.cli.main", "--config-dir", str(config)],
            cwd=ROOT, env=dict(os.environ, PENGY_CONFIG_DIR=str(config)),
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True)
        os.close(slave)
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while True:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.1)
                if not ready:
                    if self.proc.poll() is not None:
                        return
                    continue
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self.output += chunk.decode("utf-8", "replace")

    def text(self) -> str:
        with self._lock:
            return self.output

    def type(self, text: str):
        os.write(self.master, text.encode())

    def plain(self) -> str:
        """Captured output with ANSI sequences removed (Rich colours split words)."""
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", self.text())

    def wait_for(self, needle: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.plain():
                return True
            time.sleep(0.05)
        return False

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        try:
            os.close(self.master)
        except OSError:
            pass


@pytest.fixture
def cli(tmp_path):
    sessions = []

    def start(stub, tool_confirmation="all"):
        session = _Session(tmp_path, stub.base_url, tool_confirmation)
        sessions.append(session)
        return session

    yield start
    for session in sessions:
        session.close()


def test_typed_lines_are_queued_and_sent_in_order(cli):
    stub = _Stub([(2.5, _text_response("R1")), (0.05, _text_response("R2")),
                  (0.05, _text_response("R3")), (0.05, _text_response("R4"))])
    session = cli(stub)
    assert session.wait_for("You"), session.text()

    session.type("first\r")
    time.sleep(0.8)
    session.type("QA\r")
    time.sleep(0.2)
    session.type("QB\r")
    time.sleep(0.2)
    session.type("QC\r")

    # Announced while the turn is still in flight (the queue's own feedback).
    assert session.wait_for("queued 3", 15), session.text()
    for expected in ("QA", "QB", "QC"):
        assert stub.wait_for(expected, 20), f"{expected} never sent: {stub.messages()}"
    assert stub.messages()[:4] == ["first", "QA", "QB", "QC"], stub.messages()


def test_confirmation_does_not_eat_typeahead_or_execute_on_enter(cli):
    stub = _Stub([(1.5, _tool_call_response("echo hi")), (0.05, _text_response("done")),
                  (0.05, _text_response("done2"))])
    session = cli(stub, tool_confirmation="none")
    assert session.wait_for("You"), session.text()

    session.type("first\r")
    time.sleep(0.5)
    session.type("my queued follow-up\r")

    assert session.wait_for("[1] Execute", 20), session.text()
    assert "queued 1" in session.plain(), session.text()

    # Enter alone must re-ask, never execute. Give the editor a moment to be the
    # one reading: the prompt text is printed before the editor starts.
    time.sleep(0.6)
    session.type("\r")
    assert session.wait_for("Press 1 to execute", 15), session.text()

    # Declining lets the turn finish, and the queued message goes out next.
    time.sleep(0.6)   # let the re-asked prompt start reading
    session.type("3\r")
    assert stub.wait_for("my queued follow-up", 20), stub.messages()


def test_blank_question_answer_does_not_pick_option_1(cli):
    """A blank line at a question used to be read as option 1."""
    question = {
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": [{"id": "call_q", "type": "function",
                                                 "function": {"name": "ask_user_question",
                                                              "arguments": json.dumps({"questions": [{
                                                                  "header": "Pick",
                                                                  "question": "Which one?",
                                                                  "options": [{"label": "alpha", "description": "a"},
                                                                              {"label": "beta", "description": "b"}],
                                                              }]})}}]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    stub = _Stub([(0.3, question), (0.05, _text_response("done"))])
    session = cli(stub, tool_confirmation="all")
    assert session.wait_for("You"), session.text()
    session.type("ask me\r")
    assert session.wait_for("Which one?", 20), session.text()
    time.sleep(0.6)
    session.type("\r")               # blank: must re-ask, not answer "alpha"
    time.sleep(1.5)
    text = session.plain()
    assert text.count("Choose [1-2]") >= 2, text


def test_headless_run_never_imports_the_line_editor():
    """cron / systemd / Qt-subagent runs must not pay for (or touch) the editor."""
    script = "import sys, pengy.cli.main; print('prompt_toolkit' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout


def test_single_shot_with_piped_stdin_is_unchanged(tmp_path):
    stub = _Stub([(0.05, _text_response("JSON-OK"))])
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({
        "base_url": stub.base_url, "api_key": "test", "model": "stub-model",
        "tool_confirmation": "all"}))
    out = subprocess.run(
        [sys.executable, "-m", "pengy.cli.main", "--config-dir", str(config),
         "--no-save", "--output", "json", "hello"],
        cwd=ROOT, env=dict(os.environ, PENGY_CONFIG_DIR=str(config)),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["content"] == "JSON-OK"
