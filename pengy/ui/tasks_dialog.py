"""Desktop task/template manager for Pengy."""
from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from pengy.core import task_manager
from pengy.ui.theme import get_theme


class TasksDialog(QDialog):
    """Scrollable prompt-template manager.

    Emits ``task_played`` with the fully rendered prompt. The caller is expected
    to route that through the normal chat send path.
    """

    task_played = Signal(str)

    def __init__(self, theme: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        self._theme = theme or get_theme()
        self._tasks: list[dict] = []
        self.setWindowTitle("Tasks")
        self.resize(640, 520)
        self._setup_ui()
        self.apply_theme(self._theme)
        self._load_tasks()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Tasks")
        title.setStyleSheet("font-size: 16pt; font-weight: bold;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        self._new_btn = QPushButton("+ New Template")
        self._new_btn.clicked.connect(self._new_task)
        header_layout.addWidget(self._new_btn)
        layout.addWidget(header)

        hint = QLabel("Use %placeholder% in templates to prompt for dynamic values.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        layout.addWidget(self._list, 1)

        close_row = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_row.rejected.connect(self.reject)
        layout.addWidget(close_row)

    def apply_theme(self, theme: dict[str, str]):
        self._theme = theme
        self.setStyleSheet(f"""
            QDialog {{ background-color: {theme['bg']}; color: {theme['fg']}; }}
            QLabel {{ color: {theme['fg']}; }}
            QListWidget {{
                background-color: {theme['panel']};
                color: {theme['fg']};
                border: 1px solid {theme['border_soft']};
                border-radius: 6px;
            }}
        """)
        self._new_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme['primary']}; color: {theme['primary_fg']};
                border: none; border-radius: 8px; padding: 7px 14px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {theme['primary_hover']}; }}
        """)

    def _load_tasks(self):
        self._tasks = task_manager.load_tasks()
        self._list.clear()
        if not self._tasks:
            item = QListWidgetItem()
            empty = QLabel("No task templates yet. Click + New Template to create one.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {self._theme['muted']}; padding: 28px;")
            item.setSizeHint(empty.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, empty)
            return

        for task in self._tasks:
            item = QListWidgetItem()
            row = self._make_task_row(task)
            item.setData(Qt.ItemDataRole.UserRole, task["id"])
            item.setSizeHint(row.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row)

    def _make_task_row(self, task: dict) -> QWidget:
        row = QWidget()
        row.setStyleSheet(f"""
            QWidget {{ background-color: {self._theme['panel']}; color: {self._theme['fg']}; }}
        """)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 6, 6, 6)
        layout.setSpacing(6)

        text_col = QWidget()
        text_layout = QVBoxLayout(text_col)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title = QLabel(task.get("title", "Untitled Task"))
        title.setStyleSheet(f"font-weight: bold; color: {self._theme['fg']};")
        title.setMinimumWidth(0)
        text_layout.addWidget(title)

        preview = (task.get("template", "") or "").replace("\n", " ")
        if len(preview) > 70:
            preview = preview[:70] + "…"
        preview_label = QLabel(preview)
        preview_label.setStyleSheet(f"font-size: 11px; color: {self._theme['muted']};")
        preview_label.setMinimumWidth(0)
        text_layout.addWidget(preview_label)

        layout.addWidget(text_col, 1)

        btn_style = f"""
            QPushButton {{
                background-color: transparent;
                color: {self._theme['fg']};
                border: none;
                border-radius: 4px;
                font-size: 13px;
            }}
            QPushButton:hover {{ background-color: {self._theme['hover']}; }}
        """

        play_btn = QPushButton("▶")
        play_btn.setFixedSize(28, 28)
        play_btn.setToolTip("Play task")
        play_btn.setStyleSheet(btn_style)
        play_btn.clicked.connect(lambda: self._play_task(task))
        layout.addWidget(play_btn)

        edit_btn = QPushButton("✏")
        edit_btn.setFixedSize(28, 28)
        edit_btn.setToolTip("Edit task")
        edit_btn.setStyleSheet(btn_style)
        edit_btn.clicked.connect(lambda: self._edit_task(task))
        layout.addWidget(edit_btn)

        del_btn = QPushButton("🗑")
        del_btn.setFixedSize(28, 28)
        del_btn.setToolTip("Delete task")
        del_btn.setStyleSheet(btn_style)
        del_btn.clicked.connect(lambda: self._delete_task(task))
        layout.addWidget(del_btn)

        return row

    def _new_task(self):
        dialog = TaskEditDialog(theme=self._theme, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            title, template = dialog.values()
            task_manager.create_task(title, template)
            self._load_tasks()

    def _edit_task(self, task: dict):
        dialog = TaskEditDialog(task, theme=self._theme, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            title, template = dialog.values()
            task_manager.update_task(task["id"], title, template)
            self._load_tasks()

    def _delete_task(self, task: dict):
        result = QMessageBox.question(
            self,
            "Delete Task",
            f"Delete task '{task.get('title', 'Untitled Task')}'?",
            QMessageBox.StandardButton.Delete | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result == QMessageBox.StandardButton.Delete:
            task_manager.delete_task(task["id"])
            self._load_tasks()

    def _play_task(self, task: dict):
        template = task.get("template", "") or ""
        placeholders = task_manager.extract_placeholders(template)
        values: dict[str, str] = {}
        if placeholders:
            dialog = PlaceholderDialog(placeholders, theme=self._theme, parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            values = dialog.values()

        prompt = task_manager.render_template(template, values).strip()
        if not prompt:
            QMessageBox.warning(self, "Empty Task", "This task produced an empty prompt.")
            return
        self.task_played.emit(prompt)
        self.accept()


class TaskEditDialog(QDialog):
    """Create/edit dialog for one task template."""

    def __init__(self, task: dict | None = None, theme: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        self._task = task or {}
        self._theme = theme or get_theme()
        self.setWindowTitle("Edit Task" if task else "New Task")
        self.resize(560, 380)
        self._setup_ui()
        self._apply_theme()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._title = QLineEdit(self._task.get("title", ""))
        self._title.setPlaceholderText("e.g. Summarize YouTube Video")
        form.addRow("Title", self._title)
        layout.addLayout(form)

        layout.addWidget(QLabel("Prompt template"))
        self._template = QPlainTextEdit(self._task.get("template", ""))
        self._template.setPlaceholderText(
            "Summarize this youtube video: %Youtube Video URL% always use the youtube transcription skill!"
        )
        layout.addWidget(self._template, 1)

        hint = QLabel("Placeholders use %name% and each unique name is requested once when played.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_theme(self):
        t = self._theme
        self.setStyleSheet(f"""
            QDialog {{ background-color: {t['bg']}; color: {t['fg']}; }}
            QLabel {{ color: {t['fg']}; }}
            QLineEdit, QPlainTextEdit {{
                background-color: {t['input_bg']}; color: {t['input_fg']};
                border: 1px solid {t['border']}; border-radius: 6px; padding: 5px;
                selection-background-color: {t['primary']}; selection-color: {t['primary_fg']};
            }}
        """)

    def _accept_if_valid(self):
        if not self._title.text().strip():
            QMessageBox.warning(self, "Missing Title", "Please enter a task title.")
            return
        if not self._template.toPlainText().strip():
            QMessageBox.warning(self, "Missing Template", "Please enter a prompt template.")
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return self._title.text().strip(), self._template.toPlainText()


class PlaceholderDialog(QDialog):
    """Prompt for dynamic placeholder values."""

    def __init__(self, placeholders: list[str], theme: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        self._placeholders = placeholders
        self._theme = theme or get_theme()
        self._inputs: dict[str, QLineEdit] = {}
        self.setWindowTitle("Task Inputs")
        self.resize(460, max(160, min(520, 90 + len(placeholders) * 42)))
        self._setup_ui()
        self._apply_theme()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for name in self._placeholders:
            edit = QLineEdit()
            edit.setPlaceholderText(name)
            self._inputs[name] = edit
            form.addRow(name, edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_theme(self):
        t = self._theme
        self.setStyleSheet(f"""
            QDialog {{ background-color: {t['bg']}; color: {t['fg']}; }}
            QLabel {{ color: {t['fg']}; }}
            QLineEdit {{
                background-color: {t['input_bg']}; color: {t['input_fg']};
                border: 1px solid {t['border']}; border-radius: 6px; padding: 5px;
                selection-background-color: {t['primary']}; selection-color: {t['primary_fg']};
            }}
        """)

    def _accept_if_valid(self):
        missing = [name for name, edit in self._inputs.items() if not edit.text().strip()]
        if missing:
            QMessageBox.warning(
                self,
                "Missing Input",
                "Please fill in: " + ", ".join(missing),
            )
            return
        self.accept()

    def values(self) -> dict[str, str]:
        return {name: edit.text().strip() for name, edit in self._inputs.items()}
