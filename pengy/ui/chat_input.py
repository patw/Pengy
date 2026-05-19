"""Chat input widget for Pengy."""
import mimetypes
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QTextEdit, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox,
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent


_TEXT_EXTENSIONS = {
    '.txt', '.md', '.markdown', '.rst', '.json', '.xml', '.html', '.htm',
    '.css', '.js', '.ts', '.py', '.rb', '.go', '.rs', '.c', '.cpp', '.h',
    '.java', '.kt', '.swift', '.sh', '.bash', '.zsh', '.fish', '.ps1',
    '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.config',
    '.env', '.csv', '.tsv', '.sql', '.graphql', '.proto', '.tf',
    '.log', '.diff', '.patch',
}


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in _TEXT_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("text/"):
        return True
    try:
        with open(path, "rb") as f:
            f.read(8192).decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


class _InputEdit(QTextEdit):
    submit_pressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        fixed_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed_font.setPointSize(10)
        self.setFont(fixed_font)
        self.setPlaceholderText("Type a message... (Enter to send, Shift+Enter for new line)")
        self.setMaximumHeight(60)
        self.setMinimumHeight(40)
        self.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                color: #1e1e2e;
                border: 1px solid #ccc;
                border-radius: 8px;
                padding: 6px 10px;
            }
            QTextEdit:focus {
                border: 1px solid #89b4fa;
            }
        """)
        self.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QKeyEvent.Type.KeyRelease:
            if event.key() == Qt.Key.Key_Return and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.submit_pressed.emit()
                return True
        return super().eventFilter(obj, event)


class ChatInputWidget(QWidget):
    message_sent = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._attachments: list[Path] = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # File chips row — hidden until something is attached
        self._chips_row = QWidget()
        chips_layout = QHBoxLayout(self._chips_row)
        chips_layout.setContentsMargins(2, 0, 2, 0)
        chips_layout.setSpacing(4)
        chips_layout.addStretch()
        self._chips_row.hide()
        layout.addWidget(self._chips_row)

        # Input row: attach button + text edit
        input_row = QWidget()
        row_layout = QHBoxLayout(input_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        self._attach_btn = QPushButton("📎")
        self._attach_btn.setFixedSize(32, 32)
        self._attach_btn.setToolTip("Attach a text file")
        self._attach_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #ccc;
                border-radius: 6px;
                font-size: 16px;
            }
            QPushButton:hover { background: #f0f0f0; }
        """)
        self._attach_btn.clicked.connect(self._pick_file)
        row_layout.addWidget(self._attach_btn)

        self._edit = _InputEdit()
        self._edit.submit_pressed.connect(self._on_submit)
        row_layout.addWidget(self._edit)

        layout.addWidget(input_row)

    def _pick_file(self):
        path_str, _ = QFileDialog.getOpenFileName(self, "Attach File")
        if not path_str:
            return
        path = Path(path_str)
        if not _is_text_file(path):
            QMessageBox.warning(
                self,
                "Cannot Attach File",
                f'"{path.name}" doesn\'t appear to be a text file and cannot be attached.',
            )
            return
        if path not in self._attachments:
            self._attachments.append(path)
            self._add_chip(path)

    def _add_chip(self, path: Path):
        chip = QWidget()
        chip.setStyleSheet(
            "background:#e8f0fe; border:1px solid #c0d0f0; border-radius:4px;"
        )
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(5, 2, 3, 2)
        chip_layout.setSpacing(3)

        label = QLabel(f"📄 {path.name}")
        label.setStyleSheet("font-size:11px; color:#333; border:none; background:transparent;")
        chip_layout.addWidget(label)

        remove = QPushButton("✕")
        remove.setFixedSize(14, 14)
        remove.setStyleSheet("""
            QPushButton {
                background: transparent; border: none;
                color: #888; font-size: 9px;
            }
            QPushButton:hover { color: #c00; }
        """)
        remove.clicked.connect(lambda: self._remove_chip(path, chip))
        chip_layout.addWidget(remove)

        # Insert before the trailing stretch
        cl = self._chips_row.layout()
        cl.insertWidget(cl.count() - 1, chip)
        self._chips_row.show()

    def _remove_chip(self, path: Path, chip: QWidget):
        if path in self._attachments:
            self._attachments.remove(path)
        chip.deleteLater()
        if not self._attachments:
            self._chips_row.hide()

    def _clear_chips(self):
        cl = self._chips_row.layout()
        while cl.count() > 1:
            item = cl.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chips_row.hide()

    def _on_submit(self):
        text = self._edit.toPlainText().strip()
        if not text and not self._attachments:
            return

        parts = []
        for path in self._attachments:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                parts.append(f"[File: {path.name}]\n```\n{content}\n```")
            except Exception as e:
                parts.append(f"[File: {path.name} — error reading: {e}]")

        if text:
            parts.append(text)

        self._edit.setPlainText("")
        self._attachments.clear()
        self._clear_chips()

        self.message_sent.emit("\n\n".join(parts))
