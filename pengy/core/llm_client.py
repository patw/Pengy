"""LLM client for OpenAI-compatible APIs."""
import json
import random
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

from openai import APIConnectionError, APIStatusError, OpenAI

from pengy.core import tools as _tools_mod

_REASONING_MESSAGE_FIELDS = (
    "reasoning_content",
    "reasoning",
    "reasoning_details",
)

# ── 429 / 529 backoff ────────────────────────────────────────────
_MAX_RETRIES = 5
_BASE_DELAY = 1.0          # seconds
_MAX_DELAY = 60.0          # cap
_JITTER = 0.25             # ±25 %
_RETRYABLE_STATUSES = {429, 529}

# Sentinel for graceful image-stripping recovery.
_RETRY_WITHOUT_IMAGES = object()

# Only context errors get a size-reduction retry; generic bad requests do not.
_MAX_CONTEXT_RETRIES = 4
_CONTEXT_ERROR_CODES = {
    "context_length_exceeded", "context_window_exceeded", "prompt_too_long",
    "input_too_long", "max_context_length_exceeded", "token_limit_exceeded",
}
_CONTEXT_ERROR_PHRASES = (
    "context length", "context window", "context limit", "maximum context",
    "prompt too long", "input too long", "too many tokens", "token limit exceeded",
    "exceeds the model's context", "exceeds the model context",
    "exceeds the context", "context size", "context_length_exceeded",
    "exceeds the maximum allowed number of tokens", "maximum number of tokens",
)
_CONTEXT_STUB = "[tool output omitted from provider request to fit context; original remains in chat history]"
_CONTEXT_PREVIEW = 1500


def _is_context_limit_error(exc: "APIStatusError") -> bool:
    """Recognize a provider's *explicit* context-limit response, not a generic 400."""
    if exc.status_code not in (400, 413, 422):
        return False
    body = getattr(exc, "body", None)
    if not isinstance(body, dict) and getattr(exc, "response", None) is not None:
        try:
            body = exc.response.json()
        except (ValueError, TypeError, AttributeError):
            body = None
    details = body.get("error", body) if isinstance(body, dict) else {}
    if isinstance(details, dict):
        codes = (details.get("code"), details.get("type"),
                 body.get("code") if isinstance(body, dict) else None)
        if any(str(code).lower() in _CONTEXT_ERROR_CODES for code in codes):
            return True
        text = str(details.get("message") or exc.message or "").lower()
    else:
        text = str(details or exc.message or "").lower()
    return any(phrase in text for phrase in _CONTEXT_ERROR_PHRASES)


def _compact_tool_result(messages: list[dict], stage: int) -> tuple[list[dict], int] | None:
    """Shrink tool bodies in a private request copy; never alter the transcript.

    Keep head/tail previews on the first error, then stub previews and other
    large results on subsequent errors. Protect the newest result until other
    candidates are exhausted. Compact enough per retry to avoid many failed
    provider requests for a multi-tool turn.
    """
    candidates = []
    all_tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    newest_tool = all_tool_indices[-1] if all_tool_indices else None
    for idx, msg in enumerate(messages):
        if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
            continue
        text = msg["content"]
        if text.startswith(_CONTEXT_STUB) or len(text) < 256:
            continue
        if text.startswith("Tool execution was declined") or text.startswith("User cancelled"):
            continue
        if stage == 1 and len(text) <= 2 * _CONTEXT_PREVIEW + 200:
            continue
        candidates.append((idx, text))
    if not candidates:
        return None
    pool = [(idx, text) for idx, text in candidates if idx != newest_tool] or candidates
    result = list(messages)
    saved = 0
    for idx, text in pool:
        if stage == 1:
            replacement = (text[:_CONTEXT_PREVIEW] +
                           f"\n\n[... {len(text) - 2 * _CONTEXT_PREVIEW:,} characters omitted from provider request; original remains in chat history ...]\n\n" +
                           text[-_CONTEXT_PREVIEW:])
        else:
            replacement = _CONTEXT_STUB
        reduction = len(text) - len(replacement)
        if reduction > 0:
            result[idx] = {**messages[idx], "content": replacement}
            saved += reduction
    return (result, saved) if saved else None


def _has_image_url_parts(messages: list[dict]) -> bool:
    """True if any message in the list contains image_url parts."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _strip_image_url_parts(messages: list[dict]):
    """Remove image_url parts from all messages in-place."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                p for p in content
                if not (isinstance(p, dict) and p.get("type") == "image_url")
            ]
            # Collapse single remaining text items back to plain string
            if len(msg["content"]) == 1 and msg["content"][0].get("type") == "text":
                msg["content"] = msg["content"][0]["text"]
            elif not msg["content"]:
                msg["content"] = "[Empty — image content was removed]"


_IMAGE_ERROR_KEYWORDS = {"image", "multimodal", "vision", "not support", "unsupported"}


def _is_image_input_error(e: "APIStatusError") -> bool:
    """Check if the API error is about the model not supporting image inputs."""
    err_text = (e.message or "").lower()
    body = getattr(e, "body", None)
    if body and isinstance(body, (dict, list)):
        err_text += " " + str(body).lower()
    return any(kw in err_text for kw in _IMAGE_ERROR_KEYWORDS)

def _retry_after_delay(status_code: int, headers) -> float | None:
    """Extract Retry-After from response headers.

    OpenAI uses ``retry-after-ms`` (integer milliseconds).
    Everyone else uses standard ``retry-after`` (seconds or HTTP-date).
    Anthropic 529 also includes ``retry-after``.
    Returns seconds, or *None* if the header is absent / unparseable.
    """
    # OpenAI-specific: retry-after-ms
    ms = headers.get("retry-after-ms")
    if ms is not None:
        try:
            return float(ms) / 1000.0
        except (ValueError, TypeError):
            pass

    # Standard Retry-After (seconds)
    ra = headers.get("retry-after")
    if ra is not None:
        try:
            return float(ra)
        except (ValueError, TypeError):
            pass

    return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Compute sleep seconds for attempt 0..N-1 with jitter."""
    if retry_after is not None:
        base = min(retry_after, _MAX_DELAY)
    else:
        base = min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY)
    jitter = base * _JITTER * (random.random() * 2.0 - 1.0)
    return max(0.1, base + jitter)


def _sleep_interruptible(seconds: float, is_cancelled: Callable[[], bool] | None):
    """Sleep in 500 ms slices, checking *is_cancelled* each slice."""
    if is_cancelled is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_cancelled():
            raise _Cancelled()
        time.sleep(min(0.5, deadline - time.monotonic()))


class _Cancelled(Exception):
    """Raised inside the generator when the user cancels during a retry sleep."""
    pass


def _get_msg_field(message, field: str):
    if isinstance(message, dict):
        return message.get(field)
    return getattr(message, field, None)


def _serialize_tool_calls(tool_calls):
    serialized = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            serialized.append({
                "id": tc.get("id", ""),
                "type": tc.get("type", "function"),
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                },
            })
        else:
            serialized.append({
                "id": tc.id,
                "type": getattr(tc, "type", "function"),
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            })
    return serialized


def _serialize_assistant_message(message, preserve_reasoning: bool = False) -> dict:
    serialized = {
        "role": "assistant",
        "content": _get_msg_field(message, "content") or "",
    }
    tool_calls = _get_msg_field(message, "tool_calls")
    if tool_calls:
        serialized["tool_calls"] = _serialize_tool_calls(tool_calls)
    if preserve_reasoning:
        for field in _REASONING_MESSAGE_FIELDS:
            value = _get_msg_field(message, field)
            if value is not None:
                serialized[field] = value
    return serialized


def _run_tool(name: str, args: dict, context=None) -> str:
    """Run a tool in a daemon thread with an outer safety-net timeout.

    Subprocess-based tools (_run_bash, _run_python) have their own timeout
    at the process level.  The outer join timeout here catches tools that lack
    an internal deadline (e.g. read_file on a hung NFS mount, fetch_url
    trickle) without requiring every tool to implement its own guard.

    *context* is the per-run :class:`ToolContext` (sudo/subprocess scoping);
    ``None`` uses the module-level default context.
    """
    result: list = [None]
    exc: list = [None]

    def _target():
        try:
            result[0] = _tools_mod.execute_tool(name, args, context)
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    tool_timeout = _tools_mod._tool_timeout
    outer = None if tool_timeout == -1 else tool_timeout + 30
    t.join(timeout=outer)
    if t.is_alive():
        return f"Tool timed out (outer safety net after {outer}s)"
    if exc[0] is not None:
        return f"Tool error: {exc[0]}"
    return result[0]



def _format_question_answers(questions: list[dict], answers: list[str]) -> str:
    """Format question answers for the tool result message."""
    lines = []
    for i, q in enumerate(questions):
        header = q.get("header", f"Q{i + 1}")
        answer = answers[i] if i < len(answers) else "(no answer)"
        # Find the matching option description
        detail = ""
        for opt in q.get("options", []):
            if opt.get("label") == answer:
                detail = f" — {opt.get('description', '')}"
                break
        lines.append(f"**{header}**: {answer}{detail}")
    return "\n".join(lines)


class CredentialError(RuntimeError):
    """The endpoint rejected us for missing or invalid credentials.

    Exists so every frontend (CLI, Web UI, GUI) can show Pengy's own
    configuration instructions instead of the raw SDK text — which tells users to
    set ``OPENAI_API_KEY`` and friends, environment variables Pengy never reads.

    ``kind``/``exit_code`` let callers classify it without string matching.
    """

    kind = "credentials"
    exit_code = 2


class ConfigError(RuntimeError):
    """A turn cannot be attempted until the user chooses something.

    Today that means one case: no model is selected.  Pengy's default endpoint is
    a local server, which ships no model of its own, so the alternative to saying
    this clearly is sending an empty ``model`` to the endpoint and showing the
    user whatever it says about that (Ollama: ``model "" not found``).
    """

    kind = "config"
    exit_code = 1


# Sentinel credentials for endpoints that need none.  The OpenAI SDK refuses to
# construct with an empty key -- "Missing credentials. Please pass an `api_key` …
# or set the `OPENAI_API_KEY` … environment variable" -- which is precisely the
# message that sends a local-server user chasing a variable Pengy never reads.
# Ollama, llama.cpp and vLLM accept any value; a hosted provider still rejects
# this one, and that rejection is translated into Pengy's own instructions by
# :func:`_translate_api_error`, so nothing is masked.
NO_KEY_PLACEHOLDER = "not-needed"

# Hosts that mean "a model server on this machine".
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}


def is_local_endpoint(base_url: str) -> bool:
    """True when *base_url* points at this machine."""
    try:
        host = urlparse(base_url).hostname or ""
    except ValueError:
        return False
    return host.lower() in _LOCAL_HOSTS or host.startswith("127.")


# Phrases the OpenAI SDK (and most OpenAI-compatible servers) use when a request
# cannot be authenticated.  Matched case-insensitively on the message text, in
# addition to status codes and exception class names, because SDK versions differ
# in which exception type they raise.
_CREDENTIAL_PHRASES = (
    "missing credentials",
    "no api key",
    "api key is required",
    "api_key is required",
    "api key must be set",
    "api_key client option must be set",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "invalid authentication",
    "authentication failed",
    "unauthorized",
    "credentials not found",
    "you didn't provide an api key",
)

_CREDENTIAL_TYPE_NAMES = ("AuthenticationError", "PermissionDeniedError")


def _looks_like_credential_problem(exc: BaseException) -> bool:
    """Best-effort detection of an authentication/credential failure."""
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    for klass in type(exc).__mro__:
        if klass.__name__ in _CREDENTIAL_TYPE_NAMES:
            return True
    text = str(exc).lower()
    return any(phrase in text for phrase in _CREDENTIAL_PHRASES)


def no_model_help(base_url: str) -> str:
    """The instructions a user needs when no model is selected.

    A local endpoint ships no model of its own (a fresh Ollama has an empty
    model list), so the useful answer is how to choose one -- not the endpoint's
    complaint about an empty model field.
    """
    return (
        f"No model is selected for {base_url}.\n"
        "\n"
        "Pengy's default endpoint is a local server, which has no model of its own:\n"
        "    pengy-cli /models               list the models this endpoint offers\n"
        "    pengy-cli /model <name>         select one\n"
        "    ollama pull <name>              (Ollama) download one first, if the list is empty\n"
        "  Or open Settings in the GUI / Web UI and use Fetch Models."
    )


def unreachable_help(base_url: str, detail: str = "") -> str:
    """What to say when the endpoint did not answer at all.

    With a local default this is the likeliest first-run failure, and the raw
    transport text ("Connection error.", or a URL and a socket error) does not
    tell a new user that the fix is to start their own server.
    """
    suffix = f" ({detail})" if detail else ""
    if is_local_endpoint(base_url):
        return (
            f"Nothing answered at {base_url}{suffix}.\n"
            "\n"
            "Is your local model server running?\n"
            "    ollama serve                    (Ollama) start the server, then: ollama pull <name>\n"
            "    pengy-cli /models               list the models it offers\n"
            "    pengy-cli /baseurl <url>        point Pengy at a different endpoint\n"
            "    pengy-cli /config               review the current settings"
        )
    return f"Could not reach {base_url}{suffix}. Check the endpoint with pengy-cli /baseurl <url>."


def credential_help(base_url: str) -> str:
    """The instructions a user actually needs when credentials are missing."""
    try:
        from pengy.core.config import get_config_dir

        config_path = get_config_dir() / "settings.json"
    except Exception:  # pragma: no cover - config is always importable
        config_path = "~/.config/pengy/settings.json"

    return (
        f"No API credentials are configured for {base_url}.\n"
        "\n"
        f"Configure Pengy (the CLI, Web UI and GUI all share {config_path}):\n"
        "    pengy-cli /apikey <your-key>    set the API key\n"
        "    pengy-cli /baseurl <url>        change the endpoint "
        "(a local Ollama/vLLM needs no key)\n"
        "    pengy-cli /model <name>         choose a model\n"
        "    pengy-cli /config               review the current settings\n"
        "  Or run pengy-web and open Settings (http://127.0.0.1:5000/settings).\n"
        "\n"
        "Note: Pengy reads credentials from its own settings file. OPENAI_API_KEY\n"
        "and similar environment variables are NOT used, whatever the API error says."
    )


def _translate_api_error(exc: BaseException, base_url: str) -> BaseException:
    """Return a friendlier exception for credential/connection failures, else ``exc``."""
    if isinstance(exc, (CredentialError, ConfigError)):
        return exc
    if isinstance(exc, APIConnectionError):
        # The endpoint never answered. Distinguish "your server is not running"
        # from "the network is down" so a local-first user gets told the former.
        return RuntimeError(unreachable_help(base_url, _connection_detail(exc)))
    if not _looks_like_credential_problem(exc):
        return exc
    return CredentialError(credential_help(base_url))


def _connection_detail(exc: BaseException) -> str:
    """A short human phrase for a connection failure, excluding SDK boilerplate."""
    text = str(exc).strip()
    if not text:
        return "connection failed"
    # "Connection error." is all the SDK gives; anything longer is worth showing.
    return text if len(text) < 200 else text[:197] + "..."


class LLMClient:
    """Client for interacting with OpenAI-compatible LLM APIs."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 llm_timeout: float = 300.0):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.llm_timeout = llm_timeout
        self._client = None

    @property
    def client(self):
        if self._client is None:
            # A local endpoint needs no key, but the SDK refuses to construct
            # with an empty one -- raising "Missing credentials. Please pass an
            # `api_key` … or set the `OPENAI_API_KEY` … environment variable",
            # the exact message that sends a local-server user after a variable
            # Pengy never reads.  A hosted provider still rejects the sentinel,
            # and that rejection becomes Pengy's own instructions (see
            # _translate_api_error), so a real credentials problem is not hidden.
            key = self.api_key or NO_KEY_PLACEHOLDER
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=key,
                timeout=self.llm_timeout,
                max_retries=0,
                default_headers={"api-key": key},
            )
        return self._client

    def _reset_client(self):
        self._client = None

    def chat(self, messages: list[dict], tool_confirmation: str = "none",
             reasoning_effort: str = "", preserve_reasoning: bool = False,
             cancel_fn: Callable[[], bool] | None = None,
             tool_context=None, model: str | None = None):
        """
        Send a chat request and handle tool calls.
        Yields intermediate tool call info for UI updates.

        *tool_confirmation* is one of:
          "all"  – execute every tool without asking (YOLO)
          "safe" – auto-approve read-only tools; confirm write/execute
          "none" – confirm every tool call

        *model*, if given, overrides the client's default model for this
        request (used by per-tab model selection).

        *cancel_fn*, if given, is polled during retry backoff sleeps so the
        user can abort a long wait.  Return ``True`` to cancel.
        """
        current_messages = list(messages)
        # Request-only reductions: tool events and persisted chat retain full output.
        accumulated_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # Checked here rather than in each frontend so the CLI, GUI and Web UI
        # cannot disagree -- and because an empty model would otherwise be sent
        # to the endpoint, whose complaint about it is not an instruction.
        if not (model or self.model).strip():
            raise ConfigError(no_model_help(self.base_url))

        while True:
            # A fresh tool round begins with the full history; the original
            # messages remain untouched for events and history persistence.
            request_messages = list(current_messages)
            context_retries = 0
            rate_retries = 0
            # ── API call with 429 / 529 exponential backoff ──────────
            while True:
                if cancel_fn and cancel_fn():
                    raise _Cancelled()
                try:
                    request_kwargs = {
                        "model": model or self.model,
                        "messages": request_messages,
                        "tools": _tools_mod.TOOLS,
                        "tool_choice": "auto",
                    }
                    if reasoning_effort:
                        request_kwargs["reasoning_effort"] = reasoning_effort
                    response = self.client.chat.completions.create(**request_kwargs)
                    break  # success — exit retry loop
                except APIStatusError as e:
                    # ── Graceful handling: model doesn't support images ──
                    if (e.status_code == 400
                            and not _is_context_limit_error(e)
                            and _has_image_url_parts(current_messages)
                            and _is_image_input_error(e)):
                        # Input history belongs to this generator only; avoid
                        # modifying the caller's image-bearing message objects.
                        current_messages = [dict(msg) for msg in current_messages]
                        _strip_image_url_parts(current_messages)
                        current_messages.append({
                            "role": "user",
                            "content": (
                                "[This AI model does not support image/vision inputs, "
                                "so the image could not be attached. "
                                "The file metadata was returned above.]"
                            ),
                        })
                        self._reset_client()
                        response = _RETRY_WITHOUT_IMAGES
                        break  # exit retry loop

                    if _is_context_limit_error(e):
                        if context_retries < _MAX_CONTEXT_RETRIES:
                            compacted = _compact_tool_result(
                                request_messages, 1 if context_retries == 0 else 2)
                            if compacted is not None:
                                request_messages, saved = compacted
                                context_retries += 1
                                yield {
                                    "type": "context_compacted",
                                    "attempt": context_retries,
                                    "max_attempts": _MAX_CONTEXT_RETRIES,
                                    "chars_removed": saved,
                                }
                                continue
                        raise RuntimeError(
                            "Model context limit reached; could not fit this request after "
                            f"{context_retries} tool-output reductions. The full tool outputs "
                            "remain in chat history. Try a shorter request or a larger-context model."
                        ) from e
                    if e.status_code not in _RETRYABLE_STATUSES or rate_retries >= _MAX_RETRIES:
                        self._reset_client()
                        # 401/403 → tell the user how to configure Pengy instead
                        # of surfacing the SDK's misleading env-var advice.
                        raise _translate_api_error(e, self.base_url) from e
                    # 429 / 529 — backoff and retry
                    headers = getattr(e.response, "headers", {}) if e.response is not None else {}
                    ra = _retry_after_delay(e.status_code, headers)
                    delay = _backoff_delay(rate_retries, ra)
                    rate_retries += 1
                    yield {
                        "type": "retrying",
                        "attempt": rate_retries,
                        "max_attempts": _MAX_RETRIES,
                        "delay_secs": round(delay, 1),
                        "status_code": e.status_code,
                        "message": e.message or str(e),
                    }
                    try:
                        _sleep_interruptible(delay, cancel_fn)
                    except _Cancelled:
                        yield {
                            "type": "final_response",
                            "content": "Request cancelled during backoff.",
                            "message": None,
                            "usage": accumulated_usage,
                        }
                        return
                    self._reset_client()
                except _Cancelled:
                    yield {
                        "type": "final_response", "content": "Request cancelled.",
                        "message": None, "usage": accumulated_usage,
                    }
                    return
                except Exception as exc:
                    self._reset_client()
                    raise _translate_api_error(exc, self.base_url) from exc

            # If images were stripped due to model not supporting vision,
            # restart the outer loop without them.
            if response is _RETRY_WITHOUT_IMAGES:
                continue

            # Accumulate token usage across all calls in this turn
            if response.usage:
                accumulated_usage["prompt_tokens"] += response.usage.prompt_tokens
                accumulated_usage["completion_tokens"] += response.usage.completion_tokens
                accumulated_usage["total_tokens"] += response.usage.total_tokens

            assistant_msg = response.choices[0].message
            serialized = _serialize_assistant_message(assistant_msg, preserve_reasoning)
            current_messages.append(serialized)

            if assistant_msg.tool_calls:
                yield {"type": "assistant_tool_calls", "message": serialized}

                for tool_call in assistant_msg.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    # ask_user_question is a special harness-level tool — it always
                    # pauses for user input regardless of tool_confirmation mode.
                    if tool_name == "ask_user_question":
                        questions = tool_args.get("questions", [])
                        response = yield {
                            "type": "question_request",
                            "name": tool_name,
                            "args": tool_args,
                            "tool_call_id": tool_call.id,
                            "questions": questions,
                        }
                        if response and response.get("answered"):
                            answers = response.get("answers", [])
                            result_text = _format_question_answers(questions, answers)
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result_text,
                            })
                            yield {
                                "type": "question_result",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "content": result_text,
                            }
                        else:
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "User cancelled the question.",
                            })
                            yield {
                                "type": "tool_result",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "args": tool_args,
                                "content": "User cancelled the question.",
                                "declined": True,
                            }
                        continue

                    # Auto-approve based on tool_confirmation mode
                    skip_confirm = (
                        tool_confirmation == "all"
                        or (tool_confirmation == "safe" and _tools_mod.is_readonly_tool(tool_name))
                    )

                    if skip_confirm:
                        yield {"type": "tool_request", "name": tool_name,
                               "args": tool_args, "tool_call_id": tool_call.id}
                        result = _run_tool(tool_name, tool_args, tool_context)
                        current_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        })
                        yield {"type": "tool_result", "tool_call_id": tool_call.id,
                               "name": tool_name, "args": tool_args,
                               "content": result, "declined": False}
                    else:
                        confirm = yield {"type": "tool_request", "name": tool_name,
                                         "args": tool_args, "tool_call_id": tool_call.id}
                        if confirm and confirm.get("confirmed"):
                            result = _run_tool(tool_name, tool_args, tool_context)
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result,
                            })
                            yield {"type": "tool_result", "tool_call_id": tool_call.id,
                                   "name": tool_name, "args": tool_args,
                                   "content": result, "declined": False}
                        else:
                            declined_msg = "Tool execution was declined by user."
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": declined_msg,
                            })
                            yield {"type": "tool_result", "tool_call_id": tool_call.id,
                                   "name": tool_name, "args": tool_args,
                                   "content": declined_msg, "declined": True}

                # read_image parks its picture on the tool context because a
                # role:"tool" message only accepts string content on
                # OpenAI-compatible APIs.  Attach anything queued as a follow-up
                # user message — after the loop, so every tool_call keeps its
                # matching tool message immediately behind the assistant one.
                pending_images = _tools_mod.take_pending_images(tool_context)
                if pending_images:
                    parts = []
                    for image in pending_images:
                        parts.append({
                            "type": "text",
                            "text": f"Image loaded by read_image: {image['path']}",
                        })
                        parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image['mime']};base64,{image['b64']}",
                            },
                        })
                    current_messages.append({"role": "user", "content": parts})
            else:
                yield {"type": "final_response", "content": assistant_msg.content,
                       "message": _serialize_assistant_message(assistant_msg, preserve_reasoning),
                       "usage": accumulated_usage}
                break
