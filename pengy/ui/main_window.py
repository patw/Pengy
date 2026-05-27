"""Main window for Pengy."""
import json
from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QWidget, QVBoxLayout, QHBoxLayout,
    QDialog, QPushButton, QDialogButtonBox, QLabel, QInputDialog, QLineEdit,
)
from PySide6.QtCore import Qt, QThread

from pengy.core.config import load_config, save_config, render_system_message
from pengy.core.chat_manager import (
    load_chats, create_chat, save_chat, get_chat,
)
from pengy.core.llm_client import LLMClient
from pengy.core import tools
from pengy.ui.chat_history import ChatHistoryWidget
from pengy.ui.chat_view import ChatView
from pengy.ui.chat_input import ChatInputWidget
from pengy.ui.chat_worker import ChatWorker
from pengy.ui.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.current_chat = None
        self.llm_client = None
        self.worker = None
        self.worker_thread = None
        self._abandoned_workers = []  # (thread, worker) kept alive until threads exit
        self.setup_ui()
        self.update_llm_client()
        self.load_chat_list()
        self.chat_history.update_quick_settings(
            self.config.get("model", "gpt-4o"),
            self.config.get("yolo_mode", False),
        )

        self.chat_history.chat_selected.connect(self.load_chat)
        self.chat_history.settings_requested.connect(self.open_settings)

        # Create initial chat if none exist
        chats = load_chats()
        if not chats:
            self.create_new_chat()
        else:
            self.load_chat(chats[0]["id"])

    def setup_ui(self):
        """Build the main window UI."""
        self.setWindowTitle("Pengy 🐧")
        self.resize(1100, 700)

        # Central widget with splitter
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Left sidebar splitter
        left_splitter = QSplitter(Qt.Orientation.Vertical)

        # Chat history sidebar
        self.chat_history = ChatHistoryWidget()
        self.chat_history.new_chat_requested.connect(self.create_new_chat)
        left_splitter.addWidget(self.chat_history)

        # Right pane splitter (chat view + input)
        right_splitter = QSplitter(Qt.Orientation.Vertical)

        self.chat_view = ChatView()
        right_splitter.addWidget(self.chat_view)

        # Input row with text input and submit button
        input_row = QWidget()
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(8, 4, 8, 4)

        self.chat_input = ChatInputWidget()
        self.chat_input.message_sent.connect(self.send_message)
        input_layout.addWidget(self.chat_input)

        self._stop_btn = QPushButton("⏹ Stop")
        self._stop_btn.setFixedHeight(32)
        self._stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #d20f39;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 4px 14px;
                font-weight: bold;
                font-size: 11pt;
            }
            QPushButton:hover { background-color: #e64553; }
        """)
        self._stop_btn.clicked.connect(self._stop_worker)
        self._stop_btn.hide()
        input_layout.addWidget(self._stop_btn)

        right_splitter.addWidget(input_row)
        right_splitter.setStretchFactor(0, 1)

        # Main splitter (left | right)
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(left_splitter)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([300, 800])

        main_layout.addWidget(main_splitter)

    def update_llm_client(self):
        """Recreate the LLM client with current config."""
        self.llm_client = LLMClient(
            base_url=self.config["base_url"],
            api_key=self.config["api_key"],
            model=self.config["model"],
        )
        tools.set_user_agent(self.config.get("user_agent", "PengyAgent/1.0"))

    def load_chat_list(self):
        """Load and display chat history."""
        chats = load_chats()
        self.chat_history.load_chats(chats)

    def create_new_chat(self):
        """Create a new chat session."""
        self.current_chat = create_chat()
        self.chat_history.load_chats(load_chats())
        self.chat_history.select_chat_by_id(self.current_chat["id"])
        self.chat_view.clear()

    def open_settings(self):
        """Open the settings dialog."""
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.get_config()
            save_config(self.config)
            self.update_llm_client()
            self.chat_history.update_quick_settings(
                self.config.get("model", "gpt-4o"),
                self.config.get("yolo_mode", False),
            )

    def load_chat(self, chat_id: str):
        """Load a chat session."""
        chat = get_chat(chat_id)
        if not chat:
            return

        self.current_chat = chat
        self.chat_history.select_chat_by_id(chat_id)
        self.chat_view.clear()

        # Render existing messages
        for msg in chat.get("messages", []):
            if msg["role"] == "user":
                self.chat_view.append_message("user", msg["content"])
            elif msg["role"] == "assistant":
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        try:
                            args = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"].get("arguments"), str) else tc["function"].get("arguments", {})
                        except (json.JSONDecodeError, KeyError):
                            args = {}
                        self.chat_view.append_message("tool_request", {
                            "tool_call_id": tc["id"],
                            "name": tc["function"]["name"],
                            "args": args,
                        })
                if msg.get("content"):
                    self.chat_view.append_message("assistant", msg["content"])
            elif msg["role"] == "tool":
                self.chat_view.append_message("tool_result", {
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": msg["content"],
                    "declined": False,
                })

    def send_message(self, text: str):
        """Send a message to the LLM."""
        if not self.current_chat:
            return

        # Add user message
        user_msg = {"role": "user", "content": text}
        self.current_chat["messages"].append(user_msg)
        self.chat_view.append_message("user", text)

        # Build message history
        messages = []
        system_msg = self.config.get("system_message", "")
        if system_msg:
            messages.append({"role": "system", "content": render_system_message(system_msg)})
        messages.extend(self.current_chat["messages"])

        # Update chat title from first message
        if self.current_chat["title"] == "New Chat":
            short = text[:50] + ("..." if len(text) > 50 else "")
            self.current_chat["title"] = short
            self.chat_history.update_chat_title(
                self.current_chat["id"], self.current_chat["title"]
            )
            save_chat(self.current_chat)

        self._stop_btn.show()
        self.chat_history.set_thinking(True)
        self._process_response(messages)

    def _process_response(self, messages: list[dict]):
        """Process the LLM response via a background worker thread."""
        # Abandon any previous worker that may still be running (e.g.
        # blocked in a C-level socket read).  Its signals are already
        # disconnected so it can't interfere.
        self._abandon_worker()

        yolo_mode = self.config.get("yolo_mode", False)
        self.worker = ChatWorker(self.llm_client, messages, yolo_mode)
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.response.connect(self._on_worker_response)
        self.worker.error.connect(self._on_worker_error)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.sudo_password_requested.connect(self._on_sudo_password_requested)

        self.worker_thread.start()

    def _on_worker_response(self, response: dict):
        """Handle a response from the background worker."""
        if not isinstance(response, dict) or "type" not in response:
            return
        if response["type"] == "final_response":
            self._handle_final_response(response["content"])
        elif response["type"] == "tool_request":
            self.chat_history.set_tool_running(True)
            self.chat_view.append_message("tool_request", response)
            if self.config.get("yolo_mode", False):
                self.worker.send_confirmation({
                    "confirmed": True,
                    "tool_call_id": response["tool_call_id"],
                })
            else:
                self._handle_tool_confirm(response)
        elif response["type"] == "assistant_tool_calls":
            self.current_chat["messages"].append(response["message"])
        elif response["type"] == "tool_result":
            self.chat_history.set_tool_running(False)
            self.current_chat["messages"].append({
                "role": "tool",
                "tool_call_id": response["tool_call_id"],
                "content": response["content"],
            })
            self.chat_view.append_message("tool_result", response)

    def _on_worker_error(self, error_msg: str):
        """Handle an error from the background worker."""
        self.chat_view.append_message("assistant", f"Error: {error_msg}")
        self._stop_btn.hide()
        self.chat_history.set_thinking(False)
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.quit()

    def _on_worker_finished(self):
        """Clean up worker thread."""
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.worker_thread.wait()
        self.worker = None
        self.worker_thread = None
        self._stop_btn.hide()
        self.chat_history.set_thinking(False)

    def _abandon_worker(self):
        """Disconnect the current worker and let it die silently.

        Does NOT kill the thread — severs signals, posts a quit event
        (non-blocking), then stashes references in _abandoned_workers so
        Python's GC cannot destroy the QThread while it's still running.
        The thread exits on its own and _reap_abandoned_workers cleans up.
        """
        if self.worker is not None:
            self.worker.cancel()
            try:
                self.worker.response.disconnect(self._on_worker_response)
                self.worker.error.disconnect(self._on_worker_error)
                self.worker.finished.disconnect(self._on_worker_finished)
                self.worker.sudo_password_requested.disconnect(
                    self._on_sudo_password_requested
                )
            except (TypeError, RuntimeError):
                pass

        if self.worker_thread is not None:
            self.worker_thread.quit()
            self._abandoned_workers.append((self.worker_thread, self.worker))
            self.worker_thread.finished.connect(self._reap_abandoned_workers)

        self.worker = None
        self.worker_thread = None

    def _reap_abandoned_workers(self):
        """Drop references to abandoned threads that have finished."""
        self._abandoned_workers = [
            (t, w) for t, w in self._abandoned_workers if t.isRunning()
        ]

    def _stop_worker(self):
        """User pressed Stop — abandon the worker and restore the UI."""
        self._abandon_worker()

        self._stop_btn.hide()
        self.chat_history.set_thinking(False)
        self.chat_history.set_tool_running(False)

        self.chat_view.append_message("assistant", "⏹ *Stopped*")

        if self.current_chat:
            save_chat(self.current_chat)

    def _handle_tool_confirm(self, tool_info: dict):
        """Show confirmation dialog for tool execution."""
        if not self.worker:
            return
        dialog = ToolConfirmDialog(tool_info, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.confirmed:
                self.worker.send_confirmation({
                    "confirmed": True,
                    "tool_call_id": tool_info["tool_call_id"],
                })
            else:
                self.worker.send_confirmation(None)
        else:
            self.worker.send_confirmation(None)

    def _on_sudo_password_requested(self):
        """Show a password dialog when a sudo command needs a password."""
        password, ok = QInputDialog.getText(
            self,
            "sudo Password",
            "Enter sudo password:",
            QLineEdit.EchoMode.Password,
        )
        self.worker.send_sudo_password(password if ok else None)

    def _handle_final_response(self, content: str):
        """Handle the final text response from the LLM."""
        # Add assistant message to chat
        assistant_msg = {"role": "assistant", "content": content}
        self.current_chat["messages"].append(assistant_msg)
        self.chat_view.append_message("assistant", content)

        # Save chat
        save_chat(self.current_chat)


class ToolConfirmDialog(QDialog):
    """Dialog for confirming tool execution."""

    def __init__(self, tool_info: dict, parent=None):
        super().__init__(parent)
        self.confirmed = False
        self.setup_ui(tool_info)

    def setup_ui(self, tool_info: dict):
        """Build the confirmation dialog."""
        self.setWindowTitle(f"Confirm Tool: {tool_info['name']}")
        self.setModal(True)
        self.resize(450, 300)

        layout = QVBoxLayout(self)

        # Tool info
        info_text = f"Execute tool: <b>{tool_info['name']}</b>\n\nArguments:\n"
        info_text += json.dumps(tool_info.get("args", {}), indent=2)
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #000000; padding: 8px;")
        layout.addWidget(info_label)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        """Handle accept."""
        self.confirmed = True
        super().accept()
