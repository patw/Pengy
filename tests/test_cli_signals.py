"""SIGTERM / SIGINT / SIGHUP during a one-shot pengy-cli run.

pengy-cli is mostly run by other programs as a subagent, and a parent that
gives up on it sends one of these.  Each must:

- kill the running tool's process group (it is in its own session, so the
  signal never reaches it on its own),
- repair and save the chat (no assistant tool_calls without a result),
- keep ``--output json`` stdout a valid document,
- and die by that same signal, promptly -- never exit 0.

"Promptly" is asserted as under ``EXIT_WITHIN`` seconds: the CLI takes a few
tens of milliseconds, but the bound must hold on a loaded CI machine too, and
a regression (waiting for the model, or for the 60s tool) still blows it.

These spawn the real CLI against a stub endpoint; the Rust and C++ editions
carry the same cases (``cli/tests/signals.rs``, ``cliSignal*`` in tests.cpp).
"""

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pengy.core import tools

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")

ROOT = Path(__file__).resolve().parent.parent
EXIT_WITHIN = 3.0


class _Stub(http.server.ThreadingHTTPServer):
    daemon_threads = True


def _start_stub(response: "dict | None"):
    """Serve *response* to every POST; ``None`` holds each request open.

    Returns ``(server, received)``; *received* is set once a request arrives.
    """
    received = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.set()
            if response is None:
                time.sleep(30)
                return
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = _Stub(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


def _tool_call_response(command: str) -> dict:
    return {
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_bash",
                                 "arguments": json.dumps({"command": command})},
                }],
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _gone(pid: int, within: float = 1.0) -> bool:
    """True once *pid* no longer exists (a killed child is reaped by init)."""
    deadline = time.monotonic() + within
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _dangling_tool_calls(config_dir: Path) -> list:
    dangling = []
    for path in (config_dir / "chats").glob("*.json"):
        if path.name == "index.json":
            continue
        messages = json.loads(path.read_text()).get("messages", [])
        answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        for m in messages:
            for call in m.get("tool_calls") or []:
                if call["id"] not in answered:
                    dangling.append(call["id"])
    return dangling


def _run_and_signal(tmp_path, sig, in_tool, output="json"):
    pid_file = tmp_path / "tool.pid"
    response = (_tool_call_response(f"echo $$ > '{pid_file}'; exec sleep 60")
                if in_tool else None)
    server, received = _start_stub(response)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({
        "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
        "api_key": "test", "model": "stub-model", "tool_confirmation": "all",
    }))
    env = dict(os.environ, PENGY_CONFIG_DIR=str(config_dir))
    proc = subprocess.Popen(
        [sys.executable, "-m", "pengy.cli.main", "--output", output, "run it"],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    tool_pid = None
    try:
        if in_tool:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not tool_pid:
                if pid_file.exists() and pid_file.read_text().strip():
                    tool_pid = int(pid_file.read_text())
                time.sleep(0.02)
            assert tool_pid, "the tool never started"
        else:
            assert received.wait(15), "the request never arrived"
        sent = time.monotonic()
        proc.send_signal(sig)
        stdout, stderr = proc.communicate(timeout=10)
        took = time.monotonic() - sent
    finally:
        if proc.poll() is None:
            proc.kill()
        server.shutdown()
        if tool_pid and not _gone(tool_pid, within=0):
            os.kill(tool_pid, signal.SIGKILL)
    return proc.returncode, took, stdout, stderr, tool_pid, config_dir


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP],
                         ids=lambda s: signal.Signals(s).name)
def test_signal_mid_tool_kills_the_tool_and_dies_by_the_signal(tmp_path, sig):
    rc, took, stdout, stderr, tool_pid, config_dir = _run_and_signal(tmp_path, sig, True)
    assert rc == -sig, stderr
    assert took < EXIT_WITHIN
    assert _gone(tool_pid), "the tool's process outlived the CLI"
    assert _dangling_tool_calls(config_dir) == []
    assert json.loads(stdout)["error"]["type"] == "interrupted"
    assert f"Interrupted by {signal.Signals(sig).name}" in stderr


def test_sigterm_mid_request_dies_without_waiting_for_the_model(tmp_path):
    rc, took, stdout, stderr, _, _ = _run_and_signal(tmp_path, signal.SIGTERM, False)
    assert rc == -signal.SIGTERM, stderr
    assert took < EXIT_WITHIN
    assert json.loads(stdout)["error"]["type"] == "interrupted"


def test_sigint_in_text_mode_keeps_stdout_clean(tmp_path):
    # It used to print "Cancelled." to stdout and exit 0.
    rc, _, stdout, _, _, _ = _run_and_signal(tmp_path, signal.SIGINT, False, output="raw")
    assert rc == -signal.SIGINT
    assert stdout == ""


def test_cleanup_askpass_helpers_removes_the_password_file():
    helper = tools._AskpassHelper("hunter2")
    pw_path = Path(helper._pw_path)
    assert pw_path.exists()
    tools.cleanup_askpass_helpers()
    assert not pw_path.exists()
    assert not Path(helper._dir).exists()
    helper.cleanup()  # the command's own cleanup afterwards is harmless
