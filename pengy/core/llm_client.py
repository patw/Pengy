"""LLM client for OpenAI-compatible APIs."""
import json
import threading
from openai import OpenAI

from pengy.core import tools as _tools_mod


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

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=300.0,
                max_retries=0,
                default_headers={"api-key": self.api_key},
            )
        return self._client

    def _reset_client(self):
        self._client = None

    def chat(self, messages: list[dict], tool_confirmation: str = "none"):
        """
        Send a chat request and handle tool calls.
        Returns the final assistant message content.
        Yields intermediate tool call info for UI updates.

        *tool_confirmation* is one of:
          "all"  – execute every tool without asking (YOLO)
          "safe" – auto-approve read-only tools; confirm write/execute
          "none" – confirm every tool call
        """
        current_messages = list(messages)
        accumulated_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        while True:
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=current_messages,
                    tools=_tools_mod.TOOLS,
                    tool_choice="auto",
                )
            except Exception:
                self._reset_client()
                raise

            # Accumulate token usage across all calls in this turn
            if response.usage:
                accumulated_usage["prompt_tokens"] += response.usage.prompt_tokens
                accumulated_usage["completion_tokens"] += response.usage.completion_tokens
                accumulated_usage["total_tokens"] += response.usage.total_tokens

            assistant_msg = response.choices[0].message
            current_messages.append(assistant_msg)

            if assistant_msg.tool_calls:
                serialized_assistant = {
                    "role": "assistant",
                    "content": assistant_msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in assistant_msg.tool_calls
                    ],
                }
                yield {"type": "assistant_tool_calls", "message": serialized_assistant}

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
                       "usage": accumulated_usage}
                break
