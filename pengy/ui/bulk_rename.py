"""Bulk Rename Chats: give every stored chat a model-generated title.

Started from Settings → UI.  Meant for users arriving with a long history of
chats titled after the first 50 characters of their first message.  One
request per chat, so it runs on a daemon thread behind a progress dialog and
can be stopped between chats.

Each renamed chat is marked ``title_source: "model"`` as it is written, and
only unmarked chats are queued, so a run that was stopped, crashed or closed
simply resumes on the next one.  ``"user"`` titles are never touched.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from pengy.core.chat_manager import (
    TITLE_SOURCE_MODEL, TITLE_SOURCE_USER, get_chat, load_index, set_chat_title,
)
from pengy.core.chat_title import generate_title

#: Stop after this many API failures in a row: the endpoint is down, or the
#: model is wrong, and every remaining chat would fail the same way.
MAX_CONSECUTIVE_FAILURES = 5


class BulkRenameWorker(QObject):
    """Renames *chat_ids* one by one on a daemon thread.

    Each title is written with ``set_chat_title()``, which re-reads the chat
    just before saving, so a chat that gains messages elsewhere mid-run keeps
    them.  ``renamed`` lets the owner update open tabs and the sidebar.

    The queue comes from the index, which can lag the chat files (entries
    written before ``title_source`` existed, or by another edition), so the
    file's own ``title_source`` is checked again before spending a request.
    """

    progress = Signal(int, int, str)     # done, total, latest title
    renamed = Signal(str, str)           # chat_id, new title
    finished = Signal(dict)              # summary counts

    def __init__(self, config: dict, chat_ids: list[str], redo: bool = False):
        super().__init__()
        self._config = dict(config)
        self._chat_ids = list(chat_ids)
        self._redo = redo
        self._cancelled = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        summary = {"renamed": 0, "unchanged": 0, "skipped": 0, "kept": 0, "failed": 0,
                   "cancelled": False, "aborted": False, "last_error": ""}
        consecutive = 0
        total = len(self._chat_ids)
        for done, chat_id in enumerate(self._chat_ids, 1):
            if self._cancelled.is_set():
                summary["cancelled"] = True
                break
            label = ""
            chat = get_chat(chat_id)
            if not chat:
                summary["skipped"] += 1
            elif not _wants_rename(chat.get("title_source"), self._redo):
                summary["kept"] += 1
            else:
                old_title = chat.get("title", "")
                try:
                    title = generate_title(self._config, chat)
                except ValueError:
                    # No text to name it from (or an empty reply): leave it be.
                    summary["skipped"] += 1
                except Exception as exc:  # noqa: BLE001 - counted and reported
                    summary["failed"] += 1
                    summary["last_error"] = str(exc) or exc.__class__.__name__
                    consecutive += 1
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        summary["aborted"] = True
                        self.progress.emit(done, total, "")
                        break
                else:
                    consecutive = 0
                    # Written (and marked) even when the title is unchanged, so
                    # the next run doesn't ask about this chat again.
                    if not set_chat_title(chat_id, title, TITLE_SOURCE_MODEL):
                        summary["skipped"] += 1
                    elif title == old_title:
                        summary["unchanged"] += 1
                        self.renamed.emit(chat_id, title)
                    else:
                        summary["renamed"] += 1
                        label = title
                        self.renamed.emit(chat_id, title)
                del chat  # some chats are hundreds of MB; don't hold two at once
            self.progress.emit(done, total, label)
        self.finished.emit(summary)


def summary_text(summary: dict) -> str:
    parts = [f"Renamed {summary['renamed']} chat{'s' if summary['renamed'] != 1 else ''}."]
    if summary["unchanged"]:
        parts.append(f"{summary['unchanged']} already had that title.")
    if summary["kept"]:
        parts.append(f"{summary['kept']} already titled, left alone.")
    if summary["skipped"]:
        parts.append(f"{summary['skipped']} skipped (no messages).")
    if summary["failed"]:
        parts.append(f"{summary['failed']} failed.")
    if summary["aborted"]:
        parts.append(f"Stopped after {MAX_CONSECUTIVE_FAILURES} failures in a row.")
    elif summary["cancelled"]:
        parts.append("Stopped by you; chats already renamed keep their new titles. "
                     "Run it again to continue where it left off.")
    if summary["last_error"]:
        parts.append(f"Last error: {summary['last_error']}")
    return " ".join(parts)


class BulkRenameDialog(QDialog):
    """Progress for a BulkRenameWorker; Stop cancels between chats."""

    def __init__(self, worker: BulkRenameWorker, total: int, parent=None):
        super().__init__(parent)
        self._worker = worker
        self._running = True
        self.setWindowTitle("Bulk Rename Chats")
        self.setModal(True)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        self.status_label = QLabel(f"Renaming 0 of {total} chats…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.bar = QProgressBar()
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(0)
        layout.addWidget(self.bar)
        self.latest_label = QLabel("")
        self.latest_label.setWordWrap(True)
        layout.addWidget(self.latest_label)

        row = QHBoxLayout()
        row.addStretch(1)
        self.button = QPushButton("Stop")
        self.button.clicked.connect(self._on_button)
        row.addWidget(self.button)
        layout.addLayout(row)

        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)

    def _on_progress(self, done: int, total: int, title: str):
        self.bar.setValue(done)
        if self._running:
            self.status_label.setText(f"Renaming {done} of {total} chats…")
        if title:
            self.latest_label.setText(f"Latest: {title}")

    def _on_finished(self, summary: dict):
        self._running = False
        self.status_label.setText(summary_text(summary))
        self.latest_label.setText("")
        self.button.setText("Close")
        self.button.setEnabled(True)

    def _on_button(self):
        if self._running:
            self._stop()
        else:
            self.accept()

    def _stop(self):
        self._worker.cancel()
        self.button.setEnabled(False)
        self.status_label.setText("Stopping after the current chat…")

    def reject(self):
        # Esc / window close: stop the run, but stay open until it has stopped
        # so the summary is shown and no title is written after we're gone.
        if self._running:
            self._stop()
        else:
            super().reject()


_MISSING = object()


def _wants_rename(source, redo: bool) -> bool:
    """Whether a chat whose title came from *source* is renamed in this run."""
    if source == TITLE_SOURCE_USER:
        return False
    return redo or not source


def bulk_rename_targets(redo: bool = False) -> list[str]:
    """IDs of the chats a run would rename, newest first.

    Chats with no messages are left out.  An index entry without a
    ``title_source`` key is unknown, not unmarked: it is queued, and the worker
    reads the file to decide.
    """
    targets = []
    for entry in load_index():
        if not entry.get("msg_count", 1):
            continue
        source = entry.get("title_source", _MISSING)
        if source is _MISSING or _wants_rename(source, redo):
            targets.append(entry["id"])
    return targets


def confirm_bulk_rename(parent: QWidget, config: dict) -> bool | None:
    """Warn and ask.  ``None`` = don't run; otherwise whether to redo model titles."""
    model = config.get("model", "")
    if not model:
        QMessageBox.warning(parent, "Bulk Rename Chats",
                            "No default model is configured. Set one in Settings → LLM first.")
        return None
    pending = len(bulk_rename_targets())
    redo_total = len(bulk_rename_targets(redo=True))
    if not redo_total:
        QMessageBox.information(parent, "Bulk Rename Chats", "There are no chats to rename.")
        return None
    if pending:
        status = (f"{pending} chat{'s' if pending != 1 else ''} still "
                  f"{'have their' if pending != 1 else 'has its'} automatic title.")
    else:
        status = ("Every chat already has a model-generated or hand-picked title. "
                  "Tick the box below to generate new ones anyway.")
    box = QMessageBox(QMessageBox.Icon.Warning, "Bulk Rename Chats",
        f"This will use the currently configured default model ({model} at "
        f"{config.get('base_url', '')}) to generate new titles, replacing the "
        f"current ones. This cannot be undone.\n\n{status}\n\n"
        "Titles you set yourself in Pengy are left alone, but chats renamed by "
        "hand before this version can't be told apart from automatic titles "
        "and will be renamed too.\n\n"
        "It makes one request per chat, so a long history can take a while. "
        "You can stop at any time and run it again later to continue where it "
        "left off.\n\nContinue?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, parent)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    redo_box = QCheckBox(f"Rename all chats again, including model-titled ones ({redo_total})")
    box.setCheckBox(redo_box)
    if box.exec() != QMessageBox.StandardButton.Yes:
        return None
    if not pending and not redo_box.isChecked():
        return None
    return redo_box.isChecked()
