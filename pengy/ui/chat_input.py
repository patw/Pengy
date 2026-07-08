"""Chat input widget for Pengy."""
import mimetypes
import os
import tempfile
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QTextEdit, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox,
)
from PySide6.QtCore import Signal, Qt, QMimeData
from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent, QImage, QPixmap

from pengy.ui.theme import get_theme


_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def _is_image_file(path: Path) -> bool:
    if path.suffix.lower() in _IMAGE_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(str(path))
    return bool(mime and mime.startswith("image/"))


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
    image_pasted = Signal(Path)

    def __init__(self, parent=None):
        super().__init__(parent)
        fixed_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed_font.setPointSize(10)
        self.setFont(fixed_font)
        self.setPlaceholderText("Type a message... (Enter to send, Shift+Enter for new line)")
        self.setMaximumHeight(60)
        self.setMinimumHeight(40)
        self.apply_theme(get_theme())
        self.installEventFilter(self)

    def apply_theme(self, theme: dict[str, str]):
        self.setStyleSheet(f"""
            QTextEdit {{
                background-color: {theme['input_bg']};
                color: {theme['input_fg']};
                border: 1px solid {theme['border']};
                border-radius: 8px;
                padding: 6px 10px;
            }}
            QTextEdit:focus {{
                border: 1px solid {theme['focus']};
            }}
        """)

    def insertFromMimeData(self, source: QMimeData):
        if source.hasImage():
            img_data = source.imageData()
            if isinstance(img_data, QPixmap):
                image = img_data.toImage()
            elif isinstance(img_data, QImage):
                image = img_data
            else:
                image = QImage(img_data)
            if not image.isNull():
                fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="pengy_clip_")
                os.close(fd)
                if image.save(tmp_path):
                    self.image_pasted.emit(Path(tmp_path))
                    return
        super().insertFromMimeData(source)

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QKeyEvent.Type.KeyRelease:
            if event.key() == Qt.Key.Key_Return and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.submit_pressed.emit()
                return True
        return super().eventFilter(obj, event)


class ChatInputWidget(QWidget):
    message_sent = Signal(str, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._attachments: list[Path] = []
        self._theme = get_theme()
        self._setup_ui()
        self.apply_theme(self._theme)

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
        self._attach_btn.setToolTip("Attach a file (text or image)")
        self._attach_btn.clicked.connect(self._pick_file)
        row_layout.addWidget(self._attach_btn)

        self._edit = _InputEdit()
        self._edit.submit_pressed.connect(self._on_submit)
        self._edit.image_pasted.connect(self._on_image_pasted)
        row_layout.addWidget(self._edit)

        layout.addWidget(input_row)

    def apply_theme(self, theme: dict[str, str]):
        self._theme = theme
        self._edit.apply_theme(theme)
        self._attach_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {theme['fg']};
                border: 1px solid {theme['border']};
                border-radius: 6px;
                font-size: 16px;
            }}
            QPushButton:hover {{ background: {theme['hover']}; }}
        """)

    def _pick_file(self):
        path_str, _ = QFileDialog.getOpenFileName(self, "Attach File")
        if not path_str:
            return
        path = Path(path_str)
        if not _is_text_file(path) and not _is_image_file(path):
            QMessageBox.warning(
                self,
                "Cannot Attach File",
                f'"{path.name}" is not a supported file type.\n'
                "Supported: text files and images (JPEG, PNG, GIF, WebP).",
            )
            return
        if path not in self._attachments:
            self._attachments.append(path)
            self._add_chip(path)

    def _add_chip(self, path: Path):
        chip = QWidget()
        chip.setStyleSheet(
            f"background:{self._theme['selection']}; border:1px solid {self._theme['border']}; border-radius:4px;"
        )
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(5, 2, 3, 2)
        chip_layout.setSpacing(3)

        icon = "🖼" if _is_image_file(path) else "📄"
        label = QLabel(f"{icon} {path.name}")
        label.setStyleSheet(f"font-size:11px; color:{self._theme['fg']}; border:none; background:transparent;")
        chip_layout.addWidget(label)

        remove = QPushButton("✕")
        remove.setFixedSize(14, 14)
        remove.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                color: {self._theme['muted']}; font-size: 9px;
            }}
            QPushButton:hover {{ color: {self._theme['danger']}; }}
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

    def _on_image_pasted(self, path: Path):
        if path not in self._attachments:
            self._attachments.append(path)
            self._add_chip(path)

    def _on_submit(self):
        text = self._edit.toPlainText().strip()
        if not text and not self._attachments:
            return

        parts = []
        images = []
        for path in self._attachments:
            if _is_image_file(path):
                images.append(path)
            else:
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

        self.message_sent.emit("\n\n".join(parts), images)
