"""LLM client for OpenAI-compatible APIs."""
import json
import random
import threading
import time
from collections.abc import Callable
from openai import APIStatusError, OpenAI

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


def _run_tool(name: str, args: dict) -> str:
    """Run a tool in a daemon thread with an outer safety-net timeout.

    Subprocess-based tools (_run_bash, _run_python) have their own timeout
    at the process level.  The outer join timeout here catches tools that lack
    an internal deadline (e.g. read_file on a hung NFS mount, fetch_url
    trickle) without requiring every tool to implement its own guard.
    """
    result: list = [None]
    exc: list = [None]

    def _target():
        try:
            result[0] = _tools_mod.execute_tool(name, args)
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
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.llm_timeout,
                max_retries=0,
                default_headers={"api-key": self.api_key},
            )
        return self._client

    def _reset_client(self):
        self._client = None

    def chat(self, messages: list[dict], tool_confirmation: str = "none",
             reasoning_effort: str = "", preserve_reasoning: bool = False,
             cancel_fn: Callable[[], bool] | None = None):
        """
        Send a chat request and handle tool calls.
        Yields intermediate tool call info for UI updates.

        *tool_confirmation* is one of:
          "all"  – execute every tool without asking (YOLO)
          "safe" – auto-approve read-only tools; confirm write/execute
          "none" – confirm every tool call

        *cancel_fn*, if given, is polled during retry backoff sleeps so the
        user can abort a long wait.  Return ``True`` to cancel.
        """
        current_messages = list(messages)
        accumulated_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        while True:
            # ── API call with 429 / 529 exponential backoff ──────────
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    request_kwargs = {
                        "model": self.model,
                        "messages": current_messages,
                        "tools": _tools_mod.TOOLS,
                        "tool_choice": "auto",
                    }
                    if reasoning_effort:
                        request_kwargs["reasoning_effort"] = reasoning_effort
                    response = self.client.chat.completions.create(**request_kwargs)
                    break  # success — exit retry loop
                except APIStatusError as e:
                    if e.status_code not in _RETRYABLE_STATUSES or attempt >= _MAX_RETRIES:
                        self._reset_client()
                        raise
                    # 429 / 529 — backoff and retry
                    headers = getattr(e.response, "headers", {}) if e.response is not None else {}
                    ra = _retry_after_delay(e.status_code, headers)
                    delay = _backoff_delay(attempt, ra)
                    yield {
                        "type": "retrying",
                        "attempt": attempt + 1,
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
                except Exception:
                    self._reset_client()
                    raise

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

                    # Auto-approve based on tool_confirmation mode
                    skip_confirm = (
                        tool_confirmation == "all"
                        or (tool_confirmation == "safe" and _tools_mod.is_readonly_tool(tool_name))
                    )

                    if skip_confirm:
                        yield {"type": "tool_request", "name": tool_name,
                               "args": tool_args, "tool_call_id": tool_call.id}
                        result = _run_tool(tool_name, tool_args)
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
                            result = _run_tool(tool_name, tool_args)
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
            else:
                yield {"type": "final_response", "content": assistant_msg.content,
                       "message": _serialize_assistant_message(assistant_msg, preserve_reasoning),
                       "usage": accumulated_usage}
                break
