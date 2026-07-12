"""Chat history sidebar for Pengy."""
import re
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QListWidget,
    QListWidgetItem, QMenu, QFrame, QLabel, QHBoxLayout,
    QFileDialog,
)
from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtGui import QAction

from pengy.ui.theme import get_theme


class ChatHistoryWidget(QWidget):
    """Widget for managing chat history in the sidebar."""

    chat_selected = Signal(str)
    new_chat_requested = Signal()
    settings_requested = Signal()
    tasks_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_chat_id = None
        self._theme = get_theme()
        self.setup_ui()
        self.apply_theme(self._theme)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # New Chat button
        self.new_chat_btn = QPushButton("+ New Chat")
        self.new_chat_btn.setFixedHeight(36)
        self.new_chat_btn.clicked.connect(lambda: self.new_chat_requested.emit())
        layout.addWidget(self.new_chat_btn)

        # Settings button
        self.settings_btn = QPushButton("⚙ Settings")
        self.settings_btn.setFixedHeight(36)
        self.settings_btn.clicked.connect(lambda: self.settings_requested.emit())
        layout.addWidget(self.settings_btn)

        # Tasks button
        self.tasks_btn = QPushButton("📋 Tasks")
        self.tasks_btn.setFixedHeight(36)
        self.tasks_btn.clicked.connect(lambda: self.tasks_requested.emit())
        layout.addWidget(self.tasks_btn)

        layout.addSpacing(8)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(divider)

        layout.addSpacing(4)

        # Chat list
        self.chat_list = QListWidget()
        self.chat_list.itemClicked.connect(self.on_item_clicked)
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.chat_list, 1)

        layout.addSpacing(8)

        # Quick Settings panel
        qs_frame = QFrame()
        qs_frame.setFrameShape(QFrame.Shape.StyledPanel)
        qs_frame.setFrameShadow(QFrame.Shadow.Raised)
        qs_layout = QVBoxLayout(qs_frame)
        qs_layout.setContentsMargins(8, 8, 8, 8)
        qs_layout.setSpacing(4)

        status_row = QWidget()
        status_row_layout = QHBoxLayout(status_row)
        status_row_layout.setContentsMargins(0, 0, 0, 0)
        status_row_layout.setSpacing(6)

        self.status_label = QLabel("Status")
        self.status_label.setStyleSheet(f"font-weight: bold; color: {self._theme['fg']};")
        status_row_layout.addWidget(self.status_label)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {self._theme['success_soft']}; font-size: 14px;")
        status_row_layout.addWidget(self.status_dot)

        self.status_text = QLabel("Idle")
        self.status_text.setStyleSheet(f"color: {self._theme['fg']};")
        status_row_layout.addWidget(self.status_text)
        status_row_layout.addStretch()

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(500)
        self._blink_timer.timeout.connect(self._blink_dot)
        self._dot_phase = True

        qs_layout.addWidget(status_row)

        qs_divider = QFrame()
        qs_divider.setFrameShape(QFrame.Shape.HLine)
        qs_divider.setFrameShadow(QFrame.Shadow.Sunken)
        qs_layout.addWidget(qs_divider)

        self.model_label = QLabel("Model: gpt-4o")
        self.model_label.setStyleSheet(f"color: {self._theme['fg']};")
        qs_layout.addWidget(self.model_label)

        self.confirm_label = QLabel("Tool Confirm: None")
        self.confirm_label.setStyleSheet(f"color: {self._theme['fg']};")
        qs_layout.addWidget(self.confirm_label)

        self.tokens_label = QLabel("Tokens: —")
        self.tokens_label.setStyleSheet(f"color: {self._theme['fg']};")
        qs_layout.addWidget(self.tokens_label)

        layout.addWidget(qs_frame)

    def apply_theme(self, theme: dict[str, str]):
        self._theme = theme
        self.setStyleSheet(f"""
            ChatHistoryWidget {{ background-color: {theme['panel']}; color: {theme['fg']}; }}
            QFrame {{ border-color: {theme['border']}; }}
        """)
        self.status_label.setStyleSheet(f"font-weight: bold; color: {theme['fg']};")
        for label in (self.model_label, self.confirm_label, self.tokens_label):
            label.setStyleSheet(f"color: {theme['fg']};")
        self.load_chats([]) if False else None  # keep method import-safe; rows are restyled on reload

    def _make_item_widget(self, chat_id: str, title: str) -> QWidget:
        """Create the inline widget for a chat list row."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self._theme['panel']}; color: {self._theme['fg']};")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 2, 2, 2)
        row_layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {self._theme['fg']};")
        title_label.setMinimumWidth(0)
        row_layout.addWidget(title_label, 1)

        btn_style = f"""
            QPushButton {{
                background-color: transparent;
                color: {self._theme['fg']};
                border: none;
                border-radius: 4px;
                font-size: 12px;
            }}
            QPushButton:hover {{ background-color: {self._theme['hover']}; }}
        """

        save_btn = QPushButton("💾")
        save_btn.setFixedSize(24, 24)
        save_btn.setToolTip("Save chat as markdown")
        save_btn.setStyleSheet(btn_style)
        save_btn.clicked.connect(lambda: self._save_chat_markdown(chat_id))
        row_layout.addWidget(save_btn)

        del_btn = QPushButton("🗑")
        del_btn.setFixedSize(24, 24)
        del_btn.setToolTip("Delete chat")
        del_btn.setStyleSheet(btn_style)
        del_btn.clicked.connect(lambda: self._delete_by_id(chat_id))
        row_layout.addWidget(del_btn)

        return row

    def load_chats(self, chats: list[dict]):
        """Populate the chat list."""
        self.chat_list.clear()
        for chat in chats:
            chat_id = chat["id"]
            title = chat.get("title", "Untitled")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, chat_id)
            item.setSizeHint(self._make_item_widget(chat_id, title).sizeHint())
            self.chat_list.addItem(item)
            self.chat_list.setItemWidget(item, self._make_item_widget(chat_id, title))

    def on_item_clicked(self, item: QListWidgetItem):
        """Handle chat selection."""
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        self.current_chat_id = chat_id
        self.chat_selected.emit(chat_id)

    def show_context_menu(self, position):
        """Show context menu for chat items."""
        item = self.chat_list.itemAt(position)
        if not item:
            return

        menu = QMenu(self)
        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self._delete_item(item))
        menu.addAction(delete_action)
        menu.exec(self.chat_list.mapToGlobal(position))

    def _save_chat_markdown(self, chat_id: str):
        """Export a chat's messages as a markdown file."""
        from pengy.core.chat_manager import get_chat
        chat = get_chat(chat_id)
        if not chat:
            return

        lines = [f"# {chat.get('title', 'Chat')}\n"]
        for msg in chat.get("messages", []):
            role = msg.get("role")
            content = msg.get("content") or ""
            if role == "user":
                lines.append("**You**\n")
                lines.append(content)
                lines.append("\n---\n")
            elif role == "assistant":
                lines.append("**Assistant**\n")
                lines.append(content)
                lines.append("\n---\n")
        markdown = "\n".join(lines)

        safe_title = re.sub(r'[^\w\s-]', '', chat.get("title", "chat")).strip()
        safe_title = re.sub(r'\s+', '_', safe_title) or "chat"
        default_path = f"{safe_title}.md"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Chat as Markdown", default_path, "Markdown (*.md);;All Files (*)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(markdown)

    def _delete_by_id(self, chat_id: str):
        """Delete a chat by ID."""
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                self._delete_item(item)
                return

    def _delete_item(self, item: QListWidgetItem):
        """Remove a chat item from the list and storage."""
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        from pengy.core.chat_manager import delete_chat
        delete_chat(chat_id)
        self.chat_list.takeItem(self.chat_list.row(item))
        if self.current_chat_id == chat_id:
            self.current_chat_id = None

    def select_chat_by_id(self, chat_id: str):
        """Select a chat by ID."""
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                self.chat_list.setCurrentItem(item)
                self.current_chat_id = chat_id
                break

    def update_chat_title(self, chat_id: str, title: str):
        """Update the title label of a chat row in place."""
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                widget = self.chat_list.itemWidget(item)
                if widget:
                    label = widget.findChild(QLabel)
                    if label:
                        label.setText(title)
                break

    def set_thinking(self, thinking: bool):
        """Toggle the status dot between idle (green) and thinking (red blinking)."""
        if thinking:
            self._dot_phase = True
            self.status_dot.setStyleSheet(f"color: {self._theme['danger']}; font-size: 14px;")
            self.status_text.setText("Thinking…")
            self._blink_timer.start()
        else:
            self._blink_timer.stop()
            self.status_dot.setStyleSheet(f"color: {self._theme['success_soft']}; font-size: 14px;")
            self.status_text.setText("Idle")

    def set_tool_running(self, running: bool):
        """Show a solid orange dot while a tool is executing."""
        if running:
            self._blink_timer.stop()
            self.status_dot.setStyleSheet(f"color: {self._theme['running']}; font-size: 14px;")
            self.status_text.setText("Running Tool…")
            # Force an immediate synchronous repaint so the orange bubble is
            # visible on screen before the caller unblocks the worker thread.
            # Otherwise QTimer.singleShot(0, …) races with Qt's paint cycle
            # and the dot can flip straight from red (thinking) → red again
            # (tool result) without ever painting the orange state.
            self.status_dot.repaint()
        else:
            # Revert to thinking (red blinking) — caller should have set that
            self._dot_phase = True
            self.status_dot.setStyleSheet(f"color: {self._theme['danger']}; font-size: 14px;")
            self.status_text.setText("Thinking…")
            self._blink_timer.start()

    def _blink_dot(self):
        self._dot_phase = not self._dot_phase
        color = self._theme['danger'] if self._dot_phase else "transparent"
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 14px;")

    _CONFIRM_LABELS = {"all": "Tool Confirm: YOLO", "safe": "Tool Confirm: Safe", "none": "Tool Confirm: None"}

    def update_quick_settings(self, model: str, tool_confirmation: str):
        """Update the quick settings display."""
        self.model_label.setText(f"Model: {model}")
        self.confirm_label.setText(
            self._CONFIRM_LABELS.get(tool_confirmation, f"Tool Confirm: {tool_confirmation}")
        )

    def update_token_usage(self, prompt: int, completion: int):
        """Show the last turn's token usage."""
        if prompt or completion:
            self.tokens_label.setText(f"Tokens: {prompt:,} in / {completion:,} out")
        else:
            self.tokens_label.setText("Tokens: —")
