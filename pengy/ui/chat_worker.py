"""Background worker for LLM chat requests."""
import threading
from PySide6.QtCore import QObject, Signal

from pengy.core import tools


class ChatWorker(QObject):
    """Worker that processes LLM chat requests in a background thread."""

    response = Signal(dict)
    error = Signal(str)
    finished = Signal()
    sudo_password_requested = Signal()
    question_requested = Signal(dict)

    def __init__(self, llm_client, messages: list[dict],
                 tool_confirmation: str = "none", reasoning_effort: str = "",
                 preserve_reasoning: bool = False, model: str | None = None):
        super().__init__()
        self.llm_client = llm_client
        self.messages = messages
        self.tool_confirmation = tool_confirmation
        self.reasoning_effort = reasoning_effort
        self.preserve_reasoning = preserve_reasoning
        self.model = model
        self.generator = None
        self._cancelled = threading.Event()
        self._confirmation_event = threading.Event()
        self._pending_confirmation = None
        self._sudo_event = threading.Event()
        self._pending_sudo_password = None
        # Per-run tool state so concurrent tabs don't share a sudo provider
        # or kill each other's subprocesses.
        self._tool_context = tools.ToolContext(
            sudo_provider=self._request_sudo_password)

    def cancel(self):
        """Signal the worker to stop at the next opportunity.

        Kills this run's active tool subprocesses and unblocks pending
        confirmation or sudo-password waits so the worker loop exits promptly.
        """
        self._cancelled.set()
        self._tool_context.kill_all()
        self._confirmation_event.set()
        self._sudo_event.set()
        self._question_event.set()

    def run(self):
        """Execute the chat request in the background thread."""
        # Bail immediately if cancelled before we even start
        if self._cancelled.is_set():
            self.finished.emit()
            return

        try:
            self.generator = self.llm_client.chat(
                self.messages,
                self.tool_confirmation,
                reasoning_effort=self.reasoning_effort,
                preserve_reasoning=self.preserve_reasoning,
                cancel_fn=self._cancelled.is_set,
                tool_context=self._tool_context,
                model=self.model,
            )
            send_value = None
            while True:
                # Check for cancellation before every generator step
                if self._cancelled.is_set():
                    return

                response = self.generator.send(send_value) if send_value is not None else next(self.generator)
                send_value = None

                # Clear the event BEFORE emitting so that even if send_confirmation
                # arrives before we call wait(), wait() sees it was already set.
                if response["type"] == "tool_request":
                    self._pending_confirmation = None
                    self._confirmation_event.clear()

                self.response.emit(response)

                if response["type"] == "final_response":
                    break
                elif response["type"] == "retrying":
                    # Backoff sleep handled inside the generator; just pass through
                    # the event so the UI can show "Overloaded, retrying…"
                    continue
                elif response["type"] == "tool_request":
                    self._confirmation_event.wait()
                    if self._cancelled.is_set():
                        return
                    send_value = self._pending_confirmation
                elif response["type"] == "question_request":
                    self._pending_question_response = None
                    self._question_event.clear()
                    # Emit question_requested AFTER clearing so the handler sees a fresh state
                    self.question_requested.emit(response)
                    self._question_event.wait()
                    if self._cancelled.is_set():
                        return
                    send_value = self._pending_question_response
        except StopIteration:
            pass
        except Exception as e:
            if not self._cancelled.is_set():
                self.error.emit(str(e))
        finally:
            self.generator = None
            self._tool_context.clear_sudo()
            if not self._cancelled.is_set():
                self.finished.emit()

    def _request_sudo_password(self):
        """Block the calling thread and ask the main thread for a sudo password."""
        if self._cancelled.is_set():
            return None
        self._pending_sudo_password = None
        self._sudo_event.clear()
        self.sudo_password_requested.emit()
        self._sudo_event.wait()
        return self._pending_sudo_password

    def send_sudo_password(self, password):
        """Called from the main thread with the user-entered password (or None to cancel)."""
        self._pending_sudo_password = password
        self._sudo_event.set()

    def send_confirmation(self, confirmation):
        """Called from the main thread to confirm or decline a tool call."""
        if not self.generator:
            return
        self._pending_confirmation = confirmation
        self._confirmation_event.set()

    def send_question_response(self, response: dict | None):
        """Called from the main thread with the user's answers (or None to cancel)."""
        if not self.generator:
            return
        self._pending_question_response = response
        self._question_event.set()
