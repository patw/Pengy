"""Desktop file drops must follow the same attachment path as the paperclip."""
import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])

from pengy.ui.chat_input import ChatInputWidget


def _drop(widget, urls):
    mime = QMimeData()
    mime.setUrls(urls)
    event = QDropEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget._edit.dropEvent(event)
    return event


def test_drop_cue_only_during_local_file_hover(qapp, tmp_path):
    widget = ChatInputWidget()
    widget._edit.setPlainText("unfinished draft")
    text = tmp_path / "notes.md"
    text.write_text("hello")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(text))])
    enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget._edit.dragEnterEvent(enter)
    assert enter.isAccepted()
    assert not widget._drop_hint.isHidden()
    assert widget._edit.property("fileDragActive") is True
    move = QDragMoveEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget._edit.dragMoveEvent(move)
    assert move.isAccepted() and not widget._drop_hint.isHidden()
    widget._edit.dragLeaveEvent(QDragLeaveEvent())
    assert widget._drop_hint.isHidden()
    assert widget._edit.property("fileDragActive") is False

    invalid = QMimeData()
    invalid.setUrls([QUrl("https://example.com/file.txt")])
    enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, invalid,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget._edit.dragEnterEvent(enter)
    assert not enter.isAccepted() and widget._drop_hint.isHidden()

    widget._edit.dragEnterEvent(QDragEnterEvent(
        QPoint(5, 5), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert not widget._drop_hint.isHidden()
    _drop(widget, [QUrl.fromLocalFile(str(text))])
    assert widget._drop_hint.isHidden()
    assert widget._edit.property("fileDragActive") is False
    assert widget._edit.toPlainText() == "unfinished draft"


def test_drop_attaches_multiple_files_without_inserting_paths(qapp, tmp_path):
    widget = ChatInputWidget()
    text = tmp_path / "notes.md"
    text.write_text("hello from file", encoding="utf-8")
    image = tmp_path / "photo.png"
    assert QImage(2, 2, QImage.Format.Format_RGB32).save(str(image))

    event = _drop(widget, [QUrl.fromLocalFile(str(text)), QUrl.fromLocalFile(str(image))])
    assert event.isAccepted()
    assert widget._attachments == [text, image]
    assert not widget._chips_row.isHidden()
    assert widget._edit.toPlainText() == ""

    _drop(widget, [QUrl.fromLocalFile(str(text))])
    assert widget._attachments == [text, image]
    sent = []
    widget.message_sent.connect(lambda message, images: sent.append((message, images)))
    widget._on_submit()
    assert sent == [("[File: notes.md]\n```\nhello from file\n```", [image])]
    assert widget._attachments == []


def test_drop_rejects_nonlocal_urls_and_directories(qapp, tmp_path):
    widget = ChatInputWidget()
    event = _drop(widget, [QUrl("https://example.com/file.txt"),
                           QUrl.fromLocalFile(str(tmp_path))])
    assert not event.isAccepted()
    assert widget._attachments == []
    assert widget._edit.toPlainText() == ""


def test_drop_skips_unsupported_files_but_keeps_supported(qapp, tmp_path, monkeypatch):
    widget = ChatInputWidget()
    good = tmp_path / "good.txt"
    good.write_text("ok")
    bad = tmp_path / "binary.bin"
    bad.write_bytes(b"\xff\x00")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    _drop(widget, [QUrl.fromLocalFile(str(bad)), QUrl.fromLocalFile(str(good))])
    assert widget._attachments == [good]
    assert len(warnings) == 1 and "binary.bin" in warnings[0]


def test_submitted_and_removed_chips_leave_the_parent_immediately(qapp, tmp_path):
    """A chip must not stay a child of the row once it is queued for deletion.

    deleteLater() alone removed the chip from the *layout* but not from
    _chips_row's child list, so the deferred delete left a dangling child that a
    later widget teardown walked as freed memory (QObject::~QObject ->
    setParent_helper) -- the single-process full-suite segfault. Detaching with
    setParent(None) before the deferred delete fixes it, and this pins that.
    """
    widget = ChatInputWidget()
    text = tmp_path / "notes.md"
    text.write_text("hello")
    image = tmp_path / "photo.png"
    assert QImage(2, 2, QImage.Format.Format_RGB32).save(str(image))
    _drop(widget, [QUrl.fromLocalFile(str(text)), QUrl.fromLocalFile(str(image))])
    chips = widget._chips_row.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
    assert len(chips) == 2

    # The × button path detaches its chip at once.
    widget._remove_chip(text, chips[0])
    assert chips[0].parent() is None

    # Submitting clears the rest without leaving them parented to the row.
    widget._on_submit()
    assert widget._chips_row.findChildren(
        QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly) == []
    assert widget._chips_row.layout().count() == 1  # only the trailing stretch
