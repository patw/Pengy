"""LLM client for OpenAI-compatible APIs."""
import json
import threading
from openai import OpenAI

from pengy.core.tools import TOOLS, execute_tool

_TOOL_TIMEOUT = 60


def _run_tool(name: str, args: dict) -> str:
    """Run a tool in a daemon thread with a hard timeout.

    Using a daemon thread (rather than ThreadPoolExecutor) means join() returns
    immediately after the timeout without blocking on executor shutdown.
    """
    result: list = [None]
    exc: list = [None]

    def _target():
        try:
            result[0] = execute_tool(name, args)
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(_TOOL_TIMEOUT)
    if t.is_alive():
        return (
            f"Tool '{name}' timed out after {_TOOL_TIMEOUT} seconds. "
            "Please try again or use a different approach."
        )
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
            )
        return self._client

    def chat(self, messages: list[dict], yolo_mode: bool = False):
        """
        Send a chat request and handle tool calls.
        Returns the final assistant message content.
        Yields intermediate tool call info for UI updates.
        """
        current_messages = list(messages)

        while True:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=current_messages,
                tools=TOOLS,
                tool_choice="auto",
            )

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

                    if yolo_mode:
                        yield {"type": "tool_request", "name": tool_name, "args": tool_args, "tool_call_id": tool_call.id}
                        result = _run_tool(tool_name, tool_args)
                        current_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        })
                        yield {"type": "tool_result", "tool_call_id": tool_call.id, "name": tool_name, "args": tool_args, "content": result, "declined": False}
                    else:
                        confirm = yield {"type": "tool_request", "name": tool_name, "args": tool_args, "tool_call_id": tool_call.id}
                        if confirm and confirm.get("confirmed"):
                            result = _run_tool(tool_name, tool_args)
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result,
                            })
                            yield {"type": "tool_result", "tool_call_id": tool_call.id, "name": tool_name, "args": tool_args, "content": result, "declined": False}
                        else:
                            declined_msg = "Tool execution was declined by user."
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": declined_msg,
                            })
                            yield {"type": "tool_result", "tool_call_id": tool_call.id, "name": tool_name, "args": tool_args, "content": declined_msg, "declined": True}
            else:
                yield {"type": "final_response", "content": assistant_msg.content}
                break
