"""The composer's queue strip and the keys that drive it.

Covers the widget half of "type while Pengy works": the strip renders the queued
previews, Enter on an empty composer asks the window to release the queue instead
of being swallowed, and Up/Down on an empty composer ask for a recall while
leaving ordinary cursor movement alone. Mirrors gui/tests/test_chatinput_queue.cpp.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from pengy.ui.chat_input import ChatInputWidget


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _press(widget, key):
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def test_idle_composer_says_enter_sends(qapp):
    input_ = ChatInputWidget()
    assert input_.composer_empty()
    assert input_._queue_label.isHidden()
    input_.set_queue_state([], False, False)
    assert "Enter to send" in input_._edit.placeholderText()
    assert "queue" not in input_._edit.placeholderText()


def test_running_composer_says_enter_queues(qapp):
    input_ = ChatInputWidget()
    input_.set_queue_state([], False, True)
    assert input_._queue_label.isHidden()
    assert "queue" in input_._edit.placeholderText()


def test_strip_lists_the_queue(qapp):
    input_ = ChatInputWidget()
    input_.set_queue_state(["read the logs", "then summarise"], False, True)
    assert not input_._queue_label.isHidden()
    text = input_._queue_label.text()
    assert "Queued (2)" in text
    assert "1. read the logs" in text
    assert "2. then summarise" in text
    assert "sends when this turn ends" in text


def test_held_strip_says_parked_and_how_to_release(qapp):
    input_ = ChatInputWidget()
    input_.set_queue_state(["parked"], True, False)
    text = input_._queue_label.text()
    assert "held after Stop" in text
    assert "Enter to send" in text


def test_strip_escapes_markup(qapp):
    """A message containing markup must render as text, not as HTML."""
    input_ = ChatInputWidget()
    input_.set_queue_state(["<b>not bold</b>"], False, False)
    text = input_._queue_label.text()
    assert "&lt;b&gt;" in text
    assert "<b>not bold" not in text


def test_strip_hides_when_queue_drains(qapp):
    input_ = ChatInputWidget()
    input_.set_queue_state(["one"], False, True)
    assert not input_._queue_label.isHidden()
    input_.set_queue_state([], False, False)
    assert input_._queue_label.isHidden()


def test_enter_on_empty_composer_asks_for_release_and_sends_nothing(qapp):
    input_ = ChatInputWidget()
    releases, sent = [], []
    input_.empty_submit.connect(lambda: releases.append(1))
    input_.message_sent.connect(lambda text, images: sent.append(text))
    input_._edit.submit_pressed.emit()
    assert releases == [1]
    assert sent == []


def test_enter_with_text_still_sends(qapp):
    input_ = ChatInputWidget()
    sent = []
    input_.message_sent.connect(lambda text, images: sent.append(text))
    input_.set_composer_text("hello")
    assert not input_.composer_empty()
    input_._edit.submit_pressed.emit()
    assert sent == ["hello"]
    assert input_.composer_empty()


def test_up_and_down_on_empty_composer_ask_for_recall(qapp):
    input_ = ChatInputWidget()
    recalls = []
    input_.queue_recall_requested.connect(recalls.append)
    _press(input_._edit, Qt.Key.Key_Up)
    _press(input_._edit, Qt.Key.Key_Down)
    assert recalls == [-1, 1]


def test_up_down_with_a_draft_stay_cursor_keys(qapp):
    input_ = ChatInputWidget()
    recalls = []
    input_.queue_recall_requested.connect(recalls.append)
    input_.set_composer_text("draft")
    _press(input_._edit, Qt.Key.Key_Up)
    _press(input_._edit, Qt.Key.Key_Down)
    assert recalls == []


def test_attach_paths_reattaches_a_recalled_image(qapp, tmp_path):
    """A recalled message must come back with its attachments."""
    from PySide6.QtGui import QImage
    input_ = ChatInputWidget()
    image = tmp_path / "shot.png"
    assert QImage(4, 4, QImage.Format.Format_RGB32).save(str(image))
    input_.attach_paths([image])
    assert image in input_._attachments
    assert not input_.composer_empty()
