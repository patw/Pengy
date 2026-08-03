"""Main window for Pengy — tabbed chat UI."""
import base64
import json
from dataclasses import dataclass, field
from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QWidget, QVBoxLayout, QHBoxLayout,
    QDialog, QPushButton, QDialogButtonBox, QLabel, QInputDialog, QLineEdit,
    QApplication, QPlainTextEdit, QTabWidget, QToolButton, QRadioButton,
    QButtonGroup, QScrollArea, QGroupBox,
)
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QTextOption

from pengy.core.config import load_config, save_config, render_system_message
from pengy.core.chat_manager import (
    load_index, create_chat, save_chat, get_chat, delete_chat,
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


@dataclass
class _TabSession:
    """Per-tab state for a single chat."""
    chat: dict
    chat_view: ChatView
    worker: ChatWorker | None = None
    worker_thread: QThread | None = None
    yolo_this_turn: bool = False
    thinking: bool = False
    tool_running: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


class MainWindow(QMainWindow):
    """Main application window with tabbed chats."""

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.llm_client = None
        self._theme = get_theme(self.config)

        # tab state
        self.open_tabs: dict[str, _TabSession] = {}
        self.active_chat_id: str | None = None
        # map worker id() → chat_id so signal handlers can find their session
        self._worker_to_chat: dict[int, str] = {}
        # workers that have been abandoned but threads are still running
        self._abandoned_workers: list[tuple[QThread, ChatWorker]] = []

        # question dialog queuing (Option B: one-at-a-time across tabs)
        self._pending_questions: list[tuple[str, dict]] = []  # (chat_id, question_data)
        self._question_dialog_open = False

        self.setup_ui()
        self.apply_theme()
        self.update_llm_client()
        self.load_chat_list()

        self.chat_history.chat_selected.connect(self._on_chat_selected)
        self.chat_history.settings_requested.connect(self.open_settings)
        self.chat_history.tasks_requested.connect(self.open_tasks)

        # Restore open tabs or create initial chat
        open_ids = self.config.get("open_tabs", [])
        restored = False
        if open_ids:
            for chat_id in open_ids:
                chat = get_chat(chat_id)
                if chat:
                    self._add_tab(chat, switch_to=(chat_id == open_ids[-1]))
                    restored = True

        if not self.open_tabs:
            chats = load_index()
            if not chats:
                self.create_new_chat()
            else:
                self._load_into_new_tab(chats[0]["id"])

    # ── UI setup ──────────────────────────────────────────────────

    def setup_ui(self):
        """Build the main window UI with tabbed chat view."""
        self.setWindowTitle("Pengy 🐧")
        self.resize(1100, 700)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Left sidebar
        left_splitter = QSplitter(Qt.Orientation.Vertical)
        self.chat_history = ChatHistoryWidget()
        self.chat_history.new_chat_requested.connect(self.create_new_chat)
        left_splitter.addWidget(self.chat_history)

        # Right pane: tab widget + input row
        right_splitter = QSplitter(Qt.Orientation.Vertical)

        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.setUsesScrollButtons(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        right_splitter.addWidget(self.tab_widget)

        # Input row
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

        # Main splitter
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(left_splitter)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([300, 800])

        main_layout.addWidget(main_splitter)

    # ── Tab management ────────────────────────────────────────────

    def _add_tab(self, chat: dict, switch_to: bool = True) -> _TabSession:
        """Create a TabSession and add its ChatView as a new tab."""
        chat_view = ChatView()
        session = _TabSession(chat=chat, chat_view=chat_view)

        # Apply theme FIRST so render_now() uses the correct colours
        chat_view.apply_theme(self._theme)

        # Render existing messages into the new ChatView
        for msg in chat.get("messages", []):
            self._render_message(chat_view, msg)
        chat_view.render_now()

        self.open_tabs[chat["id"]] = session
        title = chat.get("title", "New Chat")[:30]
        idx = self.tab_widget.addTab(chat_view, title)

        if switch_to:
            self.tab_widget.setCurrentIndex(idx)

        self._save_open_tabs()
        return session

    def _close_tab(self, index: int):
        """Close a tab: save chat, clean worker, remove from state."""
        chat_view = self.tab_widget.widget(index)
        # Find which session owns this chat_view
        chat_id = None
        for cid, session in self.open_tabs.items():
            if session.chat_view is chat_view:
                chat_id = cid
                break

        if chat_id is None:
            self.tab_widget.removeTab(index)
            return

        session = self.open_tabs.get(chat_id)
        if session:
            self._abandon_worker_for(session)

        # Save or delete the chat
        is_empty_new = (session and session.chat
                        and session.chat.get("title") == "New Chat"
                        and not session.chat.get("messages"))
        if session and session.chat:
            if is_empty_new:
                delete_chat(session.chat["id"])
            else:
                save_chat(session.chat)

        self.tab_widget.removeTab(index)
        self.open_tabs.pop(chat_id, None)

        self._save_open_tabs()

        # If the user closed the last tab, always give them a fresh one
        if self.tab_widget.count() == 0:
            self.create_new_chat()

    def _on_tab_changed(self, index: int):
        """Update active tab state when user switches tabs."""
        if index < 0:
            return
        chat_view = self.tab_widget.widget(index)
        for cid, session in self.open_tabs.items():
            if session.chat_view is chat_view:
                self.active_chat_id = cid
                self.chat_history.select_chat_by_id(cid)
                self._update_quick_settings_for(session)
                self._stop_btn.setVisible(session.thinking)
                return

    def _tab_for_chat(self, chat_id: str) -> _TabSession | None:
        return self.open_tabs.get(chat_id)

    def _save_open_tabs(self):
        """Persist the list of open chat IDs to config."""
        self.config["open_tabs"] = list(self.open_tabs.keys())
        save_config(self.config)

    def _update_tab_title(self, session: _TabSession):
        """Update the tab label with optional status indicator."""
        base = session.chat.get("title", "New Chat")[:30]
        prefix = "● " if session.thinking else ""

        for i in range(self.tab_widget.count()):
            if self.tab_widget.widget(i) is session.chat_view:
                self.tab_widget.setTabText(i, f"{prefix}{base}")
                return

    # ── Chat list / sidebar interaction ───────────────────────────

    def _on_chat_selected(self, chat_id: str):
        """User clicked a chat in the sidebar."""
        existing = self._tab_for_chat(chat_id)
        if existing:
            # Already open — switch to its tab
            for i in range(self.tab_widget.count()):
                if self.tab_widget.widget(i) is existing.chat_view:
                    self.tab_widget.setCurrentIndex(i)
                    return
        else:
            self._load_into_new_tab(chat_id)

    def _load_into_new_tab(self, chat_id: str):
        """Load a chat from disk into a new tab and switch to it."""
        # If there's a single empty "New Chat" tab, replace it
        if len(self.open_tabs) == 1:
            only_id = next(iter(self.open_tabs))
            only_session = self.open_tabs[only_id]
            if (only_session.chat.get("title") == "New Chat"
                    and not only_session.chat.get("messages")):
                # Delete the empty chat and remove its tab
                delete_chat(only_session.chat["id"])
                for i in range(self.tab_widget.count()):
                    if self.tab_widget.widget(i) is only_session.chat_view:
                        self.tab_widget.removeTab(i)
                        break
                self.open_tabs.pop(only_id, None)

        chat = get_chat(chat_id)
        if not chat:
            return

        session = self._add_tab(chat, switch_to=True)
        self.active_chat_id = chat_id
        self.chat_history.select_chat_by_id(chat_id)
        self._update_quick_settings_for(session)

    # ── Theme ─────────────────────────────────────────────────────

    def apply_theme(self):
        """Apply the current appearance settings to the main window and child widgets."""
        self._theme = get_theme(self.config)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qt_app_stylesheet(self._theme))
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
        # Re-theme all open chat views
        for session in self.open_tabs.values():
            session.chat_view.apply_theme(self._theme)

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
        chats = load_index()
        self.chat_history.load_chats(chats)

    # ── Chat lifecycle ────────────────────────────────────────────

    def create_new_chat(self):
        """Create a new chat in a new tab, or switch to an existing empty one."""
        # If any open tab is an empty "New Chat", just switch to it
        for cid, session in self.open_tabs.items():
            if (session.chat.get("title") == "New Chat"
                    and not session.chat.get("messages")):
                for i in range(self.tab_widget.count()):
                    if self.tab_widget.widget(i) is session.chat_view:
                        self.tab_widget.setCurrentIndex(i)
                        return

        chat = create_chat()
        self.load_chat_list()
        session = self._add_tab(chat, switch_to=True)
        self.active_chat_id = chat["id"]
        self.chat_history.select_chat_by_id(chat["id"])
        self._update_quick_settings_for(session)

    def open_settings(self):
        """Open the settings dialog."""
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.get_config()
            save_config(self.config)
            self.apply_theme()
            self.load_chat_list()
            if self.active_chat_id:
                self.chat_history.select_chat_by_id(self.active_chat_id)
            self.update_llm_client()
            session = self._tab_for_chat(self.active_chat_id) if self.active_chat_id else None
            if session:
                self._update_quick_settings_for(session)

    def open_tasks(self):
        """Open the task/template manager."""
        dialog = TasksDialog(theme=self._theme, parent=self)
        dialog.task_played.connect(lambda prompt: self.send_message(prompt))
        dialog.exec()

    # ── Message rendering ─────────────────────────────────────────

    def _render_message(self, chat_view: ChatView, msg: dict):
        """Render a single message into a ChatView (used when loading a tab)."""
        role = msg.get("role")
        if role == "user":
            chat_view.append_message("user", msg.get("content", ""), render=False)
        elif role == "assistant":
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    try:
                        args = json.loads(tc["function"]["arguments"]) if isinstance(
                            tc["function"].get("arguments"), str
                        ) else tc["function"].get("arguments", {})
                    except (json.JSONDecodeError, KeyError):
                        args = {}
                    chat_view.append_message("tool_request", {
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "args": args,
                    }, render=False)
            if msg.get("content"):
                chat_view.append_message(
                    "assistant", msg["content"],
                    reasoning_content=msg.get("reasoning_content"),
                    render=False,
                )
        elif role == "tool":
            chat_view.append_message("tool_result", {
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": msg["content"],
                "declined": False,
            }, render=False)

    # ── Sending messages ──────────────────────────────────────────

    def send_message(self, text: str, images: list = None):
        """Send a message from the active tab to the LLM."""
        if not self.active_chat_id:
            return
        session = self._tab_for_chat(self.active_chat_id)
        if not session:
            return
        if images is None:
            images = []

        session.yolo_this_turn = False

        placeholder_parts = [f"[Image: {img.name}]" for img in images]
        if text:
            placeholder_parts.append(text)
        display_content = "\n".join(placeholder_parts)

        user_msg = {"role": "user", "content": display_content}
        session.chat["messages"].append(user_msg)
        session.chat_view.append_message("user", display_content)

        # Build message history
        messages = []
        system_msg = self.config.get("system_message", "")
        if system_msg:
            messages.append({"role": "system", "content": render_system_message(system_msg)})
        prior = list(session.chat["messages"][:-1])
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
        if session.chat["title"] == "New Chat":
            title_source = text or (images[0].name if images else "")
            short = title_source[:50] + ("..." if len(title_source) > 50 else "")
            session.chat["title"] = short
            self.chat_history.update_chat_title(session.chat["id"], short)
            save_chat(session.chat)
            self._update_tab_title(session)

        self._stop_btn.show()
        session.thinking = True
        self._update_tab_title(session)
        self._update_quick_settings_for(session)
        self._process_response(session, messages)

    def _process_response(self, session: _TabSession, messages: list[dict]):
        """Launch a background worker for a tab's LLM request."""
        chat_id = session.chat["id"]
        self._abandon_worker_for(session)

        tool_confirmation = self.config.get("tool_confirmation", "none")
        worker = ChatWorker(
            self.llm_client,
            messages,
            tool_confirmation=tool_confirmation,
            reasoning_effort=self.config.get("reasoning_effort", ""),
            preserve_reasoning=bool(self.config.get("preserve_reasoning", False)),
        )
        thread = QThread()
        worker.moveToThread(thread)

        # Map worker to chat so handlers can find their session via sender()
        self._worker_to_chat[id(worker)] = chat_id

        # Connect directly to self methods — PySide6 auto-detects the
        # thread boundary and uses QueuedConnection because worker lives
        # in the background thread and self lives in the main thread.
        worker.response.connect(self._on_worker_response)
        worker.error.connect(self._on_worker_error)
        worker.finished.connect(self._on_worker_finished)
        worker.sudo_password_requested.connect(self._on_sudo_password_requested)
        worker.question_requested.connect(self._on_worker_question)

        thread.started.connect(worker.run)
        thread.start()

        session.worker = worker
        session.worker_thread = thread

    # ── Worker signal handlers ────────────────────────────────────

    def _sender_chat_id(self) -> str | None:
        """Return the chat_id for the worker that emitted the current signal."""
        worker = self.sender()
        return self._worker_to_chat.get(id(worker)) if worker else None

    def _on_worker_response(self, response: dict):
        chat_id = self._sender_chat_id()
        if not chat_id:
            return
        session = self._tab_for_chat(chat_id)
        if not session:
            return
        if not isinstance(response, dict) or "type" not in response:
            return

        if response["type"] == "final_response":
            self._handle_final_response(session, response)
        elif response["type"] == "tool_request":
            session.thinking = True
            session.tool_running = True
            self._update_tab_title(session)
            session.chat_view.append_message("tool_request", response)
            if self.config.get("tool_confirmation") == "all" or session.yolo_this_turn:
                tool_call_id = response["tool_call_id"]
                QTimer.singleShot(0, lambda cid=tool_call_id, s=session: (
                    s.worker.send_confirmation({"confirmed": True, "tool_call_id": cid})
                    if s.worker else None
                ))
            else:
                self._handle_tool_confirm(chat_id, session, response)
        elif response["type"] == "assistant_tool_calls":
            session.yolo_this_turn = False
            session.chat["messages"].append(response["message"])
        elif response["type"] == "tool_result":
            session.tool_running = False
            session.thinking = True  # still thinking after tool result
            self._update_tab_title(session)
            session.chat["messages"].append({
                "role": "tool",
                "tool_call_id": response["tool_call_id"],
                "content": response["content"],
            })
            session.chat_view.append_message("tool_result", response)

    def _on_worker_error(self, error_msg: str):
        chat_id = self._sender_chat_id()
        if not chat_id:
            return
        session = self._tab_for_chat(chat_id)
        if not session:
            return
        session.chat_view.append_message("assistant", f"Error: {error_msg}")
        session.thinking = False
        session.tool_running = False
        self._update_tab_title(session)
        if session is self._tab_for_chat(self.active_chat_id):
            self._stop_btn.hide()
            self._update_quick_settings_for(session)
        if session.worker_thread and session.worker_thread.isRunning():
            session.worker_thread.quit()

    def _on_worker_finished(self):
        chat_id = self._sender_chat_id()
        if chat_id:
            self._worker_to_chat.pop(id(self.sender()), None)
        if not chat_id:
            return
        session = self._tab_for_chat(chat_id)
        if not session:
            return
        if session.worker_thread and session.worker_thread.isRunning():
            session.worker_thread.quit()
            session.worker_thread.wait(5000)
        session.worker = None
        session.worker_thread = None
        session.thinking = False
        session.tool_running = False
        self._update_tab_title(session)
        if session is self._tab_for_chat(self.active_chat_id):
            self._stop_btn.hide()
            self._update_quick_settings_for(session)

    # ── Worker lifecycle ──────────────────────────────────────────

    def _abandon_worker_for(self, session: _TabSession):
        """Disconnect signals from a tab's worker and let it die silently."""
        if session.worker is not None:
            self._worker_to_chat.pop(id(session.worker), None)
            session.worker.cancel()
            try:
                session.worker.response.disconnect(self._on_worker_response)
                session.worker.error.disconnect(self._on_worker_error)
                session.worker.finished.disconnect(self._on_worker_finished)
                session.worker.sudo_password_requested.disconnect(
                    self._on_sudo_password_requested)
                session.worker.question_requested.disconnect(
                    self._on_worker_question)
            except (TypeError, RuntimeError):
                pass

        if session.worker_thread is not None:
            session.worker_thread.quit()
            self._abandoned_workers.append((session.worker_thread, session.worker))
            session.worker_thread.finished.connect(self._reap_abandoned_workers)

        session.worker = None
        session.worker_thread = None

    def _reap_abandoned_workers(self):
        """Drop references to abandoned threads that have finished."""
        self._abandoned_workers = [
            (t, w) for t, w in self._abandoned_workers if t.isRunning()
        ]

    def _stop_worker(self):
        """User pressed Stop — stop the active tab's worker."""
        session = self._tab_for_chat(self.active_chat_id) if self.active_chat_id else None
        if not session:
            return

        self._abandon_worker_for(session)

        self._stop_btn.hide()
        session.thinking = False
        session.tool_running = False
        self._update_tab_title(session)

        if session.chat:
            session.chat["messages"] = clean_dangling_tool_calls(session.chat["messages"])
            session.chat_view.append_message("assistant", "⏹ *Stopped*")
            save_chat(session.chat)

    # ── Tool confirmation ─────────────────────────────────────────

    def _handle_tool_confirm(self, chat_id: str, session: _TabSession, tool_info: dict):
        """Show confirmation dialog for tool execution."""
        if not session.worker:
            return
        dialog = ToolConfirmDialog(tool_info, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.confirmed:
            if dialog.yolo_turn:
                session.yolo_this_turn = True
            session.worker.send_confirmation({
                "confirmed": True,
                "yolo_turn": dialog.yolo_turn,
                "tool_call_id": tool_info["tool_call_id"],
            })
        else:
            session.worker.send_confirmation(None)

    # ── Question dialog ────────────────────────────────────────

    def _on_worker_question(self, question_data: dict):
        """Handle a question_request from a worker (queued one-at-a-time)."""
        chat_id = self._sender_chat_id()
        if not chat_id:
            return
        session = self._tab_for_chat(chat_id)
        if not session or not session.worker:
            return

        if self._question_dialog_open:
            # Queue it — show it when the current one closes
            self._pending_questions.append((chat_id, question_data))
            # Flash the tab title to indicate it's waiting
            session.thinking = True
            self._update_tab_title(session)
            return

        self._show_question_dialog(chat_id, session, question_data)

    def _show_question_dialog(self, chat_id: str, session: _TabSession, question_data: dict):
        """Show the QuestionDialog and send the response back to the worker."""
        self._question_dialog_open = True

        tab_title = session.chat.get("title", "New Chat")[:30]
        questions = question_data.get("questions", [])
        dialog = QuestionDialog(tab_title, questions, self._theme, self)
        result = dialog.exec()

        if result == QDialog.DialogCode.Accepted and dialog.answers:
            session.worker.send_question_response({
                "answered": True,
                "tool_call_id": question_data["tool_call_id"],
                "answers": dialog.answers,
            })
        else:
            session.worker.send_question_response(None)

        # Clean up thinking flag if we flashed it for queued state
        session.thinking = bool(session.worker and session.worker_thread and session.worker_thread.isRunning())
        self._update_tab_title(session)

        self._question_dialog_open = False

        # Show next queued question if any
        if self._pending_questions:
            next_chat_id, next_data = self._pending_questions.pop(0)
            next_session = self._tab_for_chat(next_chat_id)
            if next_session and next_session.worker:
                # Clear the queued flashing indicator
                next_session.thinking = bool(next_session.worker and next_session.worker_thread and next_session.worker_thread.isRunning())
                self._update_tab_title(next_session)
                self._show_question_dialog(next_chat_id, next_session, next_data)

    def _on_sudo_password_requested(self):
        """Show a password dialog when a sudo command needs a password."""
        chat_id = self._sender_chat_id()
        if not chat_id:
            return
        session = self._tab_for_chat(chat_id)
        if not session or not session.worker:
            return
        password, ok = QInputDialog.getText(
            self,
            "sudo Password",
            "Enter sudo password:",
            QLineEdit.EchoMode.Password,
        )
        session.worker.send_sudo_password(password if ok else None)

    # ── Final response handling ───────────────────────────────────

    def _handle_final_response(self, session: _TabSession, response: dict):
        """Handle the final text response from the LLM."""
        content = response.get("content", "")

        if content:
            assistant_msg = response.get("message") or {"role": "assistant", "content": content}
            session.chat["messages"].append(assistant_msg)
            reasoning = assistant_msg.get("reasoning_content") or assistant_msg.get("reasoning")
            session.chat_view.append_message("assistant", content, reasoning_content=reasoning)
            save_chat(session.chat)

        usage = response.get("usage", {})
        if usage:
            session.prompt_tokens = usage.get("prompt_tokens", 0)
            session.completion_tokens = usage.get("completion_tokens", 0)
            if session is self._tab_for_chat(self.active_chat_id):
                self.chat_history.update_token_usage(
                    session.prompt_tokens, session.completion_tokens)

    def _update_quick_settings_for(self, session: _TabSession):
        """Update the sidebar quick-settings panel for a given tab."""
        self.chat_history.update_quick_settings(
            self.config.get("model", "gpt-4o"),
            self.config.get("tool_confirmation", "none"),
        )
        if session.prompt_tokens or session.completion_tokens:
            self.chat_history.update_token_usage(
                session.prompt_tokens, session.completion_tokens)
        # Drive the status dot + label from this tab's state
        if session.tool_running:
            self.chat_history.set_tool_running(True)
        elif session.thinking:
            self.chat_history.set_thinking(True)
        else:
            self.chat_history.set_thinking(False)

    # ── Clean shutdown ────────────────────────────────────────────

    def closeEvent(self, event):
        """Save all open tabs and stop running workers before closing.

        QThreads that are still running when the window is destroyed abort the
        process ("QThread: Destroyed while thread is still running"), so cancel
        every live worker and wait briefly for its thread to exit first.
        """
        self._save_open_tabs()
        for session in self.open_tabs.values():
            if session.chat:
                save_chat(session.chat)

        # Cancel all live workers (open tabs + already-abandoned ones), then
        # wait for their threads to finish so none is destroyed mid-run.
        threads: list[QThread] = []
        for session in self.open_tabs.values():
            if session.worker is not None:
                session.worker.cancel()
            if session.worker_thread is not None:
                threads.append(session.worker_thread)
        for thread, worker in self._abandoned_workers:
            if worker is not None:
                worker.cancel()
            if thread is not None:
                threads.append(thread)

        for thread in threads:
            if thread is not None and thread.isRunning():
                thread.quit()
        for thread in threads:
            if thread is not None and thread.isRunning():
                thread.wait(3000)

        # Cancel any pending queued questions so their workers don't hang
        for chat_id, qdata in self._pending_questions:
            session = self._tab_for_chat(chat_id)
            if session and session.worker:
                session.worker.send_question_response(None)
        self._pending_questions.clear()

        super().closeEvent(event)


class QuestionDialog(QDialog):
    """Modal dialog for answering LLM questions — one at a time, tab-aware."""

    def __init__(self, tab_title: str, questions: list[dict], theme: dict, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.answers: list[str] = []
        self._button_groups: list[QButtonGroup] = []
        self.setup_ui(tab_title, questions, theme)

    def setup_ui(self, tab_title: str, questions: list[dict], theme: dict):
        """Build the question dialog with radio buttons for each question."""
        self.setWindowTitle(f"Pengy — Tab: {tab_title}")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setMaximumWidth(680)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        header = QLabel(f"<b>Questions from tab:</b> {tab_title}")
        header.setStyleSheet(f"color: {theme['fg']}; padding: 4px;")
        layout.addWidget(header)

        pending_count = 0
        parent_window = self.parent()
        if parent_window and hasattr(parent_window, '_pending_questions'):
            pending_count = len(parent_window._pending_questions)
        if pending_count:
            note = QLabel(f"[{pending_count} pending question(s) in other tabs]")
            note.setStyleSheet(f"color: {theme['muted']}; font-size: 10pt; padding: 0 4px;")
            layout.addWidget(note)

        # Scroll area for multiple questions
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {theme['bg']}; }}")
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(16)

        for qi, q in enumerate(questions):
            group_box = QGroupBox()
            group_box.setStyleSheet(f"""
                QGroupBox {{
                    color: {theme['primary']};
                    font-weight: bold;
                    border: 1px solid {theme['border_soft']};
                    border-radius: 6px;
                    margin-top: 8px;
                    padding: 12px 8px 8px 8px;
                }}
                QGroupBox::title {{
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                }}
            """)
            group_box.setTitle(q.get("header", f"Question {qi + 1}"))

            group_layout = QVBoxLayout(group_box)

            question_label = QLabel(q.get("question", ""))
            question_label.setWordWrap(True)
            question_label.setStyleSheet(f"color: {theme['fg']}; font-weight: normal; padding: 4px 0;")
            group_layout.addWidget(question_label)

            btn_group = QButtonGroup(self)
            self._button_groups.append(btn_group)

            options = q.get("options", [])
            for oi, opt in enumerate(options):
                radio = QRadioButton()
                label_text = opt.get("label", "")
                desc = opt.get("description", "")
                radio.setText(f"{label_text}  —  {desc}" if desc else label_text)
                radio.setStyleSheet(f"""
                    QRadioButton {{
                        color: {theme['fg']};
                        padding: 3px 0;
                        spacing: 8px;
                    }}
                    QRadioButton::indicator {{
                        width: 14px;
                        height: 14px;
                    }}
                """)
                btn_group.addButton(radio, oi)
                group_layout.addWidget(radio)

                # Select first option by default
                if oi == 0:
                    radio.setChecked(True)

            scroll_layout.addWidget(group_box)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, stretch=1)

        # Buttons
        btn_layout = QHBoxLayout()

        submit_btn = QPushButton("Submit Answers")
        submit_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme['primary']};
                color: {theme['primary_fg']};
                border: none;
                border-radius: 6px;
                padding: 8px 24px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {theme['primary_hover']}; }}
        """)
        submit_btn.clicked.connect(self._on_submit)
        btn_layout.addWidget(submit_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme['danger']};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 24px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {theme['danger_hover']}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _on_submit(self):
        """Collect answers from all button groups."""
        self.answers = []
        for bg in self._button_groups:
            checked = bg.checkedButton()
            if checked:
                # Extract just the label from the button text ("label  —  description")
                full_text = checked.text()
                label = full_text.split("  —  ")[0] if "  —  " in full_text else full_text
                self.answers.append(label)
            else:
                self.answers.append("")
        self.accept()


class ToolConfirmDialog(QDialog):
    """Dialog for confirming tool execution with Yes-to-all-this-turn."""

    def __init__(self, tool_info: dict, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
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

        header_label = QLabel(f"Execute tool: <b>{tool_info['name']}</b>")
        header_label.setStyleSheet(f"color: {theme['fg']}; padding: 8px;")
        layout.addWidget(header_label)

        args_label = QLabel("Arguments:")
        args_label.setStyleSheet(f"color: {theme['fg']}; padding: 0 8px;")
        layout.addWidget(args_label)

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
