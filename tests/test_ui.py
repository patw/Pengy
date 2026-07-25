"""Tests for Qt widget behaviour that doesn't need a visible display.

Runs against the offscreen Qt platform plugin, so it works headless / in CI.
Covers the auto-growing chat input, Enter vs Shift+Enter submit handling, and
the confirmation guard on chat deletion.

Run with:  python -m pytest tests/test_ui.py -v
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Must be set before PySide6 creates a QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from pengy.ui import chat_history as chat_history_mod
from pengy.ui.chat_history import ChatHistoryWidget
from pengy.ui.chat_input import ChatInputWidget


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the module — Qt allows only a single instance."""
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tmp_cfg():
    from pengy.core.config import set_config_dir, get_config_dir

    with tempfile.TemporaryDirectory(prefix="pengy-uitest-") as cfg_dir:
        set_config_dir(cfg_dir)
        assert get_config_dir() != Path.home() / ".config" / "pengy"
        yield Path(cfg_dir)
    set_config_dir(None)


@pytest.fixture
def chat_input(qapp):
    widget = ChatInputWidget()
    widget.resize(600, 80)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def send_key(qapp, widget, key, modifiers=Qt.KeyboardModifier.NoModifier,
             event_type=QEvent.Type.KeyPress):
    qapp.sendEvent(widget, QKeyEvent(event_type, key, modifiers, "\r"))
    qapp.processEvents()


def height_for(qapp, edit, text: str) -> int:
    edit.setPlainText(text)
    qapp.processEvents()
    return edit.height()


def lines(count: int) -> str:
    return "\n".join(f"line {i}" for i in range(count))


# ────────────────────────────────────────────────────────────────────
# Auto-growing input
# ────────────────────────────────────────────────────────────────────

class TestInputAutoGrow:
    def test_single_line_is_compact(self, qapp, chat_input):
        edit = chat_input._edit
        h = height_for(qapp, edit, "one line")
        assert h <= edit._MAX_H // 2, f"single line should be compact, got {h}px"

    def test_grows_with_added_lines(self, qapp, chat_input):
        edit = chat_input._edit
        one = height_for(qapp, edit, "one line")
        five = height_for(qapp, edit, lines(5))
        assert five > one, "input did not grow with content"

    def test_growth_is_monotonic(self, qapp, chat_input):
        edit = chat_input._edit
        heights = [height_for(qapp, edit, lines(n)) for n in (1, 2, 3, 4, 5, 6)]
        assert heights == sorted(heights), f"non-monotonic growth: {heights}"

    def test_caps_at_max_height(self, qapp, chat_input):
        edit = chat_input._edit
        h = height_for(qapp, edit, lines(200))
        assert h <= edit._MAX_H, f"grew past the cap: {h} > {edit._MAX_H}"

    def test_cap_is_taller_than_the_old_two_line_box(self, qapp, chat_input):
        """Regression guard: the box used to be pinned at 60px."""
        edit = chat_input._edit
        assert height_for(qapp, edit, lines(200)) > 60

    def test_scrollbar_only_appears_once_capped(self, qapp, chat_input):
        edit = chat_input._edit
        height_for(qapp, edit, "one line")
        assert edit.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

        height_for(qapp, edit, lines(200))
        assert edit.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded

    def test_shrinks_back_after_clearing(self, qapp, chat_input):
        edit = chat_input._edit
        one = height_for(qapp, edit, "one line")
        height_for(qapp, edit, lines(50))
        assert height_for(qapp, edit, "") <= one

    def test_never_below_the_floor(self, qapp, chat_input):
        edit = chat_input._edit
        assert height_for(qapp, edit, "") >= edit._MIN_H

    def test_survives_reapplying_the_theme(self, qapp, chat_input):
        """apply_theme() re-derives the bounds; it must not undo the sizing."""
        from pengy.ui.theme import get_theme

        edit = chat_input._edit
        height_for(qapp, edit, lines(5))
        before = edit.height()
        edit.apply_theme(get_theme())
        qapp.processEvents()
        assert edit.height() == before


# ────────────────────────────────────────────────────────────────────
# Enter / Shift+Enter
# ────────────────────────────────────────────────────────────────────

class TestSubmitKeys:
    @pytest.fixture
    def sent(self, chat_input):
        captured = []
        chat_input.message_sent.connect(lambda text, images: captured.append(text))
        return captured

    def test_enter_sends(self, qapp, chat_input, sent):
        chat_input._edit.setPlainText("hello")
        send_key(qapp, chat_input._edit, Qt.Key.Key_Return)
        assert sent == ["hello"]

    def test_numpad_enter_sends(self, qapp, chat_input, sent):
        chat_input._edit.setPlainText("hello")
        send_key(qapp, chat_input._edit, Qt.Key.Key_Enter)
        assert sent == ["hello"]

    def test_shift_enter_inserts_a_newline_and_does_not_send(self, qapp, chat_input, sent):
        edit = chat_input._edit
        edit.setPlainText("draft")
        send_key(qapp, edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)

        assert sent == []
        assert "\n" in edit.toPlainText()

    def test_releasing_shift_before_enter_does_not_send(self, qapp, chat_input, sent):
        """The old KeyRelease filter saw no modifier here and sent the draft.

        Typing Shift+Enter and lifting Shift a fraction early is a completely
        ordinary thing to do, so this used to fire mid-sentence.
        """
        edit = chat_input._edit
        edit.setPlainText("half written")

        send_key(qapp, edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        send_key(qapp, edit, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier,
                 QEvent.Type.KeyRelease)
        send_key(qapp, edit, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier,
                 QEvent.Type.KeyRelease)

        assert sent == []

    def test_enter_does_not_leave_a_stray_newline_in_the_box(self, qapp, chat_input, sent):
        """Filtering KeyPress means the newline is never inserted at all."""
        edit = chat_input._edit
        edit.setPlainText("hello")
        send_key(qapp, edit, Qt.Key.Key_Return)
        assert edit.toPlainText() == ""

    def test_empty_input_does_not_send(self, qapp, chat_input, sent):
        chat_input._edit.setPlainText("   ")
        send_key(qapp, chat_input._edit, Qt.Key.Key_Return)
        assert sent == []

    def test_input_clears_after_sending(self, qapp, chat_input, sent):
        edit = chat_input._edit
        edit.setPlainText("hello")
        send_key(qapp, edit, Qt.Key.Key_Return)
        assert edit.toPlainText() == ""

    def test_ordinary_typing_is_not_intercepted(self, qapp, chat_input, sent):
        edit = chat_input._edit
        edit.setPlainText("")
        send_key(qapp, edit, Qt.Key.Key_A)
        assert sent == []


# ────────────────────────────────────────────────────────────────────
# Delete confirmation
# ────────────────────────────────────────────────────────────────────

class TestDeleteConfirmation:
    @pytest.fixture
    def sidebar(self, qapp, tmp_cfg):
        from pengy.core.chat_manager import create_chat, save_chat, load_chats

        for title in ("Alpha", "Beta"):
            chat = create_chat()
            chat["title"] = title
            save_chat(chat)

        widget = ChatHistoryWidget()
        widget.load_chats(load_chats())
        yield widget
        widget.deleteLater()
        qapp.processEvents()

    @staticmethod
    def _answer(monkeypatch, button):
        """Stub the modal so the test never blocks, recording the call."""
        calls = []

        def fake_question(*args, **kwargs):
            calls.append((args, kwargs))
            return button

        monkeypatch.setattr(chat_history_mod.QMessageBox, "question",
                            staticmethod(fake_question))
        return calls

    def test_cancel_keeps_the_chat(self, monkeypatch, sidebar):
        from pengy.core.chat_manager import load_chats

        calls = self._answer(monkeypatch, QMessageBox.StandardButton.Cancel)
        before = [c["title"] for c in load_chats()]

        sidebar._delete_item(sidebar.chat_list.item(0))

        assert calls, "delete must ask before destroying a chat"
        assert [c["title"] for c in load_chats()] == before
        assert sidebar.chat_list.count() == len(before)

    def test_yes_deletes_the_chat(self, monkeypatch, sidebar):
        from pengy.core.chat_manager import load_chats

        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        before = [c["title"] for c in load_chats()]

        sidebar._delete_item(sidebar.chat_list.item(0))

        remaining = [c["title"] for c in load_chats()]
        assert remaining == before[1:]
        assert sidebar.chat_list.count() == len(before) - 1

    def test_closing_the_dialog_is_treated_as_cancel(self, monkeypatch, sidebar):
        """Anything that isn't an explicit Yes must not delete."""
        from pengy.core.chat_manager import load_chats

        self._answer(monkeypatch, QMessageBox.StandardButton.Escape)
        before = [c["title"] for c in load_chats()]

        sidebar._delete_item(sidebar.chat_list.item(0))

        assert [c["title"] for c in load_chats()] == before

    def test_prompt_names_the_chat(self, monkeypatch, sidebar):
        calls = self._answer(monkeypatch, QMessageBox.StandardButton.Cancel)
        title = sidebar._item_title(sidebar.chat_list.item(0))

        sidebar._delete_item(sidebar.chat_list.item(0))

        message = calls[0][0][2]
        assert title in message
        assert "cannot be undone" in message.lower()

    def test_default_button_is_cancel(self, monkeypatch, sidebar):
        """A stray Enter on the dialog must not delete."""
        calls = self._answer(monkeypatch, QMessageBox.StandardButton.Cancel)

        sidebar._delete_item(sidebar.chat_list.item(0))

        assert QMessageBox.StandardButton.Cancel in calls[0][0][4:]

    def test_trash_button_routes_through_the_confirmation(self, monkeypatch, sidebar):
        """The per-row 🗑 button is the easiest thing to hit by accident."""
        from pengy.core.chat_manager import load_chats

        calls = self._answer(monkeypatch, QMessageBox.StandardButton.Cancel)
        chat_id = sidebar.chat_list.item(0).data(Qt.ItemDataRole.UserRole)
        before = [c["title"] for c in load_chats()]

        sidebar._delete_by_id(chat_id)

        assert calls, "the row delete button bypassed the confirmation"
        assert [c["title"] for c in load_chats()] == before


# ────────────────────────────────────────────────────────────────────
# Tool-confirmation labels
# ────────────────────────────────────────────────────────────────────

class TestConfirmLabels:
    @pytest.mark.parametrize("mode,expected", [
        ("all", "YOLO"),
        ("safe", "Safe"),
        ("none", "Confirm All"),
    ])
    def test_sidebar_label(self, qapp, mode, expected):
        widget = ChatHistoryWidget()
        widget.update_quick_settings("gpt-4o", mode)
        assert expected in widget.confirm_label.text()
        widget.deleteLater()

    def test_safest_mode_is_not_labelled_none(self, qapp):
        widget = ChatHistoryWidget()
        widget.update_quick_settings("gpt-4o", "none")
        assert widget.confirm_label.text() != "Tool Confirm: None"
        widget.deleteLater()
