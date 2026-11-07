"""Edit Chat dialog: rename, generate a title with the model, or export as Markdown."""
from __future__ import annotations

import re
import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

from pengy.core.chat_manager import TITLE_SOURCE_MODEL, TITLE_SOURCE_USER
from pengy.core.chat_title import generate_title
from pengy.ui.theme import get_theme
from pengy.ui.icons import apply_button_icon


def chat_markdown(chat: dict) -> str:
    """User and assistant turns of *chat* as a Markdown document."""
    lines = [f"# {chat.get('title', 'Chat')}\n"]
    for msg in chat.get("messages", []):
        role = msg.get("role")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if role == "user":
            lines.append("**You**\n")
            lines.append(content)
            lines.append("\n---\n")
        elif role == "assistant" and content:
            lines.append("**Assistant**\n")
            lines.append(content)
            lines.append("\n---\n")
    return "\n".join(lines)


class EditChatDialog(QDialog):
    """Edit a chat's title; Save applies it, Export works independently.

    Generation runs on a daemon thread so a slow endpoint never freezes the
    window; the result comes back through a signal and only fills the field —
    nothing is written until the user presses Save.
    """

    _title_generated = Signal(str)
    _title_failed = Signal(str)

    def __init__(self, chat: dict, config: dict, theme: dict | None = None, parent=None):
        super().__init__(parent)
        self._chat = chat
        self._config = config
        self._theme = theme or get_theme()
        self._generated = ""
        self.setWindowTitle("Edit Chat")
        self.setMinimumWidth(460)
        self._title_generated.connect(self._on_generated)
        self._title_failed.connect(self._on_failed)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Title"))
        title_row = QHBoxLayout()
        self.title_input = QLineEdit(self._chat.get("title", ""))
        self.title_input.setPlaceholderText("Chat title…")
        self.title_input.selectAll()
        title_row.addWidget(self.title_input, 1)

        self.generate_btn = QPushButton("Generate")
        self.generate_btn.setToolTip(
            f"Generate a title with {self._config.get('model') or 'the configured model'}")
        apply_button_icon(self.generate_btn, "refresh", self._theme, size=14)
        self.generate_btn.clicked.connect(self._generate)
        title_row.addWidget(self.generate_btn)
        layout.addLayout(title_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {self._theme['muted']};")
        self.status_label.hide()
        layout.addWidget(self.status_label)

        layout.addSpacing(4)
        bottom = QHBoxLayout()
        self.export_btn = QPushButton("Export as Markdown…")
        apply_button_icon(self.export_btn, "save", self._theme, size=14)
        self.export_btn.clicked.connect(self._export_markdown)
        bottom.addWidget(self.export_btn)
        bottom.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self._save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        self._save_btn.setDefault(True)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

        self.setStyleSheet(f"""
            QDialog {{ background-color: {self._theme['bg']}; color: {self._theme['fg']}; }}
            QLabel {{ color: {self._theme['fg']}; }}
        """)

    def title(self) -> str:
        return self.title_input.text().strip()

    def title_source(self) -> str | None:
        """"model" for an unedited generated title, "user" for a typed one.

        Saving the title the chat already had keeps its current source, so
        just opening the dialog and pressing Save doesn't mark an automatic
        title as hand-picked (and hide it from Bulk Rename).
        """
        if self._generated and self.title() == self._generated:
            return TITLE_SOURCE_MODEL
        if self.title() == self._chat.get("title", ""):
            return self._chat.get("title_source")
        return TITLE_SOURCE_USER

    def _accept(self):
        if self.title():
            self.accept()

    def _generate(self):
        self.generate_btn.setEnabled(False)
        self.generate_btn.setText("Generating…")
        self._show_status("")
        chat, config = self._chat, dict(self._config)

        def run():
            try:
                self._title_generated.emit(generate_title(config, chat))
            except Exception as exc:  # noqa: BLE001 - shown to the user
                self._title_failed.emit(str(exc) or exc.__class__.__name__)

        threading.Thread(target=run, daemon=True).start()

    def _on_generated(self, title: str):
        self._reset_generate_btn()
        self._generated = title
        self.title_input.setText(title)
        self.title_input.setFocus()
        self._show_status("")

    def _on_failed(self, error: str):
        self._reset_generate_btn()
        self._show_status(f"Couldn't generate a title: {error}")

    def _reset_generate_btn(self):
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("Generate")

    def _show_status(self, text: str):
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def _export_markdown(self):
        # Export what is in the field, so a just-edited title names the file too.
        chat = dict(self._chat, title=self.title() or self._chat.get("title", "Chat"))
        safe_title = re.sub(r'[^\w\s-]', '', chat["title"]).strip()
        safe_title = re.sub(r'\s+', '_', safe_title) or "chat"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Chat as Markdown", f"{safe_title}.md",
            "Markdown (*.md);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(chat_markdown(chat))
        except OSError as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))
            return
        self._show_status(f"Exported to {path}")
