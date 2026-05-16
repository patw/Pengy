"""LLM client for OpenAI-compatible APIs."""
import json
from openai import OpenAI

from pengy.core.tools import TOOLS, execute_tool


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
                for tool_call in assistant_msg.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    if yolo_mode:
                        yield {"type": "tool_request", "name": tool_name, "args": tool_args, "tool_call_id": tool_call.id}
                        result = execute_tool(tool_name, tool_args)
                        current_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        })
                    else:
                        confirm = yield {"type": "tool_request", "name": tool_name, "args": tool_args, "tool_call_id": tool_call.id}
                        if confirm and confirm.get("confirmed"):
                            result = execute_tool(tool_name, tool_args)
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result,
                            })
                        else:
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "Tool execution was declined by user.",
                            })
            else:
                yield {"type": "final_response", "content": assistant_msg.content}
                break
