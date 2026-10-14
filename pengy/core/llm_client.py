"""LLM client for OpenAI-compatible APIs."""
import json
import math
import random
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

from openai import APITimeoutError, APIConnectionError, APIStatusError, OpenAI

from pengy.core import tools as _tools_mod
from pengy.core.context_recovery import Recovery, SUMMARY_PROMPT
from pengy.core.tool_args import parse_raw_arguments

_REASONING_MESSAGE_FIELDS = (
    "reasoning_content",
    "reasoning",
    "reasoning_details",
)

def _response_tokens_per_second(usage, seconds):
    """Final response output / successful request time, not cumulative usage."""
    tokens = getattr(usage, "completion_tokens", None)
    if not isinstance(tokens, (int, float)) or tokens < 0 or not math.isfinite(seconds) or seconds <= 0:
        return None
    rate = tokens / seconds
    return rate if math.isfinite(rate) else None


# ── 429 / 529 backoff ────────────────────────────────────────────
_MAX_RETRIES = 5
_BASE_DELAY = 1.0          # seconds
_MAX_DELAY = 60.0          # cap
_JITTER = 0.25             # ±25 %
_RETRYABLE_STATUSES = {429, 529}

# ── Provider / transport timeouts ────────────────────────────────
# A timeout is the one failure that usually means "the endpoint was busy", not
# "the request was wrong", so it gets a retry budget of its own.  The budget is
# deliberately smaller than _MAX_RETRIES: Pengy asks for the whole reply in one
# non-streaming request, so llm_timeout is total wall clock per attempt, and five
# retries at the 300 s default would hang a turn for ~25 minutes before the user
# saw the timeout message.  Cancel is polled during every backoff sleep.
_MAX_TIMEOUT_RETRIES = 3
# 408 Request Timeout, and 504 Gateway Timeout -- a proxy answering for an
# upstream that ran out of time, which is the same failure seen from the client.
_TIMEOUT_STATUSES = {408, 504}

# ── Transient server errors, and routes that drop mid-task ───────
# 502 and 503 say "the proxy or upstream is not available right now" -- a
# restart, a rolling deploy, a worker crash.  That is the same busy-endpoint
# failure a timeout is, and a ten-second blip used to kill a multi-hour task.
# 500 is often *deterministic* (our request trips a provider bug), so it gets
# one retry rather than three: cheap insurance, no long chain.
_TRANSIENT_STATUSES = {502, 503}
_MAX_TRANSIENT_RETRIES = 3
_MAX_INTERNAL_RETRIES = 1
# A connection failure *after* this endpoint has answered in this session is a
# dropped route -- server restart, OOM kill, sleep/wake, a VM's address changing
# -- not a misconfigured endpoint.  First contact keeps failing fast: that is
# the case the "is your server running?" help is written for, and a wrong URL
# should not cost three waits.
_MAX_UNREACHABLE_RETRIES = 3

# Only context errors get a size-reduction retry; generic bad requests do not.
_MAX_CONTEXT_RETRIES = 4
_CONTEXT_ERROR_CODES = {
    "context_length_exceeded", "context_window_exceeded", "prompt_too_long",
    "input_too_long", "max_context_length_exceeded", "token_limit_exceeded",
}
# A length stop this short is context starvation, not a plausible output cap;
# larger (or unreported) completions keep the safe truncation failure.
_SHORT_LENGTH_COMPLETION_TOKENS = 1024
_CONTEXT_ERROR_PHRASES = (
    "context length", "context window", "context limit", "maximum context",
    "prompt too long", "input too long", "too many tokens", "token limit exceeded",
    "exceeds the model's context", "exceeds the model context",
    "exceeds the context", "context size", "context_length_exceeded",
    "exceeds the maximum allowed number of tokens", "maximum number of tokens",
    "leaves no room to answer in the context",
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


_LEGACY_TEXT_ONLY_ERROR = "Only text content parts are supported by this upstream format"
_UNSUPPORTED_IMAGE_PHRASES = (
    "does not support image", "doesn't support image", "do not support image",
    "does not support vision", "does not support multimodal",
    "image inputs are not supported", "image input is not supported",
    "images are not supported", "image_url is not supported",
    "unsupported image input", "unsupported vision input",
)


def _api_error_details(exc: "APIStatusError") -> tuple[dict, str]:
    """Handle both full HTTP bodies and the SDK's unwrapped error object."""
    body = getattr(exc, "body", None)
    if body is None and getattr(exc, "response", None) is not None:
        try:
            body = exc.response.json()
        except (ValueError, TypeError, AttributeError):
            pass
    detail = body.get("error", body) if isinstance(body, dict) else body
    fields = detail if isinstance(detail, dict) else {}
    text = fields.get("message") or (detail if isinstance(detail, str) else None)
    return fields, str(text or exc.message or "")


def _is_image_input_error(exc: "APIStatusError") -> bool:
    """Recover only explicit unsupported image input, not arbitrary image errors."""
    if exc.status_code != 400:
        return False
    fields, text = _api_error_details(exc)
    if fields.get("source") == "openai-proxy":
        return (fields.get("code") == "unsupported_content_type"
                and fields.get("content_type") == "image_url")
    lower = text.lower().rstrip(".")
    if lower == _LEGACY_TEXT_ONLY_ERROR.lower():
        return True
    return (any(phrase in lower for phrase in _UNSUPPORTED_IMAGE_PHRASES)
            or (("text-only" in lower or "only text" in lower)
                and any(word in lower for word in ("image", "vision", "multimodal"))))


def _image_rejection_notice(exc: "APIStatusError") -> str:
    fields, text = _api_error_details(exc)
    if fields.get("source") == "openai-proxy" or text.rstrip(".").lower() == _LEGACY_TEXT_ONLY_ERROR.lower():
        return (
            "[The proxy adapter cannot translate image inputs for this route. "
            "Images were omitted from this request; only file metadata is available. "
            "This is not evidence that the underlying model lacks vision. "
            "Do not claim to have inspected the images.]"
        )
    return (
        "[The API endpoint rejected image/vision inputs as unsupported. "
        "Images were omitted from this request; only file metadata is available. "
        "Do not claim to have inspected the images.]"
    )

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


def _is_timeout_error(exc: BaseException) -> bool:
    """Whether a failure means "the endpoint did not answer in time".

    Two shapes reach Pengy: the SDK's own deadline (``APITimeoutError``, which
    carries no HTTP status at all) and a provider that answers in time with a
    timeout status (408, or 504 from a proxy).  Both are worth re-sending.

    A plain ``APIConnectionError`` -- connection refused, DNS, TLS -- is
    deliberately *not* a timeout: the endpoint is not there, and retrying it
    only delays the "is your server running?" help Pengy already has for that.
    """
    if isinstance(exc, APITimeoutError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _TIMEOUT_STATUSES
    return False


def _retry_delay(attempt: int, exc: BaseException) -> float:
    """Backoff before a transient retry.

    A provider answer may carry Retry-After; the SDK's own deadline and a
    connection failure have no response object, so they fall back to the plain
    exponential ladder.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    return _backoff_delay(attempt, _retry_after_delay(getattr(exc, "status_code", 0), headers))


def _transient_budget(status_code: int) -> int | None:
    """Retry budget for a provider status meaning "not available right now".

    ``None`` means the status is not transient: a 400/401/404 is the request's
    fault and must fail immediately, with no waits in the way of the message
    that explains it.
    """
    if status_code in _TIMEOUT_STATUSES:
        return _MAX_TIMEOUT_RETRIES
    if status_code in _TRANSIENT_STATUSES:
        return _MAX_TRANSIENT_RETRIES
    if status_code == 500:
        return _MAX_INTERNAL_RETRIES
    return None


def _is_dropped_route(exc: BaseException) -> bool:
    """A connection failure that is worth re-sending *mid-session*.

    Refused / DNS / TLS failures are instant, so retrying them costs seconds,
    not ``llm_timeout``.  The gate is the endpoint having answered at least
    once, which is what separates "my server restarted" from "my URL is wrong".
    """
    return isinstance(exc, APIConnectionError) and not isinstance(exc, APITimeoutError)


def _chain_failure(chain_error: BaseException | None, exc: BaseException) -> BaseException:
    """Which error to report when a retry chain ends in failure.

    A transport failure carries no provider message, so reporting it after a
    chain that started with "HTTP 500: boom" would *lose* the diagnosis -- the
    retry would change what the user is told about a failure they care about.
    When the chain has a status-bearing error, that is the one to report; a
    later failure that is itself specific (a 401 mid-chain, say) still wins.
    """
    if chain_error is not None and getattr(exc, "status_code", None) is None:
        return chain_error
    return exc


def _transient_retry(exc: BaseException, endpoint_answered: bool) -> tuple[int, str] | None:
    """Decide whether to re-send a failed request: ``(budget, reason)`` or None.

    One classification point for every shape, so the turn request and the
    context-summary request cannot drift apart:

    - the SDK's deadline, 408, 504  -> ``"timeout"``
    - 502, 503                      -> ``"server"``
    - 500                           -> ``"server"``, one attempt only
    - refused / DNS / TLS           -> ``"unreachable"``, and only once this
      endpoint has answered in this session
    - anything else (400, 401, 404, context limits, ...) -> ``None``: the
      request is the problem, and no amount of waiting will make the
      explanatory message arrive faster.
    """
    if _is_timeout_error(exc):
        return _MAX_TIMEOUT_RETRIES, "timeout"
    if isinstance(exc, APIStatusError):
        budget = _transient_budget(exc.status_code)
        if not budget:
            return None
        return budget, ("timeout" if exc.status_code in _TIMEOUT_STATUSES else "server")
    if _is_dropped_route(exc):
        return (_MAX_UNREACHABLE_RETRIES, "unreachable") if endpoint_answered else None
    return None


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


def _is_proxy_reasoning(details) -> bool:
    return (isinstance(details, dict)
            and details.get("format") == "openai-proxy/reasoning-v1")


def _without_cross_model_proxy_state(messages: list[dict], model: str) -> list[dict]:
    """Filter request copies only; keep the opaque state in persisted history."""
    result = []
    for message in messages:
        copy = dict(message)
        details = copy.get("reasoning_details")
        if _is_proxy_reasoning(details) and details.get("proxy_model") != model:
            copy.pop("reasoning_details", None)
        result.append(copy)
    return result


def _serialize_assistant_message(message, preserve_reasoning: bool = False) -> dict:
    serialized = {
        "role": "assistant",
        "content": _get_msg_field(message, "content") or "",
    }
    tool_calls = _get_msg_field(message, "tool_calls")
    if tool_calls:
        serialized["tool_calls"] = _serialize_tool_calls(tool_calls)
    for field in _REASONING_MESSAGE_FIELDS:
        value = _get_msg_field(message, field)
        # Tagged proxy state is required for continuation, not a UI preference.
        if value is not None and (preserve_reasoning or (
                field == "reasoning_details" and _is_proxy_reasoning(value))):
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



# Sent back for ask_user_question when the frontend has no user to ask
# (CLI with no TTY): unlike a cancel, the model should carry on.  Identical in
# the Rust and C++ editions.
NO_USER_ANSWER = (
    "No user is available to answer (non-interactive run). If what you found makes one "
    "option clearly the most reasonable, take it and state that assumption in your reply. "
    "If it would be a pure guess, leave that part undone and say what you need."
)


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


def _completion_tokens(usage) -> int | None:
    value = getattr(usage, "completion_tokens", None) if usage else None
    return value if isinstance(value, int) and value >= 0 else None


def _length_suggests_context_pressure(choice, usage, output_token_limit: int) -> bool:
    """Whether a length stop is worth a context-reduction retry.

    An empty answer always qualifies. A partial answer or truncated tool call
    qualifies only when the provider reports a completion too short to be an
    output cap -- typically a lead-in like "Redoing it properly:" cut off just
    before its tool call because the window was nearly full.
    """
    if choice.finish_reason != "length":
        return False
    if not choice.message.tool_calls and not (choice.message.content or "").strip():
        return True
    completion = _completion_tokens(usage)
    limit = _SHORT_LENGTH_COMPLETION_TOKENS
    if output_token_limit > 0:
        limit = min(limit, output_token_limit)
    return completion is not None and completion < limit


def _generation_limit_message(content: str | None, has_tool_calls: bool,
                              usage=None, recovery_attempts: int = 0) -> str:
    """Length means generation exhaustion, not necessarily a full context window."""
    if has_tool_calls:
        detail = "Generation limit reached during tool calls; no tools from this response were executed."
    elif not (content or "").strip():
        detail = "Generation limit reached before an answer was produced."
    else:
        detail = "Generation limit reached; the answer is incomplete."
    prompt = getattr(usage, "prompt_tokens", None) if usage else None
    completion = _completion_tokens(usage)
    counts = ""
    if isinstance(prompt, int) and completion is not None:
        counts = f" (prompt {prompt:,} tokens, completion {completion:,} tokens)"
    retried = (f"Pengy retried after {recovery_attempts} context reduction(s) without success."
               if recovery_attempts else "Pengy did not retry automatically.")
    message = (
        f"{detail} The provider reported finish_reason=length{counts}. "
        "This can mean an output-token cap or insufficient remaining context. "
        "Try a shorter conversation, a larger output allowance, or a reasoning budget "
        f"that leaves room for an answer. {retried}"
    )
    if (content or "").strip():
        message += f"\n\nPartial response (incomplete, not saved as an answer):\n{content}"
    return message


class GenerationLimitError(RuntimeError):
    """A truncated completion is a failed turn, never a completed answer."""

    kind = "truncated"
    exit_code = 1


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
        # Set once this endpoint has answered a request.  It is what lets a
        # connection failure be read as "my server restarted" (retry it) rather
        # than "my URL is wrong" (say so immediately), and it survives
        # _reset_client() because that only rebuilds the HTTP client.
        self._endpoint_answered = False

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

    def _pause_after_transient(self, attempt: int, max_attempts: int, reason: str,
                               exc: BaseException,
                               cancel_fn: Callable[[], bool] | None,
                               accumulated_usage: dict):
        """Announce one transient-failure retry, sleep out, reset the pooled client.

        *attempt* is the number of retries already spent (0-based), so the ladder
        matches the 429/529 path: 1s, 2s, 4s.  *reason* is what the frontends
        label ("timeout", "server", "unreachable").  Yields the ``retrying``
        event, then the cancellation notice when the user cancels during the
        sleep; returns whether the caller should re-send the request.  Shared by
        the turn request and the context-summary request so every frontend sees
        one shape of event.
        """
        delay = _retry_delay(attempt, exc)
        message = exc.message if isinstance(exc, APIStatusError) else str(exc)
        yield {
            "type": "retrying",
            "reason": reason,
            "attempt": attempt + 1,
            "max_attempts": max_attempts,
            "delay_secs": round(delay, 1),
            "status_code": getattr(exc, "status_code", 0),
            "message": message or "Request failed",
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
            return False
        self._reset_client()
        return True

    def chat(self, messages: list[dict], tool_confirmation: str = "none",
             reasoning_effort: str = "", preserve_reasoning: bool = False,
             cancel_fn: Callable[[], bool] | None = None,
             tool_context=None, model: str | None = None, *, chat_id: str = "",
             auto_context_recovery: bool = True, recovery_keep_turns: int = 3,
             output_token_limit: int = 0, output_token_parameter: str = "max_tokens"):
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
        # Once this endpoint rejects image input, don't reattach the same
        # images on subsequent tool rounds in this turn.
        image_input_rejected = False
        image_rejection = None

        # Checked here rather than in each frontend so the CLI, GUI and Web UI
        # cannot disagree -- and because an empty model would otherwise be sent
        # to the endpoint, whose complaint about it is not an instruction.
        if not (model or self.model).strip():
            raise ConfigError(no_model_help(self.base_url))

        recovery = Recovery(current_messages, self.base_url, model or self.model,
                            chat_id=chat_id, enabled=auto_context_recovery,
                            keep_turns=recovery_keep_turns)
        output_token_parameter = output_token_parameter if isinstance(output_token_parameter, str) else ""
        if output_token_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ConfigError("output_token_parameter must be max_tokens or max_completion_tokens")

        def summarize(source):
            if cancel_fn and cancel_fn(): raise _Cancelled()
            summary_kwargs = {
                "model": model or self.model,
                "messages": [{"role": "system", "content": SUMMARY_PROMPT},
                             {"role": "user", "content": source}],
                output_token_parameter: 2048,
            }
            summary_retries = 0
            summary_chain = None
            # The summary is part of this turn, so a provider timeout gets the
            # same retry budget here as the turn request itself -- a big
            # conversation's prefill is exactly what blows llm_timeout on a slow
            # engine, and losing the turn there is the worst place to lose it.
            # No events are emitted: Recovery calls summarize() synchronously and
            # cannot carry a yield.
            while True:
                try:
                    reply = self.client.chat.completions.create(**summary_kwargs)
                    self._endpoint_answered = True
                    break
                except (APITimeoutError, APIConnectionError, APIStatusError) as exc:
                    transient = _transient_retry(exc, self._endpoint_answered)
                    if transient is None or summary_retries >= transient[0]:
                        reported = _chain_failure(summary_chain, exc)
                        if isinstance(reported, APIStatusError):
                            raise RuntimeError("Context summary request failed; original history retained. " + str(reported)) from reported
                        raise _translate_api_error(reported, self.base_url) from reported
                    summary_retries += 1
                    summary_chain = summary_chain or exc
                    _sleep_interruptible(_retry_delay(summary_retries - 1, exc), cancel_fn)
                    self._reset_client()
            if reply.usage:
                for key in accumulated_usage:
                    accumulated_usage[key] += getattr(reply.usage, key, 0) or 0
            if cancel_fn and cancel_fn(): raise _Cancelled()
            choice = reply.choices[0]
            if choice.finish_reason != "stop" or choice.message.tool_calls or not (choice.message.content or "").strip():
                raise RuntimeError("Context summary was incomplete; original history retained. Try a new chat or adjust the provider's reasoning/output budget.")
            return choice.message.content

        while True:
            # A validated provider-only reduction persists across tool rounds.
            request_messages = _without_cross_model_proxy_state(
                recovery.apply(current_messages), model or self.model)
            if image_input_rejected:
                request_messages = [dict(msg) for msg in request_messages]
                _strip_image_url_parts(request_messages)
                request_messages.append({"role": "user", "content": image_rejection})
            context_retries = 0
            rate_retries = 0
            transient_retries = 0
            # The failure that started this retry chain.  Reported when a retry
            # itself fails with nothing more informative, so the retries never
            # change what the user is told about the failure they care about.
            chain_error = None
            # ── API call with backoff: 429/529, timeouts, 5xx, dropped routes ──
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
                    if output_token_limit > 0:
                        request_kwargs[output_token_parameter] = output_token_limit
                    request_started = time.perf_counter()
                    response = self.client.chat.completions.create(**request_kwargs)
                    # The endpoint is reachable and speaking our protocol. From
                    # here on, a connection failure means it went away mid-work.
                    self._endpoint_answered = True
                    request_seconds = time.perf_counter() - request_started
                    choice = response.choices[0]
                    if _length_suggests_context_pressure(choice, response.usage, output_token_limit):
                        if response.usage:
                            for key in accumulated_usage:
                                accumulated_usage[key] += getattr(response.usage, key, 0) or 0
                        # The truncated reply is discarded unexecuted; a retry
                        # regenerates it against a smaller provider view.
                        event = recovery.reduce(current_messages, summarize)
                        if event:
                            yield event
                            request_messages = recovery.apply(current_messages)
                            if image_input_rejected:
                                _strip_image_url_parts(request_messages)
                                request_messages.append({"role": "user", "content": image_rejection})
                            request_messages = _without_cross_model_proxy_state(request_messages, model or self.model)
                            continue
                        raise GenerationLimitError(_generation_limit_message(
                            choice.message.content, bool(choice.message.tool_calls),
                            response.usage, recovery.attempts))
                    break  # success — exit retry loop
                except APIStatusError as e:
                    # Image recovery edits this provider request only; stored
                    # history and tool/reasoning continuation state stay intact.
                    if (not _is_context_limit_error(e)
                            and _has_image_url_parts(request_messages)
                            and _is_image_input_error(e)):
                        image_input_rejected = True
                        image_rejection = _image_rejection_notice(e)
                        request_messages = [dict(msg) for msg in request_messages]
                        _strip_image_url_parts(request_messages)
                        request_messages.append({
                            "role": "user", "content": image_rejection,
                        })
                        continue  # retry once; no image parts remain in this request

                    if _is_context_limit_error(e):
                        if auto_context_recovery:
                            event = recovery.reduce(current_messages, summarize)
                            if event:
                                yield event
                                request_messages = recovery.apply(current_messages)
                                if image_input_rejected:
                                    _strip_image_url_parts(request_messages)
                                    request_messages.append({"role": "user", "content": image_rejection})
                                request_messages = _without_cross_model_proxy_state(request_messages, model or self.model)
                                continue
                            raise RuntimeError("Model context limit reached; could not fit the protected task after bounded recovery. Full history retained; try a shorter request or a new chat.") from e
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
                    transient = _transient_retry(e, self._endpoint_answered)
                    if transient is not None:
                        # The endpoint answered, saying it is not available right
                        # now: 408/504 (out of time), 502/503 (proxy/upstream
                        # gone), or 500 (one attempt, since a 500 is often the
                        # request's fault).  Same ladder as 429/529.
                        budget, reason = transient
                        if transient_retries >= budget:
                            self._reset_client()
                            raise _translate_api_error(_chain_failure(chain_error, e),
                                                       self.base_url) from e
                        chain_error = chain_error or e
                        if not (yield from self._pause_after_transient(
                                transient_retries, budget, reason, e,
                                cancel_fn, accumulated_usage)):
                            return
                        transient_retries += 1
                        continue
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
                except APITimeoutError as e:
                    # The SDK's own deadline (llm_timeout) elapsed with no answer
                    # at all.  This is the failure a retry actually fixes -- a busy
                    # or single-slot endpoint usually answers on the next attempt.
                    # Once the budget is spent the user gets the ordinary timeout
                    # message, unchanged.
                    budget, reason = _transient_retry(e, self._endpoint_answered)
                    if transient_retries >= budget:
                        self._reset_client()
                        raise _translate_api_error(_chain_failure(chain_error, e),
                                                   self.base_url) from e
                    chain_error = chain_error or e
                    if not (yield from self._pause_after_transient(
                            transient_retries, budget, reason, e,
                            cancel_fn, accumulated_usage)):
                        return
                    transient_retries += 1
                    continue
                except APIConnectionError as e:
                    # Refused / DNS / TLS.  Gated on the endpoint having answered
                    # before: mid-task that means a restart or a dropped route and
                    # is worth seconds, not a verdict.  On first contact it means
                    # the endpoint is not there at all, and the "is your server
                    # running?" help must arrive immediately, not after three
                    # waits.  These failures are instant, so the retries are cheap.
                    transient = _transient_retry(e, self._endpoint_answered)
                    if transient is None:
                        self._reset_client()
                        raise _translate_api_error(_chain_failure(chain_error, e),
                                                   self.base_url) from e
                    budget, reason = transient
                    if transient_retries >= budget:
                        self._reset_client()
                        raise _translate_api_error(_chain_failure(chain_error, e),
                                                   self.base_url) from e
                    chain_error = chain_error or e
                    if not (yield from self._pause_after_transient(
                            transient_retries, budget, reason, e,
                            cancel_fn, accumulated_usage)):
                        return
                    transient_retries += 1
                    continue
                except _Cancelled:
                    yield {
                        "type": "final_response", "content": "Request cancelled.",
                        "message": None, "usage": accumulated_usage,
                    }
                    return
                except Exception as exc:
                    self._reset_client()
                    raise _translate_api_error(_chain_failure(chain_error, exc),
                                               self.base_url) from exc

            # Accumulate token usage across all calls in this turn
            if response.usage:
                accumulated_usage["prompt_tokens"] += response.usage.prompt_tokens
                accumulated_usage["completion_tokens"] += response.usage.completion_tokens
                accumulated_usage["total_tokens"] += response.usage.total_tokens

            choice = response.choices[0]
            assistant_msg = choice.message
            # Check before history/events/tools: even valid-looking arguments
            # may belong to an incomplete tool-call sequence. Do not retry a
            # generation cap as though it proved context overflow.
            if choice.finish_reason == "length":
                raise GenerationLimitError(_generation_limit_message(
                    assistant_msg.content, bool(assistant_msg.tool_calls),
                    response.usage, recovery.attempts))
            serialized = _serialize_assistant_message(assistant_msg, preserve_reasoning)
            current_messages.append(serialized)

            if assistant_msg.tool_calls:
                yield {"type": "assistant_tool_calls", "message": serialized}

                for tool_call in assistant_msg.tool_calls:
                    tool_name = tool_call.function.name
                    # Malformed JSON is kept (not replaced by {}) so the tool
                    # layer can tell the model its call didn't parse.
                    tool_args = parse_raw_arguments(tool_call.function.arguments)

                    # ask_user_question is a special harness-level tool — it always
                    # pauses for user input regardless of tool_confirmation mode.
                    if tool_name == "ask_user_question":
                        tool_args, arg_error = _tools_mod.validate_question_arguments(tool_args)
                        if arg_error:
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": arg_error,
                            })
                            yield {
                                "type": "tool_result",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "args": tool_args,
                                "content": arg_error,
                                "declined": False,
                            }
                            continue
                        questions = tool_args["questions"]
                        response = yield {
                            "type": "question_request",
                            "name": tool_name,
                            "args": tool_args,
                            "tool_call_id": tool_call.id,
                            "questions": questions,
                            # Running turn usage so the UI can tick the token
                            # count before the turn ends (see final_response).
                            # Copied: the accumulator is mutated in place on the
                            # next round, so a retained reference would drift.
                            "usage": dict(accumulated_usage),
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
                            # A headless run (no one to ask) is not a refusal:
                            # tell the model to decide and say what it assumed.
                            unanswered = (NO_USER_ANSWER if response and response.get("unavailable")
                                          else "User cancelled the question.")
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": unanswered,
                            })
                            yield {
                                "type": "tool_result",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "args": tool_args,
                                "content": unanswered,
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
                               "args": tool_args, "tool_call_id": tool_call.id,
                               "usage": dict(accumulated_usage)}
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
                                         "args": tool_args, "tool_call_id": tool_call.id,
                                         "usage": dict(accumulated_usage)}
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
                       "usage": accumulated_usage,
                       "tokens_per_second": _response_tokens_per_second(response.usage, request_seconds)}
                break
