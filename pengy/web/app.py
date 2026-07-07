"""Pengy Web UI — Flask server-side-rendered chat interface."""

import json
import queue
import re
import threading
import time

import markdown as _md
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from pygments.formatters import HtmlFormatter

from pengy.core.config import load_config, save_config, render_system_message
from pengy.core.llm_client import LLMClient
from pengy.core.chat_manager import (
    create_chat, delete_chat, get_chat, load_chats, save_chat,
    clean_dangling_tool_calls, elide_old_tool_results,
)
from pengy.core import tools


app = Flask(__name__)

_workers: dict[str, "WebWorker"] = {}
_workers_lock = threading.Lock()


def _safe_id(tool_call_id: str) -> str:
    """Convert a tool_call_id to a safe HTML element ID."""
    return "tc_" + re.sub(r"[^a-zA-Z0-9]", "", tool_call_id)


def _render_md(content: str) -> str:
    if not content:
        return ""
    return _md.markdown(
        content,
        extensions=["fenced_code", "codehilite", "tables"],
        extension_configs={
            "codehilite": {"css_class": "highlight", "guess_lang": True}
        },
    )


def _pygments_css() -> str:
    return HtmlFormatter(style="friendly").get_style_defs(".highlight")


def _build_messages(chat: dict, config: dict) -> list[dict]:
    system_msg = config.get("system_message", "")
    messages = []
    if system_msg:
        messages.append({"role": "system", "content": render_system_message(system_msg)})
    raw = clean_dangling_tool_calls(list(chat.get("messages", [])))
    raw = elide_old_tool_results(raw, config.get("context_keep_turns", 0))
    messages.extend(raw)
    return messages


def _group_messages(raw_messages: list[dict]) -> list[dict]:
    """Convert raw messages list to display-ready turn groups."""
    messages = clean_dangling_tool_calls(list(raw_messages))
    turns = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "user":
            turns.append({"type": "user", "content": msg.get("content", "")})
            i += 1

        elif role == "assistant":
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls")

            if tool_calls:
                if content:
                    turns.append({"type": "assistant", "html": _render_md(content)})

                events = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        args = {}
                    tc_id = tc.get("id", "")
                    events.append({
                        "name": fn.get("name", "?"),
                        "args": args,
                        "tool_call_id": tc_id,
                        "safe_id": _safe_id(tc_id),
                        "result": None,
                        "declined": False,
                    })

                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    tc_id = messages[i].get("tool_call_id", "")
                    result = messages[i].get("content", "")
                    for ev in events:
                        if ev["tool_call_id"] == tc_id:
                            ev["result"] = result
                            ev["declined"] = result in (
                                "Tool execution was declined by user.",
                                "Tool execution was cancelled by user.",
                            )
                    i += 1

                turns.append({"type": "tool_use", "events": events})

            else:
                if content:
                    turns.append({"type": "assistant", "html": _render_md(content)})
                i += 1

        elif role in ("tool", "system"):
            i += 1
        else:
            i += 1

    return turns


# ─── WebWorker ────────────────────────────────────────────────────────────────

class WebWorker:
    """Drives the LLMClient generator in a background thread.

    Mirrors the GUI's ChatWorker pattern: blocks on threading.Events for tool
    confirmation and sudo password, communicates results to the SSE stream via
    a queue.
    """

    def __init__(self, chat: dict, config: dict):
        self._chat = {**chat, "messages": list(chat.get("messages", []))}
        self._config = config
        self._queue: queue.Queue = queue.Queue()
        self._confirm_event = threading.Event()
        self._confirm_result: dict | None = None
        self._sudo_event = threading.Event()
        self._sudo_result: str | None = None
        self._done = False
        self._cancelled = False
        self._yolo_this_turn = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def cancel(self):
        self._cancelled = True
        self._confirm_event.set()
        self._sudo_event.set()

    def send_confirmation(self, confirmed: bool, tool_call_id: str, yolo_turn: bool = False):
        self._confirm_result = {
            "confirmed": confirmed,
            "tool_call_id": tool_call_id,
            "yolo_turn": yolo_turn,
        }
        self._confirm_event.set()

    def send_sudo_password(self, password: str | None):
        self._sudo_result = password
        self._sudo_event.set()

    def iter_events(self, timeout: float = 3600.0):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield {"type": "error", "message": "Stream timeout"}
                break
            try:
                event = self._queue.get(timeout=min(remaining, 25.0))
                yield event
                if event.get("type") in ("final_response", "error"):
                    break
            except queue.Empty:
                if self._done:
                    break
                yield {"type": "keepalive"}

    def _get_sudo_password(self) -> str | None:
        self._sudo_event.clear()
        self._queue.put({"type": "sudo_request"})
        self._sudo_event.wait(timeout=120.0)
        if self._cancelled:
            return None
        return self._sudo_result

    def _run(self):
        config = self._config
        chat = self._chat
        skip_final_save = False
        try:
            tools.set_sudo_password_provider(self._get_sudo_password)
            tools.set_user_agent(config.get("user_agent", "PengyAgent/1.0"))
            tools.set_tool_timeout(config.get("tool_timeout", 60))

            llm = LLMClient(
                base_url=config.get("base_url", "https://api.openai.com/v1"),
                api_key=config.get("api_key", ""),
                model=config.get("model", "gpt-4o"),
            )

            messages = _build_messages(chat, config)
            tc_mode = config.get("tool_confirmation", "none")
            gen = llm.chat(
                messages,
                tool_confirmation=tc_mode,
                reasoning_effort=config.get("reasoning_effort", ""),
                preserve_reasoning=bool(config.get("preserve_reasoning", False)),
            )
            send_value = None

            while True:
                if self._cancelled:
                    break
                try:
                    response = gen.send(send_value) if send_value is not None else next(gen)
                except StopIteration:
                    break
                send_value = None
                rtype = response.get("type", "")

                if rtype == "assistant_tool_calls":
                    self._yolo_this_turn = False
                    chat["messages"].append(response["message"])

                elif rtype == "tool_request":
                    name = response.get("name", "")
                    tool_call_id = response.get("tool_call_id", "")
                    args = response.get("args", {})

                    # Mirrors the generator's own skip_confirm logic so we know
                    # whether the generator will block waiting for a send() value.
                    skip_confirm = (
                        tc_mode == "all"
                        or (tc_mode == "safe" and tools.is_readonly_tool(name))
                    )
                    auto_approved = skip_confirm or self._yolo_this_turn

                    self._queue.put({
                        "type": "tool_request",
                        "name": name,
                        "args": args,
                        "tool_call_id": tool_call_id,
                        "safe_id": _safe_id(tool_call_id),
                        "auto_approved": auto_approved,
                    })

                    if not skip_confirm:
                        if self._yolo_this_turn:
                            # Generator expects a send() value even though we're
                            # auto-approving — the generator's skip_confirm was False.
                            send_value = {"confirmed": True, "tool_call_id": tool_call_id}
                        else:
                            self._confirm_event.clear()
                            self._confirm_event.wait(timeout=300.0)
                            if self._cancelled:
                                break
                            result = self._confirm_result
                            if result and result.get("yolo_turn"):
                                self._yolo_this_turn = True
                            send_value = result
                            self._confirm_result = None
                    # else: generator used statement-yield (skip_confirm=True), it
                    # doesn't read the send value, so leave send_value=None.

                elif rtype == "tool_result":
                    content = response.get("content", "")
                    declined = response.get("declined", False)
                    chat["messages"].append({
                        "role": "tool",
                        "tool_call_id": response["tool_call_id"],
                        "content": content,
                    })
                    display = content if len(content) <= 3000 else content[:3000] + "\n… [truncated]"
                    self._queue.put({
                        "type": "tool_result",
                        "tool_call_id": response["tool_call_id"],
                        "safe_id": _safe_id(response["tool_call_id"]),
                        "name": response.get("name", ""),
                        "content": display,
                        "declined": declined,
                    })

                elif rtype == "final_response":
                    content = response.get("content") or ""
                    chat["messages"].append(response.get("message") or {"role": "assistant", "content": content})
                    save_chat(chat)
                    self._queue.put({
                        "type": "final_response",
                        "html": _render_md(content),
                        "usage": response.get("usage", {}),
                    })
                    break

        except Exception as e:
            app.logger.error("Worker error for chat %s: %s", self._chat["id"], e)
            self._queue.put({"type": "error", "message": str(e)})
        finally:
            self._done = True
            tools.set_sudo_password_provider(None)
            # Normal final responses save before queuing final_response. Avoid
            # saving from error/cancel paths here; tests and callers may tear down
            # temporary config dirs while a failed worker is unwinding.
            # Do NOT remove from _workers here — the SSE endpoint holds a reference
            # and will clean up once it drains the queue.  Removing here causes a
            # race where a fast failure disappears before the browser SSE connects.


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    chats = load_chats()
    if chats:
        return redirect(url_for("chat_view", chat_id=chats[0]["id"]))
    chat = create_chat()
    return redirect(url_for("chat_view", chat_id=chat["id"]))


@app.route("/chat/new", methods=["POST"])
def new_chat():
    chat = create_chat()
    return redirect(url_for("chat_view", chat_id=chat["id"]))


@app.route("/chat/<chat_id>")
def chat_view(chat_id: str):
    chat = get_chat(chat_id)
    if not chat:
        return redirect(url_for("index"))
    chats = load_chats()
    config = load_config()
    turns = _group_messages(chat.get("messages", []))
    return render_template(
        "chat.html",
        chat=chat,
        chats=chats,
        config=config,
        turns=turns,
        pygments_css=_pygments_css(),
    )


@app.route("/chat/<chat_id>/send", methods=["POST"])
def chat_send(chat_id: str):
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Empty message"}), 400

    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    with _workers_lock:
        existing = _workers.get(chat_id)
    if existing:
        existing.cancel()

    chat["messages"].append({"role": "user", "content": content})
    if chat.get("title") == "New Chat":
        chat["title"] = content[:50] + ("…" if len(content) > 50 else "")
    save_chat(chat)

    config = load_config()
    worker = WebWorker(chat, config)
    with _workers_lock:
        _workers[chat_id] = worker
    worker.start()

    return jsonify({"status": "ok", "title": chat["title"]})


@app.route("/chat/<chat_id>/stream")
def chat_stream(chat_id: str):
    def generate():
        worker = None
        for _ in range(30):
            with _workers_lock:
                worker = _workers.get(chat_id)
            if worker:
                break
            time.sleep(0.1)

        if not worker:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No active task'})}\n\n"
            return

        try:
            for event in worker.iter_events():
                if event.get("type") == "keepalive":
                    yield ": keepalive\n\n"
                else:
                    yield f"data: {json.dumps(event)}\n\n"
        finally:
            # Clean up now that SSE has drained the queue
            with _workers_lock:
                if _workers.get(chat_id) is worker:
                    del _workers[chat_id]

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/chat/<chat_id>/confirm", methods=["POST"])
def chat_confirm(chat_id: str):
    data = request.get_json() or {}
    with _workers_lock:
        worker = _workers.get(chat_id)
    if not worker:
        return jsonify({"error": "No active task"}), 404
    worker.send_confirmation(
        confirmed=bool(data.get("confirmed")),
        tool_call_id=data.get("tool_call_id", ""),
        yolo_turn=bool(data.get("yolo_turn")),
    )
    return jsonify({"status": "ok"})


@app.route("/chat/<chat_id>/sudo", methods=["POST"])
def chat_sudo(chat_id: str):
    data = request.get_json() or {}
    with _workers_lock:
        worker = _workers.get(chat_id)
    if not worker:
        return jsonify({"error": "No active task"}), 404
    worker.send_sudo_password(data.get("password"))
    return jsonify({"status": "ok"})


@app.route("/chat/<chat_id>/delete", methods=["POST"])
def chat_delete(chat_id: str):
    with _workers_lock:
        worker = _workers.pop(chat_id, None)
    if worker:
        worker.cancel()
    delete_chat(chat_id)
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    config = load_config()
    saved = False
    if request.method == "POST":
        for key in ["base_url", "model", "system_message", "user_agent"]:
            config[key] = request.form.get(key, "").strip()
        api_key = request.form.get("api_key", "").strip()
        if api_key:
            config["api_key"] = api_key
        tc = request.form.get("tool_confirmation", "none")
        if tc in ("all", "safe", "none"):
            config["tool_confirmation"] = tc
        effort = request.form.get("reasoning_effort", "")
        if effort in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            config["reasoning_effort"] = effort
        config["preserve_reasoning"] = request.form.get("preserve_reasoning") == "1"
        try:
            config["tool_timeout"] = max(1, int(request.form.get("tool_timeout", 60)))
        except ValueError:
            pass
        try:
            config["context_keep_turns"] = max(0, int(request.form.get("context_keep_turns", 0)))
        except ValueError:
            pass
        save_config(config)
        saved = True
    chats = load_chats()
    return render_template("settings.html", config=config, saved=saved, chats=chats)
