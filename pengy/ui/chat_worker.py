"""Background worker for LLM chat requests."""
import json
from PySide6.QtCore import QObject, Signal, QEventLoop

from pengy.core import tools
from pengy.core.tools import execute_tool


class ChatWorker(QObject):
    """Worker that processes LLM chat requests in a background thread."""

    response = Signal(dict)
    error = Signal(str)
    finished = Signal()
    confirmation_ready = Signal(object)
    sudo_password_requested = Signal()
    sudo_password_ready = Signal(object)

    def __init__(self, llm_client, messages: list[dict], yolo_mode: bool = False):
        super().__init__()
        self.llm_client = llm_client
        self.messages = messages
        self.yolo_mode = yolo_mode
        self.generator = None
        self._event_loop = None

    def run(self):
        """Execute the chat request in the background thread."""
        tools.set_sudo_password_provider(self._request_sudo_password)
        try:
            self.generator = self.llm_client.chat(self.messages, self.yolo_mode)
            send_value = None
            while True:
                response = self.generator.send(send_value) if send_value is not None else next(self.generator)
                send_value = None
                self.response.emit(response)

                if response["type"] == "final_response":
                    break
                elif response["type"] == "tool_request":
                    self._pending_confirmation = None
                    self._event_loop = QEventLoop()
                    self.confirmation_ready.connect(self._on_confirmation)
                    self._event_loop.exec()
                    self.confirmation_ready.disconnect(self._on_confirmation)
                    send_value = self._pending_confirmation
        except StopIteration:
            pass
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.generator = None

        tools.set_sudo_password_provider(None)
        self.finished.emit()

    def _request_sudo_password(self):
        """Block the worker thread and ask the main thread for a sudo password."""
        self._pending_sudo_password = None
        self._sudo_loop = QEventLoop()
        self.sudo_password_ready.connect(self._on_sudo_password)
        self.sudo_password_requested.emit()
        self._sudo_loop.exec()
        self.sudo_password_ready.disconnect(self._on_sudo_password)
        return self._pending_sudo_password

    def _on_sudo_password(self, password):
        self._pending_sudo_password = password
        if self._sudo_loop and self._sudo_loop.isRunning():
            self._sudo_loop.quit()

    def send_sudo_password(self, password):
        """Called from the main thread with the user-entered password (or None to cancel)."""
        self.sudo_password_ready.emit(password)

    def _on_confirmation(self, confirmation):
        """Store confirmation and unblock the worker loop."""
        self._pending_confirmation = confirmation
        if self._event_loop and self._event_loop.isRunning():
            self._event_loop.quit()

    def send_confirmation(self, confirmation):
        """Send tool execution confirmation back to the generator."""
        if not self.generator:
            return
        self.confirmation_ready.emit(confirmation)
