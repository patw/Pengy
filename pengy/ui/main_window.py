"""Main window for Pengy."""
import base64
import json
from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QWidget, QVBoxLayout, QHBoxLayout,
    QDialog, QPushButton, QDialogButtonBox, QLabel, QInputDialog, QLineEdit,
    QApplication, QPlainTextEdit,
)
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QTextOption

from pengy.core.config import load_config, save_config, render_system_message
from pengy.core.chat_manager import (
    load_index, create_chat, save_chat, get_chat,
    clean_dangling_tool_calls, elide_old_tool_results,
)
from pengy.core.llm_client import LLMClient
from pengy.core.image_utils import preprocess as preprocess_image
from pengy.core import tools
from pengy.ui.chat_history import ChatHistoryWidget
from pengy.ui.chat_view import ChatView
from pengy.ui.chat_input import ChatInputWidget
from pengy.ui.chat_worker import ChatWorker
from pengy.ui.settings_dialog import SettingsDialog
from pengy.ui.tasks_dialog import TasksDialog
from pengy.ui.theme import get_theme, qt_app_stylesheet, scaled_size


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
        self._yolo_this_turn = False
        self._theme = get_theme(self.config)
        self.setup_ui()
        self.apply_theme()
        self.update_llm_client()
        self.load_chat_list()
        self.chat_history.update_quick_settings(
            self.config.get("model", "gpt-4o"),
            self.config.get("tool_confirmation", "none"),
        )

        self.chat_history.chat_selected.connect(self.load_chat)
        self.chat_history.settings_requested.connect(self.open_settings)
        self.chat_history.tasks_requested.connect(self.open_tasks)

        # Create initial chat if none exist
        chats = load_index()
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
        self._stop_btn.setFixedHeight(scaled_size(32, self._theme))
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

    def apply_theme(self):
        """Apply the current appearance settings to the main window and child widgets."""
        self._theme = get_theme(self.config)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qt_app_stylesheet(self._theme))
        self.chat_view.apply_theme(self._theme)
        self.chat_input.apply_theme(self._theme)
        self.chat_history.apply_theme(self._theme)
        self._stop_btn.setFixedHeight(scaled_size(32, self._theme))
        self._stop_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self._theme['danger']};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 4px 14px;
                font-weight: bold;
                font-size: 11pt;
            }}
            QPushButton:hover {{ background-color: {self._theme['danger_hover']}; }}
        """)

    def update_llm_client(self):
        """Recreate the LLM client with current config."""
        self.llm_client = LLMClient(
            base_url=self.config["base_url"],
            api_key=self.config["api_key"],
            model=self.config["model"],
            llm_timeout=self.config.get("llm_timeout", 300),
        )
        tools.set_user_agent(self.config.get("user_agent", "PengyAgent/1.0"))
        tools.set_tool_timeout(self.config.get("tool_timeout", 60))
        tools.set_tool_output_max_chars(self.config.get("tool_output_max_chars", 50000))

    def load_chat_list(self):
        """Load and display chat history."""
        # Summaries only — the sidebar needs id and title, not message bodies.
        chats = load_index()
        self.chat_history.load_chats(chats)

    def create_new_chat(self):
        """Create a new chat session."""
        chats = load_index()
        if chats and chats[0]["title"] == "New Chat" and not chats[0]["msg_count"]:
            self.current_chat = chats[0]
            self.chat_history.select_chat_by_id(chats[0]["id"])
            self.chat_view.clear()
            return
        self.current_chat = create_chat()
        self.chat_history.load_chats(load_index())
        self.chat_history.select_chat_by_id(self.current_chat["id"])
        self.chat_view.clear()

    def open_settings(self):
        """Open the settings dialog."""
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.get_config()
            save_config(self.config)
            self.apply_theme()
            self.load_chat_list()
            if self.current_chat:
                self.chat_history.select_chat_by_id(self.current_chat["id"])
            self.update_llm_client()
            self.chat_history.update_quick_settings(
                self.config.get("model", "gpt-4o"),
                self.config.get("tool_confirmation", "none"),
            )

    def open_tasks(self):
        """Open the task/template manager."""
        dialog = TasksDialog(theme=self._theme, parent=self)
        dialog.task_played.connect(lambda prompt: self.send_message(prompt))
        dialog.exec()

    def load_chat(self, chat_id: str):
        """Load a chat session."""
        chat = get_chat(chat_id)
        if not chat:
            return

        self.current_chat = chat
        self.chat_history.select_chat_by_id(chat_id)
        self.chat_view.clear()

        # Render existing messages. Append without rendering so the whole chat
        # is built once at the end — rendering per message is O(n^2) (each
        # append rebuilds the full HTML and calls setHtml) and makes opening a
        # long chat take tens of seconds.
        for msg in chat.get("messages", []):
            if msg["role"] == "user":
                self.chat_view.append_message("user", msg["content"], render=False)
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
                        }, render=False)
                if msg.get("content"):
                    self.chat_view.append_message(
                        "assistant", msg["content"],
                        reasoning_content=msg.get("reasoning_content"),
                        render=False,
                    )
            elif msg["role"] == "tool":
                self.chat_view.append_message("tool_result", {
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": msg["content"],
                    "declined": False,
                }, render=False)
        self.chat_view.render_now()

    def send_message(self, text: str, images: list = None):
        """Send a message to the LLM."""
        if not self.current_chat:
            return
        if images is None:
            images = []

        # Reset per-turn YOLO
        self._yolo_this_turn = False

        # Persistent/display version: image filenames as placeholders, no bytes
        placeholder_parts = [f"[Image: {img.name}]" for img in images]
        if text:
            placeholder_parts.append(text)
        display_content = "\n".join(placeholder_parts)

        user_msg = {"role": "user", "content": display_content}
        self.current_chat["messages"].append(user_msg)
        self.chat_view.append_message("user", display_content)

        # Build message history for the API call
        messages = []
        system_msg = self.config.get("system_message", "")
        if system_msg:
            messages.append({"role": "system", "content": render_system_message(system_msg)})
        # Prior turns use stored (placeholder) content; current turn gets real image data
        prior = list(self.current_chat["messages"][:-1])
        # Clean dangling tool calls + elide old tool results before sending
        prior = clean_dangling_tool_calls(prior)
        prior = elide_old_tool_results(prior, self.config.get("context_keep_turns", 0))
        messages.extend(prior)

        if images:
            content_parts = []
            max_dim = self.config.get("image_max_dimension", 4096)
            max_mb = self.config.get("image_max_mb", 4.5)
            quality = self.config.get("image_quality", 85)
            for img_path in images:
                try:
                    img_bytes, mime = preprocess_image(
                        img_path, max_dimension=max_dim, max_mb=max_mb, quality=quality
                    )
                    b64 = base64.b64encode(img_bytes).decode()
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                except Exception:
                    pass
            if text:
                content_parts.append({"type": "text", "text": text})
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": display_content})

        # Update chat title from first message
        if self.current_chat["title"] == "New Chat":
            title_source = text or (images[0].name if images else "")
            short = title_source[:50] + ("..." if len(title_source) > 50 else "")
            self.current_chat["title"] = short
            self.chat_history.update_chat_title(
                self.current_chat["id"], self.current_chat["title"]
            )
            save_chat(self.current_chat)

        self._stop_btn.show()
        self.chat_history.set_thinking(True)
        self.chat_history.update_token_usage(0, 0)
        self._process_response(messages)

    def _process_response(self, messages: list[dict]):
        """Process the LLM response via a background worker thread."""
        self._abandon_worker()

        tool_confirmation = self.config.get("tool_confirmation", "none")
        self.worker = ChatWorker(
            self.llm_client,
            messages,
            tool_confirmation=tool_confirmation,
            reasoning_effort=self.config.get("reasoning_effort", ""),
            preserve_reasoning=bool(self.config.get("preserve_reasoning", False)),
        )
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
            self._handle_final_response(response)
        elif response["type"] == "tool_request":
            self.chat_history.set_tool_running(True)
            self.chat_view.append_message("tool_request", response)
            # Auto-approve if tool_confirmation is "all" or yolo_this_turn is set
            if self.config.get("tool_confirmation") == "all" or self._yolo_this_turn:
                # Defer by one event-loop iteration so Qt can repaint the
                # orange "Tool running" status bubble before the worker wakes
                # up and potentially finishes the tool before the next paint.
                tool_call_id = response["tool_call_id"]
                QTimer.singleShot(0, lambda cid=tool_call_id: (
                    self.worker.send_confirmation({"confirmed": True, "tool_call_id": cid})
                    if self.worker else None
                ))
            else:
                self._handle_tool_confirm(response)
        elif response["type"] == "assistant_tool_calls":
            self._yolo_this_turn = False
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
            self.worker_thread.wait(5000)  # don't freeze the UI if thread is stuck
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
        """User pressed Stop — clean up and restore the UI."""
        self._abandon_worker()

        self._stop_btn.hide()
        self.chat_history.set_thinking(False)

        # Fix dangling tool_calls before saving so the chat isn't bricked
        if self.current_chat:
            self.current_chat["messages"] = clean_dangling_tool_calls(
                self.current_chat["messages"]
            )
            self.chat_view.append_message("assistant", "⏹ *Stopped*")
            save_chat(self.current_chat)

    def _handle_tool_confirm(self, tool_info: dict):
        """Show confirmation dialog for tool execution."""
        if not self.worker:
            return
        dialog = ToolConfirmDialog(tool_info, self)
        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted and dialog.confirmed:
            if dialog.yolo_turn:
                self._yolo_this_turn = True
            self.worker.send_confirmation({
                "confirmed": True,
                "yolo_turn": dialog.yolo_turn,
                "tool_call_id": tool_info["tool_call_id"],
            })
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

    def _handle_final_response(self, response: dict):
        """Handle the final text response from the LLM."""
        content = response.get("content", "")

        if content:
            assistant_msg = response.get("message") or {"role": "assistant", "content": content}
            self.current_chat["messages"].append(assistant_msg)
            reasoning = assistant_msg.get("reasoning_content") or assistant_msg.get("reasoning")
            self.chat_view.append_message("assistant", content, reasoning_content=reasoning)
            save_chat(self.current_chat)

        usage = response.get("usage", {})
        if usage:
            self.chat_history.update_token_usage(
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )


class ToolConfirmDialog(QDialog):
    """Dialog for confirming tool execution with Yes-to-all-this-turn."""

    def __init__(self, tool_info: dict, parent=None):
        super().__init__(parent)
        self.confirmed = False
        self.yolo_turn = False
        self.setup_ui(tool_info)

    def setup_ui(self, tool_info: dict):
        """Build the confirmation dialog."""
        self.setWindowTitle(f"Confirm Tool: {tool_info['name']}")
        self.setModal(True)
        self.resize(480, 320)
        self.setMaximumWidth(600)

        theme = self.parent()._theme if self.parent() is not None and hasattr(self.parent(), "_theme") else get_theme()

        layout = QVBoxLayout(self)

        # Tool name header
        header_label = QLabel(f"Execute tool: <b>{tool_info['name']}</b>")
        header_label.setStyleSheet(f"color: {theme['fg']}; padding: 8px;")
        layout.addWidget(header_label)

        args_label = QLabel("Arguments:")
        args_label.setStyleSheet(f"color: {theme['fg']}; padding: 0 8px;")
        layout.addWidget(args_label)

        # Arguments — QPlainTextEdit wraps long unbroken tokens (paths, URLs,
        # hashes) that QLabel's word-wrap would refuse to break, which used to
        # force the whole dialog to grow as wide as the longest argument line.
        args_text = json.dumps(tool_info.get("args", {}), indent=2)
        max_len = 4000
        if len(args_text) > max_len:
            args_text = args_text[:max_len] + f"\n... [truncated, {len(args_text)} chars total]"
        args_edit = QPlainTextEdit(args_text)
        args_edit.setReadOnly(True)
        args_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        args_edit.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        args_edit.setStyleSheet(f"color: {theme['fg']}; padding: 4px;")
        layout.addWidget(args_edit, stretch=1)

        # Three buttons: Execute, Yes to all this turn, Cancel
        btn_layout = QHBoxLayout()

        execute_btn = QPushButton("Execute")
        execute_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme['primary']}; color: {theme['primary_fg']}; border: none;
                border-radius: 6px; padding: 8px 18px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {theme['primary_hover']}; }}
        """)
        execute_btn.clicked.connect(self._on_execute)
        btn_layout.addWidget(execute_btn)

        yes_all_btn = QPushButton("Yes to All\nThis Turn")
        yes_all_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme['warning']}; color: white; border: none;
                border-radius: 6px; padding: 8px 14px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {theme['warning_hover']}; }}
        """)
        yes_all_btn.clicked.connect(self._on_yes_all)
        btn_layout.addWidget(yes_all_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme['danger']}; color: white; border: none;
                border-radius: 6px; padding: 8px 18px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {theme['danger_hover']}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _on_execute(self):
        self.confirmed = True
        self.yolo_turn = False
        self.accept()

    def _on_yes_all(self):
        self.confirmed = True
        self.yolo_turn = True
        self.accept()
