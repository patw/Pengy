"""Pengy Web UI — Flask server-side-rendered chat interface."""

import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import markdown as _md
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for
from pygments.formatters import HtmlFormatter

from pengy.core.config import load_config, save_config, render_system_message
from pengy.core.model_cache import load_model_cache, save_model_cache
from pengy.core.llm_client import LLMClient
from pengy.core.chat_manager import (
    create_chat, delete_chat, get_chat, load_index, save_chat,
    clean_dangling_tool_calls, elide_old_tool_results,
)
from pengy.core.image_utils import preprocess as preprocess_image
from pengy.core import tools


app = Flask(__name__)

_workers: dict[str, "WebWorker"] = {}
_workers_lock = threading.Lock()

# Completed streams remain available briefly so a reconnecting browser can
# consume the terminal event.  Persisted chat history is authoritative after
# this grace period.
_EVENT_LOG_GRACE_SECONDS = 10 * 60

# ─── Request origin guard ─────────────────────────────────────────────────────
#
# Pengy Web has no authentication: anything that can reach it can run tools as
# the current user. Two browser-driven attacks defeat a loopback bind, since
# both are issued by the user's own browser:
#
#   CSRF          — a page on any origin auto-submits a form to 127.0.0.1 and
#                   rewrites settings (base_url, system_message, YOLO mode).
#   DNS rebinding — an attacker domain re-resolves to 127.0.0.1, so the browser
#                   treats it as same-origin and can *read* replies too.
#
# Two cheap checks close both. Neither uses tokens or sessions, so there is
# nothing to thread through templates.

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# Set by pengy.web.main from --host. When the operator explicitly binds a
# non-loopback address they are fronting this with a VM boundary, an nginx
# proxy, or their own auth, and Host will legitimately be an arbitrary domain,
# so the Host allowlist is skipped in that case.
_bound_host = "127.0.0.1"

# Extra hostnames accepted in Host *and* Origin, from --trusted-host. Required
# when running behind a reverse proxy on a loopback bind: nginx either forwards
# the public domain as Host (proxy_set_header Host $host) or forwards its own
# upstream address and leaves the browser's public Origin unmatched (nginx's
# default, Host $proxy_host). Naming the public hostname covers both.
_trusted_hosts: set[str] = set()


def set_bound_host(host: str) -> None:
    """Record the interface the server was told to bind (see _bound_host)."""
    global _bound_host
    _bound_host = host


def set_trusted_hosts(hosts) -> None:
    """Record hostnames this server may legitimately be reached as."""
    global _trusted_hosts
    _trusted_hosts = {_host_only(h) for h in hosts if h.strip()}


def _is_allowed_host(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host in _trusted_hosts


def _host_only(value: str) -> str:
    """Strip any :port from a Host/Origin authority, keeping [::1] intact."""
    value = value.strip()
    if value.startswith("["):                      # bracketed IPv6 literal
        end = value.find("]")
        if end != -1:
            return value[: end + 1].lower()
    # Only strip a trailing :port when it is unambiguous. A bare IPv6 literal
    # such as "::1" (from --host ::1) has many colons and no port.
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.lower()


@app.before_request
def _guard_request_origin():
    """Reject cross-origin and rebound-DNS requests before any handler runs."""
    host = _host_only(request.host)

    # 1. DNS rebinding: when bound to loopback, the browser should only ever
    #    address us as localhost (or a --trusted-host name, for a proxy). An
    #    attacker-controlled name resolving to 127.0.0.1 arrives with that name
    #    in Host, so it fails here.
    if _host_only(_bound_host) in _LOOPBACK_HOSTS:
        if not _is_allowed_host(host):
            return jsonify({"error": "Invalid Host header"}), 403

    # 2. CSRF: accept an Origin matching the Host the request was actually sent
    #    to, or any --trusted-host (a proxy may forward its own upstream Host
    #    while the browser reports the public origin). An attacker page's
    #    Origin is its own, and never either of those. Origin is absent on
    #    non-browser clients (curl) and on same-origin GETs, so only enforce
    #    it when present.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin:
            origin_host = _host_only(urllib.parse.urlsplit(origin).netloc)
            if origin_host != host and origin_host not in _trusted_hosts:
                return jsonify({"error": "Cross-origin request blocked"}), 403

    return None

# Allowed directories for serving local files via /files route.
# Only files under these directories (after symlink resolution) are served.
_ALLOWED_FILE_ROOTS = [
    Path.home() / "Pictures",
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path("/tmp"),
]


def _safe_id(tool_call_id: str) -> str:
    """Convert a tool_call_id to a safe HTML element ID."""
    return "tc_" + re.sub(r"[^a-zA-Z0-9]", "", tool_call_id)

_SUMMARY_SECRET_KEYS = {
    "password", "passwd", "api_key", "apikey", "token", "access_token",
    "refresh_token", "authorization", "secret", "private_key",
}


def _tool_summary(name: str, args: object) -> str:
    """Return a short, non-sensitive description for a collapsed tool card."""
    if not isinstance(args, dict):
        return ""

    def value(key: str) -> str:
        raw = args.get(key, "")
        if key.lower() in _SUMMARY_SECRET_KEYS:
            return "[redacted]"
        if isinstance(raw, (dict, list)):
            return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        return str(raw).replace("\n", " ").strip()

    if name in {"read_file", "write_file", "replace_in_file", "directory_tree"}:
        summary = value("path")
    elif name == "read_multiple_files":
        paths = args.get("paths")
        summary = f"{len(paths)} files" if isinstance(paths, list) else ""
    elif name in {"web_search"}:
        summary = value("query")
    elif name == "fetch_url":
        summary = value("url")
    elif name == "download_file":
        summary = value("filename") or value("url")
    elif name in {"run_bash", "run_python"}:
        summary = value("command") or value("code")
    elif name in {"search_content", "glob"}:
        pattern = value("pattern")
        path = value("path")
        summary = f"{pattern} in {path}" if pattern and path else pattern or path
    elif name == "apply_changes":
        changes = args.get("changes")
        summary = f"{len(changes)} files" if isinstance(changes, list) else ""
    elif name == "ask_user_question":
        questions = args.get("questions")
        summary = f"{len(questions)} questions" if isinstance(questions, list) else ""
    else:
        for key, raw in args.items():
            if key.lower() in _SUMMARY_SECRET_KEYS:
                continue
            summary = str(raw).replace("\n", " ").strip()
            if summary:
                break
        else:
            summary = ""

    return summary if len(summary) <= 100 else summary[:97].rstrip() + "…"


def _fix_file_urls(html: str) -> str:
    """Replace ``file://`` image URLs with ``/files?path=`` URLs that browsers can load.

    Browsers block ``file://`` URLs from HTTP pages as a security restriction.
    This converts them to a Flask-served route so images generated by tools
    (plot, image-gen, etc.) render correctly in the web UI.
    """
    def _replace(match: re.Match) -> str:
        quote = match.group(1)
        file_url = match.group(2)
        # Strip the file:// prefix
        raw = file_url[7:] if file_url.startswith("file://") else file_url
        # Expand ~ to the user's home directory
        if raw.startswith("~"):
            raw = str(Path.home()) + raw[1:]
        # URL-encode the path so it survives as a query parameter
        quoted = urllib.parse.quote(raw, safe="")
        return f'src={quote}/files?path={quoted}{quote}'

    # Match <img src="file://..."> — capture the quote and the URL separately
    html = re.sub(
        r'src=(["\'])(file://[^"\']+)\1',
        _replace,
        html,
    )
    return html


# One reusable parser guarded by a lock; constructing a fresh Markdown() per
# message (as _md.markdown() does) is ~6x slower and this runs for every
# assistant message when a chat page is rendered. The lock keeps concurrent
# Flask request/worker threads from interleaving reset()/convert().
_md_parser = _md.Markdown(
    extensions=["fenced_code", "codehilite", "tables"],
    extension_configs={
        "codehilite": {"css_class": "highlight", "guess_lang": True}
    },
)
_md_lock = threading.Lock()


def _render_md(content: str) -> str:
    if not content:
        return ""
    with _md_lock:
        _md_parser.reset()
        html = _md_parser.convert(content)
    return _fix_file_urls(html)


def _pygments_css() -> str:
    """Syntax-highlight CSS for both themes, scoped by ``data-bs-theme``.

    Both rule sets are always emitted; the ``data-bs-theme`` attribute on
    <html> decides which one applies, so switching themes needs no reload.
    Styles match the desktop GUI (see ui/theme.py): friendly / monokai.
    """
    light = HtmlFormatter(style="friendly").get_style_defs(
        '[data-bs-theme="light"] .highlight'
    )
    dark = HtmlFormatter(style="monokai").get_style_defs(
        '[data-bs-theme="dark"] .highlight'
    )
    return light + "\n" + dark


def _theme_mode(config: dict) -> str:
    """Return the configured theme mode, validated. 'system' is resolved client-side."""
    mode = config.get("theme_mode", "system")
    return mode if mode in ("system", "light", "dark") else "system"


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
                        "summary": _tool_summary(fn.get("name", "?"), args),
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
    """Drives the LLMClient generator in a background thread."""

    def __init__(self, chat: dict, config: dict, messages_override: list[dict] | None = None):
        self._chat = {**chat, "messages": list(chat.get("messages", []))}
        self._config = config
        self._messages_override = messages_override  # pre-built API messages
        # Append-only event log so reconnecting SSE clients can resume from
        # where they left off.  A single queue would drop events if a dead
        # connection happened to consume them during an auto-reconnect.
        self._events: list[dict] = []
        self._events_lock = threading.Lock()
        self._new_event = threading.Condition(self._events_lock)
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

    @property
    def event_count(self) -> int:
        """Number of events currently in the append-only log."""
        with self._events_lock:
            return len(self._events)

    def _put_event(self, event: dict) -> None:
        """Append an event to the log and wake any waiting SSE consumers."""
        with self._events_lock:
            self._events.append(event)
            if event.get("type") in ("final_response", "error"):
                self._done = True
            self._new_event.notify_all()

    def iter_events(self, start_index: int = 0, timeout: float = 3600.0):
        """Yield events from *start_index* onward, with keepalives.

        Never yield while holding ``_events_lock``: an SSE consumer can be
        suspended by a slow client at ``yield``, while the worker must still be
        able to append progress events.
        """
        deadline = time.monotonic() + timeout
        index = start_index
        while True:
            event = None
            with self._events_lock:
                if index < len(self._events):
                    event = self._events[index]
                    index += 1
                elif self._done:
                    return
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        event = {"type": "error", "message": "Stream timeout"}
                    else:
                        notified = self._new_event.wait(timeout=min(remaining, 25.0))
                        if not notified:
                            event = {"type": "keepalive"}

            # Deliberately outside the mutex; see docstring above.
            if event is not None:
                yield event
                if event.get("type") in ("final_response", "error"):
                    return

    def _get_sudo_password(self) -> str | None:
        self._sudo_event.clear()
        self._put_event({"type": "sudo_request"})
        self._sudo_event.wait(timeout=120.0)
        if self._cancelled:
            return None
        return self._sudo_result

    def _run(self):
        config = self._config
        chat = self._chat
        try:
            tools.set_sudo_password_provider(self._get_sudo_password)
            tools.set_user_agent(config.get("user_agent", "PengyAgent/1.0"))
            tools.set_tool_timeout(config.get("tool_timeout", 300))
            tools.set_tool_output_max_chars(config.get("tool_output_max_chars", 250000))
            tools.set_download_max_mb(config.get("download_max_mb", 100))
            tools.set_image_limits(
                config.get("image_max_dimension", 4096),
                config.get("image_max_mb", 4.5),
                config.get("image_quality", 85),
            )

            llm = LLMClient(
                base_url=config.get("base_url", "https://api.openai.com/v1"),
                api_key=config.get("api_key", ""),
                model=config.get("model", "gpt-4o"),
                llm_timeout=config.get("llm_timeout", 300),
            )

            messages = self._messages_override or _build_messages(chat, config)
            tc_mode = config.get("tool_confirmation", "none")
            gen = llm.chat(
                messages,
                tool_confirmation=tc_mode,
                reasoning_effort=config.get("reasoning_effort", ""),
                preserve_reasoning=bool(config.get("preserve_reasoning", False)),
                cancel_fn=lambda: self._cancelled,
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

                if rtype == "retrying":
                    # Backoff sleep handled inside the generator; push event
                    # through so the SSE stream shows "Overloaded, retrying…"
                    self._put_event(response)
                    continue

                if rtype == "assistant_tool_calls":
                    self._yolo_this_turn = False
                    chat["messages"].append(response["message"])

                elif rtype == "tool_request":
                    name = response.get("name", "")
                    tool_call_id = response.get("tool_call_id", "")
                    args = response.get("args", {})

                    skip_confirm = (
                        tc_mode == "all"
                        or (tc_mode == "safe" and tools.is_readonly_tool(name))
                    )
                    auto_approved = skip_confirm or self._yolo_this_turn

                    self._put_event({
                        "type": "tool_request",
                        "name": name,
                        "args": args,
                        "tool_call_id": tool_call_id,
                        "safe_id": _safe_id(tool_call_id),
                        "auto_approved": auto_approved,
                    })

                    if not skip_confirm:
                        if self._yolo_this_turn:
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

                elif rtype == "question_request":
                    questions = response.get("questions", [])
                    tool_call_id = response.get("tool_call_id", "")
                    self._put_event({
                        "type": "question_request",
                        "questions": questions,
                        "tool_call_id": tool_call_id,
                        "safe_id": _safe_id(tool_call_id),
                    })
                    self._confirm_event.clear()
                    self._confirm_event.wait(timeout=300.0)
                    if self._cancelled:
                        break
                    result = self._confirm_result
                    if result and result.get("answered"):
                        send_value = {"answered": True, "tool_call_id": tool_call_id, "answers": result["answers"]}
                    else:
                        send_value = None
                    self._confirm_result = None

                elif rtype == "question_result":
                    content = response.get("content", "")
                    self._put_event({
                        "type": "tool_result",
                        "tool_call_id": response["tool_call_id"],
                        "safe_id": _safe_id(response["tool_call_id"]),
                        "name": response.get("name", ""),
                        "content": content,
                        "declined": False,
                    })

                elif rtype == "tool_result":
                    content = response.get("content", "")
                    declined = response.get("declined", False)
                    chat["messages"].append({
                        "role": "tool",
                        "tool_call_id": response["tool_call_id"],
                        "content": content,
                    })
                    display = content if len(content) <= 3000 else content[:3000] + "\n… [truncated]"
                    self._put_event({
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
                    self._put_event({
                        "type": "final_response",
                        "html": _render_md(content),
                        "usage": response.get("usage", {}),
                    })
                    break

        except Exception as e:
            app.logger.error("Worker error for chat %s: %s", self._chat["id"], e)
            self._put_event({"type": "error", "message": str(e)})
        finally:
            self._done = True
            tools.set_sudo_password_provider(None)
            # Do not depend on a client reconnecting to free completed
            # workers.  Keep the replay log for a bounded reconnect grace
            # period, then let persisted chat history take over.
            def cleanup() -> None:
                with _workers_lock:
                    if _workers.get(self._chat["id"]) is self:
                        del _workers[self._chat["id"]]
            timer = threading.Timer(_EVENT_LOG_GRACE_SECONDS, cleanup)
            timer.daemon = True
            timer.start()


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    chats = load_index()
    if chats:
        return redirect(url_for("chat_view", chat_id=chats[0]["id"]))
    chat = create_chat()
    return redirect(url_for("chat_view", chat_id=chat["id"]))


@app.route("/chat/new", methods=["POST"])
def new_chat():
    chats = load_index()
    if chats and chats[0]["title"] == "New Chat" and not chats[0]["msg_count"]:
        return redirect(url_for("chat_view", chat_id=chats[0]["id"]))
    chat = create_chat()
    return redirect(url_for("chat_view", chat_id=chat["id"]))


@app.route("/chat/<chat_id>")
def chat_view(chat_id: str):
    chat = get_chat(chat_id)
    if not chat:
        return redirect(url_for("index"))
    # Sidebar summaries only — no message bodies needed to render the list.
    chats = load_index()
    config = load_config()
    turns = _group_messages(chat.get("messages", []))
    with _workers_lock:
        worker = _workers.get(chat_id)
    has_active_worker = worker is not None and not worker._done
    return render_template(
        "chat.html",
        chat=chat,
        chats=chats,
        config=config,
        turns=turns,
        pygments_css=_pygments_css(),
        has_active_worker=has_active_worker,
        theme_mode=_theme_mode(config),
    )


@app.route("/chat/<chat_id>/send", methods=["POST"])
def chat_send(chat_id: str):
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()

    config = load_config()

    # Handle attached files (base64-encoded from client-side).
    attached_files = data.get("files") or []
    text_blocks = []
    image_parts = []

    if attached_files:
        import base64
        import tempfile
        max_dim = config.get("image_max_dimension", 4096)
        max_mb = config.get("image_max_mb", 4.5)
        quality = config.get("image_quality", 85)

        for f in attached_files:
            fname = f.get("name", "file")
            fmime = f.get("mime", "")
            fdata = base64.b64decode(f.get("data", ""))

            # Detect image by MIME or extension
            is_image = fmime.startswith("image/") or any(
                fname.lower().endswith(ext)
                for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
            )

            if is_image:
                # Write to temp file for preprocessing
                suffix = Path(fname).suffix or ".png"
                fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="pengy_web_")
                os.close(fd)
                try:
                    Path(tmp_path).write_bytes(fdata)
                    img_bytes, img_mime = preprocess_image(
                        Path(tmp_path),
                        max_dimension=max_dim, max_mb=max_mb, quality=quality,
                    )
                    b64 = base64.b64encode(img_bytes).decode()
                    image_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{img_mime};base64,{b64}"},
                    })
                except Exception:
                    pass
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                try:
                    fcontent = fdata.decode("utf-8", errors="replace")
                    text_blocks.append(f"[File: {fname}]\n```\n{fcontent}\n```")
                except Exception:
                    pass

    # Build the final content/message
    if image_parts:
        # Multimodal message with images + optional text
        parts = list(image_parts)
        if text_blocks:
            parts.append({"type": "text", "text": "\n\n".join(text_blocks)})
        if content:
            parts.append({"type": "text", "text": content})
        # Message content is the multimodal array
        user_content = parts
        # For display/saving, use a placeholder-based string
        display_parts = [f"[Image: {f.get('name', 'image')}]" for f in attached_files
                         if f.get("mime", "").startswith("image/") or any(
                             f.get("name", "").lower().endswith(ext)
                             for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))]
        if text_blocks:
            display_parts.append("\n\n".join(text_blocks))
        if content:
            display_parts.append(content)
        display_content = "\n".join(display_parts)
    elif text_blocks:
        content = "\n\n".join(text_blocks) + "\n" + content
        display_content = content
        user_content = content
    else:
        display_content = content
        user_content = content

    if not (content.strip() or text_blocks or image_parts):
        return jsonify({"error": "Empty message"}), 400

    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    with _workers_lock:
        existing = _workers.get(chat_id)
    if existing:
        existing.cancel()

    # Store the display version in chat history (no base64 bloat)
    chat["messages"].append({"role": "user", "content": display_content})
    if chat.get("title") == "New Chat":
        chat["title"] = display_content[:50] + ("…" if len(display_content) > 50 else "")
    save_chat(chat)

    # The worker will build the proper API message from chat history
    # Build the real API messages (with actual image data) for the worker
    api_messages = None
    if image_parts:
        api_messages = _build_messages(chat, config)
        # Replace the last user message with the multimodal version
        # The last message in api_messages should be the user message
        # Find and replace it
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i].get("role") == "user":
                api_messages[i]["content"] = user_content  # multimodal array
                break

    worker = WebWorker(chat, config, messages_override=api_messages)
    with _workers_lock:
        _workers[chat_id] = worker
    worker.start()

    return jsonify({"status": "ok", "title": chat["title"]})


@app.route("/chat/<chat_id>/stream")
def chat_stream(chat_id: str):
    # Browser sends Last-Event-ID when EventSource auto-reconnects after a
    # drop.  Capture it here, inside the request context, before we hand the
    # generator off to Response.
    # Explicit `after` is used only when script code constructs a replacement
    # EventSource; normal EventSource reconnects retain Last-Event-ID.
    last_id = request.args.get("after", request.headers.get("Last-Event-ID"))
    try:
        resume_index = int(last_id) + 1 if last_id is not None else None
    except ValueError:
        resume_index = None
    if resume_index is not None and resume_index < 0:
        resume_index = 0

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

        # Tell the browser to retry after 1 s if the connection drops
        # (default is 3 s).  This makes reconnection after phone sleep / app
        # switch feel snappier.
        yield "retry: 1000\n\n"

        # Resume right after Last-Event-ID so no messages are replayed twice.
        start_index = resume_index if resume_index is not None else 0

        # Fresh connection (no Last-Event-ID) to a worker that's already done:
        # the chat page already rendered the history server-side, so only
        # replay the terminal event instead of the whole task.
        if resume_index is None and worker._done:
            start_index = max(0, worker.event_count - 1)

        try:
            event_index = start_index
            for event in worker.iter_events(start_index=start_index):
                if event.get("type") == "keepalive":
                    yield ": keepalive\n\n"
                else:
                    yield f"id: {event_index}\ndata: {json.dumps(event)}\n\n"
                    event_index += 1
        finally:
            with _workers_lock:
                # A disconnected stream never owns worker lifetime. Completed
                # logs are removed by the bounded grace-period cleanup started
                # by the worker, while an active worker survives reconnects.
                pass

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


@app.route("/chat/<chat_id>/stop", methods=["POST"])
def chat_stop(chat_id: str):
    with _workers_lock:
        worker = _workers.get(chat_id)
    if worker:
        worker.cancel()
    return jsonify({"status": "ok"})


@app.route("/chat/<chat_id>/delete", methods=["POST"])
def chat_delete(chat_id: str):
    with _workers_lock:
        worker = _workers.pop(chat_id, None)
    if worker:
        worker.cancel()
    delete_chat(chat_id)
    return redirect(url_for("index"))


# ─── File serving for local images ────────────────────────────────────────────

@app.route("/files")
def serve_file():
    """Serve a local file securely.

    Only files under allowed directories (Pictures, Downloads, Desktop, /tmp)
    are served.  Symlinks are resolved before the check to prevent escape.
    This lets tools like the plot skill render their output images in the
    browser, which blocks ``file://`` URLs from HTTP pages.
    """
    raw_path = request.args.get("path", "")
    if not raw_path:
        return jsonify({"error": "Missing path parameter"}), 400

    # Expand ~ and resolve to an absolute path with symlinks resolved
    try:
        p = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return jsonify({"error": "Invalid path"}), 400

    if not p.is_file():
        return jsonify({"error": "File not found"}), 404

    # Security: ensure the resolved path is under an allowed root
    allowed = any(
        str(p).startswith(str(root.resolve()) + os.sep) or p == root.resolve()
        for root in _ALLOWED_FILE_ROOTS
    )
    if not allowed:
        return jsonify({"error": "Access denied"}), 403

    # Guess MIME type; default to image/png for unknown (most common for plots)
    mime, _ = mimetypes.guess_type(str(p))
    if mime is None:
        mime = "image/png"

    try:
        return send_file(str(p), mimetype=mime)
    except OSError:
        return jsonify({"error": "Cannot read file"}), 500


# ─── NEW: Export ──────────────────────────────────────────────────────────────

@app.route("/chat/<chat_id>/export")
def chat_export(chat_id: str):
    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    msgs = chat.get("messages", [])
    lines = []
    lines.append(f"# {chat.get('title', 'Chat')}")
    lines.append(f"*Exported {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    lines.append("")

    for msg in msgs:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif isinstance(p, dict) and p.get("type") == "image_url":
                    parts.append("[image]")
            content = " ".join(parts)
        content = str(content) if content else ""

        if role == "user":
            lines.append("### 🧑 You")
            lines.append(content)
            lines.append("")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                lines.append("### 🤖 Assistant (tool calls)")
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    lines.append(f"- **{fn.get('name', '?')}**")
                    try:
                        args_json = json.loads(fn.get("arguments", "{}"))
                        lines.append(f"  ```json\n  {json.dumps(args_json, indent=2)}\n  ```")
                    except Exception:
                        lines.append(f"  `{fn.get('arguments', '')}`")
                lines.append("")
            if content:
                lines.append("### 🤖 Assistant")
                lines.append(content)
                lines.append("")
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "?")
            lines.append(f"#### 🔧 Tool result (`{tc_id}`)")
            lines.append("```")
            lines.append(content)
            lines.append("```")
            lines.append("")
        elif role == "system":
            lines.append(f"*System: {content[:200]}*")
            lines.append("")

    safe_title = re.sub(r'[^a-zA-Z0-9 _-]', '', chat.get("title", "chat"))
    safe_title = safe_title.strip()[:50] or "chat"
    filename = f"{safe_title}.md"

    return Response(
        "\n".join(lines),
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── NEW: Rename ──────────────────────────────────────────────────────────────

@app.route("/chat/<chat_id>/rename", methods=["POST"])
def chat_rename(chat_id: str):
    data = request.get_json() or {}
    new_title = (data.get("title") or "").strip()
    if not new_title:
        return jsonify({"error": "Empty title"}), 400

    chat = get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    chat["title"] = new_title
    save_chat(chat)
    return jsonify({"status": "ok", "title": new_title})


# ─── NEW: Slash Command Handler ───────────────────────────────────────────────

@app.route("/chat/<chat_id>/command", methods=["POST"])
def chat_command(chat_id: str):
    """Handle slash commands from the chat input."""
    data = request.get_json() or {}
    cmd_text = (data.get("command") or "").strip()

    if not cmd_text.startswith("/"):
        return jsonify({"error": "Not a command"}), 400

    parts = cmd_text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    config = load_config()

    def _public(cfg: dict) -> dict:
        """Only the fields the client actually renders — never the API key."""
        return {
            "model": cfg.get("model", ""),
            "tool_confirmation": cfg.get("tool_confirmation", "none"),
        }

    # Commands that return config changes
    if cmd in ("/yolo",):
        modes = ["none", "safe", "all"]
        current = config.get("tool_confirmation", "none")
        new_mode = args[0].lower() if args and args[0].lower() in modes else modes[(modes.index(current) + 1) % 3]
        config["tool_confirmation"] = new_mode
        save_config(config)
        labels = {"all": "YOLO", "safe": "Safe", "none": "Confirm All"}
        return jsonify({
            "type": "config",
            "message": f"Tool Confirmation: {labels[new_mode]}",
            "config": _public(config),
        })

    if cmd == "/model" and args:
        config["model"] = args[0]
        save_config(config)
        return jsonify({
            "type": "config",
            "message": f"Model: {args[0]}",
            "config": _public(config),
        })

    if cmd in ("/new",):
        chat = create_chat()
        return jsonify({"type": "redirect", "url": url_for("chat_view", chat_id=chat["id"])})

    if cmd in ("/export",):
        return jsonify({"type": "redirect", "url": url_for("chat_export", chat_id=chat_id)})

    if cmd in ("/rename",) and args:
        new_title = " ".join(args)
        chat = get_chat(chat_id)
        if chat:
            chat["title"] = new_title
            save_chat(chat)
            return jsonify({"type": "rename", "title": new_title})

    if cmd in ("/help",):
        return jsonify({"type": "message", "message": (
            "Slash commands: /new /yolo [none|safe|all] /model <name> "
            "/rename <title> /export /help"
        )})

    return jsonify({"type": "message", "message": f"Unknown command: {cmd}. Try /help."})


# ─── NEW: Fetch Models API ────────────────────────────────────────────────────

@app.route("/models")
def api_models():
    """Return the model list for the configured endpoint.

    Serves the persistent cache when it matches the configured base URL,
    so the settings page is populated immediately without hitting the
    network.  ``?refresh=1`` forces a live fetch (and updates the cache);
    if that fetch fails, the stale cache is returned with ``stale: true``
    so the UI can still show *something*.
    """
    config = load_config()
    base_url = config.get("base_url", "").rstrip("/")
    api_key = config.get("api_key", "")
    models_url = f"{base_url}/models"
    refresh = request.args.get("refresh") in ("1", "true")

    cache = load_model_cache()
    cache_matches = bool(cache) and (
        cache["url"].rstrip("/").lower() == base_url.lower()
    )

    if cache_matches and not refresh:
        return jsonify({
            "models": cache["models"],
            "cached": True,
            "fetched_at": cache["fetched_at"],
        })

    try:
        req = urllib.request.Request(models_url)
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("api-key", api_key)
        req.add_header("User-Agent", config.get("user_agent", "PengyAgent/1.0"))
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        model_ids = sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))
        if model_ids:
            save_model_cache(base_url, model_ids)
        return jsonify({
            "models": model_ids,
            "cached": False,
            "fetched_at": int(time.time()) if model_ids else None,
        })
    except Exception as e:
        if cache_matches:  # live fetch failed — fall back to the stale list
            return jsonify({
                "models": cache["models"],
                "cached": True,
                "stale": True,
                "fetched_at": cache["fetched_at"],
                "error": str(e),
            })
        return jsonify({"error": str(e)}), 502


# ─── Favicon ───────────────────────────────────────────────────


_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


@app.route("/favicon.ico")
def favicon():
    """Serve the PNG favicon."""
    png_path = _ASSETS_DIR / "icon.png"
    if not png_path.is_file():
        return "", 404
    return send_file(str(png_path), mimetype="image/png")


# ─── Settings ──────────────────────────────────────────────────────────────────


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
        mode = request.form.get("theme_mode", "system")
        if mode in ("system", "light", "dark"):
            config["theme_mode"] = mode
        effort = request.form.get("reasoning_effort", "")
        if effort in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            config["reasoning_effort"] = effort
        config["preserve_reasoning"] = request.form.get("preserve_reasoning") == "1"
        try:
            config["llm_timeout"] = max(1, int(request.form.get("llm_timeout", 300)))
        except ValueError:
            pass
        try:
            config["tool_timeout"] = max(1, int(request.form.get("tool_timeout", 300)))
        except ValueError:
            pass
        try:
            config["tool_output_max_chars"] = max(0, int(request.form.get("tool_output_max_chars", 250000)))
        except ValueError:
            pass
        try:
            config["download_max_mb"] = max(0, int(request.form.get("download_max_mb", 100)))
        except ValueError:
            pass
        try:
            config["context_keep_turns"] = max(0, int(request.form.get("context_keep_turns", 0)))
        except ValueError:
            pass
        save_config(config)
        saved = True
    chats = load_index()
    return render_template(
        "settings.html", config=config, saved=saved, chats=chats,
        theme_mode=_theme_mode(config),
    )
