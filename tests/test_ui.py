"""Tests for Qt widget behaviour that doesn't need a visible display.

Runs against the offscreen Qt platform plugin, so it works headless / in CI.
Covers the auto-growing chat input, Enter vs Shift+Enter submit handling, and
the confirmation guard on chat deletion.

Run with:  python -m pytest tests/test_ui.py -v
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

# Must be set before PySide6 creates a QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

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
# Unified desktop text scaling
# ────────────────────────────────────────────────────────────────────

class TestUnifiedTextScaling:
    @pytest.mark.parametrize("scale,factor", [(75, 0.75), (100, 1.0), (150, 1.5), (200, 2.0)])
    def test_scale_factor_is_direct_pengy_multiplier(self, scale, factor):
        from pengy.ui.theme import ui_scale_factor

        assert ui_scale_factor({"ui_scale": scale}) == pytest.approx(factor)

    def test_external_qt_scale_factor_is_not_folded_into_preference(self, monkeypatch):
        from pengy.ui.theme import ui_scale_factor

        monkeypatch.setenv("QT_SCALE_FACTOR", "2.5")
        assert ui_scale_factor({"ui_scale": 150}) == pytest.approx(1.5)

    @pytest.mark.parametrize("scale", [75, 100, 150, 200])
    def test_ui_and_chat_font_roles_scale_proportionally(self, qapp, scale):
        from pengy.ui.theme import chat_font, ui_font

        ui_base = ui_font({"ui_scale": 100}).pointSizeF()
        chat_base = chat_font({"ui_scale": 100}).pointSizeF()
        factor = scale / 100
        assert ui_font({"ui_scale": scale}).pointSizeF() == pytest.approx(ui_base * factor)
        assert chat_font({"ui_scale": scale}).pointSizeF() == pytest.approx(chat_base * factor)

    @pytest.mark.parametrize("scale", [75, 100, 150, 200])
    def test_input_and_output_share_chat_font(self, qapp, scale):
        from pengy.ui.chat_input import ChatInputWidget
        from pengy.ui.chat_view import ChatView
        from pengy.ui.theme import get_theme

        theme = get_theme({"ui_scale": scale, "theme_mode": "light"})
        input_widget = ChatInputWidget()
        output_widget = ChatView()
        input_widget.apply_theme(theme)
        output_widget.apply_theme(theme)
        assert input_widget._edit.font().pointSizeF() == pytest.approx(output_widget.font().pointSizeF())
        assert input_widget._edit.document().defaultFont().pointSizeF() == pytest.approx(
            output_widget.document().defaultFont().pointSizeF()
        )
        input_widget.deleteLater()
        output_widget.deleteLater()

    def test_global_stylesheet_carries_scaled_system_font(self, qapp):
        from pengy.ui.theme import get_theme, qt_app_stylesheet, ui_font

        theme = get_theme({"ui_scale": 175, "theme_mode": "light"})
        css = qt_app_stylesheet(theme)
        expected = f"{ui_font(theme).pointSizeF():.2f}".rstrip("0").rstrip(".")
        assert f"font-size: {expected}pt" in css
        assert f'font-family: "{ui_font(theme).family()}"' in css

    def test_primary_sidebar_metrics_scale(self, qapp):
        from pengy.ui.chat_history import ChatHistoryWidget
        from pengy.ui.theme import get_theme, scaled_size

        theme = get_theme({"ui_scale": 175, "theme_mode": "light"})
        sidebar = ChatHistoryWidget()
        sidebar.apply_theme(theme)
        assert sidebar.settings_btn.height() == scaled_size(36, theme)
        assert sidebar.settings_btn.iconSize().width() == scaled_size(16, theme)
        sidebar.deleteLater()

    def test_chat_document_css_uses_relative_sizes(self, qapp):
        from pengy.ui.chat_view import _build_css
        from pengy.ui.theme import get_theme

        css = _build_css(get_theme({"ui_scale": 200, "theme_mode": "dark"}))
        assert "font-size: 1em" in css
        assert "font-size:0.9em" in css
        assert "font-size:0.85em" in css
        assert "font-size:1.4em" in css
        assert not __import__("re").search(r"font-size\s*:\s*[0-9.]+pt", css)


# ────────────────────────────────────────────────────────────────────
# Modal dialog lifetime
# ────────────────────────────────────────────────────────────────────

class TestDialogLifetime:
    def test_settings_fields_survive_accepted_exec(self, qapp):
        """Callers read fields after exec(); Qt must not delete them on close."""
        from pengy.core.config import load_config
        from pengy.ui.settings_dialog import SettingsDialog

        config = load_config()
        dialog = SettingsDialog(config)
        dialog.accept()
        qapp.processEvents()
        assert dialog.result() == dialog.DialogCode.Accepted
        result = dialog.get_config()
        assert result["base_url"] == dialog.base_url_input.text()


# ────────────────────────────────────────────────────────────────────
# Portable SVG icons
# ────────────────────────────────────────────────────────────────────

class TestPortableIcons:
    def test_all_bundled_icons_render(self, qapp):
        from pengy.ui.icons import themed_icon

        names = (
            "settings", "delete", "save", "play", "edit", "refresh",
            "close", "stop", "attach", "tasks", "new-chat", "file", "image",
        )
        for name in names:
            pixmap = themed_icon(name, "#123456").pixmap(24, 24)
            assert not pixmap.isNull(), name

    def test_button_records_portable_icon_name(self, qapp):
        from pengy.ui.icons import apply_button_icon
        from pengy.ui.theme import get_theme

        button = QPushButton()
        apply_button_icon(button, "settings", get_theme())
        assert button.property("pengyIcon") == "settings"
        assert not button.icon().isNull()

    def test_main_controls_no_longer_depend_on_emoji(self, chat_input, qapp):
        sidebar = ChatHistoryWidget()
        assert sidebar.settings_btn.text() == "Settings"
        assert sidebar.tasks_btn.text() == "Tasks"
        assert sidebar.settings_btn.property("pengyIcon") == "settings"
        assert sidebar.tasks_btn.property("pengyIcon") == "tasks"
        assert chat_input._attach_btn.text() == ""
        assert chat_input._attach_btn.property("pengyIcon") == "attach"
        sidebar.deleteLater()

    def test_themed_icon_is_cached_per_name_and_color(self, qapp):
        """themed_icon() is called with the same (name, color) for every row's
        Save/Delete button whenever a chat-list widget is built — rebuilding
        the full 5-size x 3-state QIcon from scratch per row (rather than
        reusing one built object) was the dominant cost behind "New Chat
        feels slow" on a sidebar with many chats."""
        from pengy.ui.icons import themed_icon, _build_themed_icon

        _build_themed_icon.cache_clear()
        a = themed_icon("save", "#123456")
        b = themed_icon("save", "#123456")
        assert a is b
        info = _build_themed_icon.cache_info()
        assert info.hits >= 1


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
        # The box must actually grow with content, or the guard below is vacuous.
        single = height_for(qapp, edit, "one line")
        multi = height_for(qapp, edit, lines(5))
        assert multi > single

        # Settle via apply_theme, then verify re-applying the theme is stable
        # and does not collapse the auto-grown box. We deliberately do NOT
        # compare against `multi` above: offscreen Qt font metrics can yield a
        # stale layout height for the first setPlainText+processEvents pass, so
        # that value is not a stable baseline and produces a false flake.
        edit.apply_theme(get_theme())
        qapp.processEvents()
        settled = edit.height()
        assert settled > single          # apply_theme kept it multi-line
        edit.apply_theme(get_theme())
        qapp.processEvents()
        assert edit.height() == settled  # re-derivation is idempotent


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


# ────────────────────────────────────────────────────────────────────
# ChatView render cache
# ────────────────────────────────────────────────────────────────────
# _build_html() used to re-run markdown + pygments over the whole history on
# every append, making a conversation O(n^2) to type into. Output is now
# memoised per message, so these guard that every path which changes a
# message's rendering also invalidates its cache entry.

class TestChatViewRenderCache:
    @pytest.fixture
    def view(self, qapp):
        from pengy.ui.chat_view import ChatView
        v = ChatView()
        yield v
        v.deleteLater()

    def _uncached(self, view) -> str:
        """Rendering with a cold cache — the reference output."""
        view._invalidate_all()
        return view._build_html()

    def test_cache_tracks_appends(self, view):
        view.append_message("user", "hi", render=False)
        view.append_message("assistant", "there", render=False)
        assert len(view._html_cache) == len(view._messages) == 2

    def test_cached_matches_uncached(self, view):
        view.append_message("user", "hello **world**", render=False)
        view.append_message("assistant", "```python\nprint(1)\n```",
                            reasoning_content="why\nbecause", render=False)
        cached = view._build_html()
        assert cached == self._uncached(view)

    def test_tool_result_invalidates_running_header(self, view):
        view.append_message("tool_request",
                            {"tool_call_id": "t1", "name": "read_file",
                             "args": {"path": "/x"}}, render=False)
        assert "running" in view._build_html()
        view.append_message("tool_result",
                            {"tool_call_id": "t1", "content": "DATA",
                             "declined": False}, render=False)
        html = view._build_html()
        assert "running" not in html
        assert html == self._uncached(view)

    def test_tool_expand_invalidates(self, view):
        view.append_message("tool_request",
                            {"tool_call_id": "t1", "name": "read_file",
                             "args": {}}, render=False)
        view.append_message("tool_result",
                            {"tool_call_id": "t1", "content": "SECRET-PAYLOAD",
                             "declined": False}, render=False)
        view._build_html()  # warm the cache while collapsed
        view._expanded_tools.add("t1")
        view._invalidate(view._tool_block_index("t1"))
        assert "SECRET-PAYLOAD" in view._build_html()

    def test_reasoning_expand_invalidates(self, view):
        view.append_message("assistant", "answer",
                            reasoning_content="head\nTAIL-LINE", render=False)
        view._build_html()  # warm the cache while collapsed
        view._expanded_reasoning.add(0)
        view._invalidate(0)
        assert "TAIL-LINE" in view._build_html()

    def test_theme_change_invalidates_all(self, view):
        # A theme swap rebuilds the pygments formatter, so cached code blocks
        # are stale even though the message content never changed.
        from pengy.ui.theme import get_theme
        view.append_message("assistant", "```python\nprint(1)\n```", render=False)
        view._build_html()  # warm under the old theme
        view.apply_theme(get_theme())
        assert view._build_html() == self._uncached(view)

    def test_clear_resets_cache(self, view):
        view.append_message("user", "hi", render=False)
        view.clear()
        assert view._html_cache == [] and view._messages == []

    def test_cache_realigns_if_messages_mutated_directly(self, view):
        view._messages.append({"role": "user", "content": "direct"})
        assert "direct" in view._build_html()
        assert len(view._html_cache) == len(view._messages)


# ────────────────────────────────────────────────────────────────────
# Auto-scroll pin  (regression: "snaps back up to old history")
# ────────────────────────────────────────────────────────────────────
# setHtml() replaces the whole document and resets the scrollbar to 0. The old
# _render() decided "am I at the bottom?" by reading scrollbar.value() *after*
# that reset — so any render landing while a previous render's deferred
# scroll-to-bottom was still pending read value()==0, concluded the user had
# scrolled up, and pinned the view to the top of the history. These guard the
# explicit _auto_scroll flag that replaced that brittle check.

class TestAutoScrollPin:
    @pytest.fixture
    def view(self, qapp):
        from pengy.ui.chat_view import ChatView
        v = ChatView()
        v.resize(400, 600)
        v.show()
        qapp.processEvents()
        # Fill with enough content to make the document taller than the viewport.
        for i in range(60):
            v.append_message("assistant", ("line %d " % i) * 20, render=False)
        yield v
        v.deleteLater()
        qapp.processEvents()

    def test_pin_survives_a_render(self, view, qapp):
        assert view._auto_scroll is True
        view._render()
        qapp.processEvents()
        assert view._auto_scroll is True

    def test_interleaved_render_keeps_the_pin(self, view, qapp):
        # Two renders back-to-back before the event loop flushes the deferred
        # scroll — the exact sequence (e.g. an image loaded mid-stream) that
        # used to read value()==0 and snap the view to the top.
        view._render()
        view._render()
        qapp.processEvents()
        assert view._auto_scroll is True

    def test_genuine_scroll_up_clears_the_pin(self, view, qapp):
        view._render()
        qapp.processEvents()
        view.verticalScrollBar().setValue(0)
        qapp.processEvents()
        assert view._auto_scroll is False

    def test_cleared_pin_does_not_yank_to_bottom(self, view, qapp):
        view._render()
        qapp.processEvents()
        sb = view.verticalScrollBar()
        sb.setValue(0)
        qapp.processEvents()
        assert view._auto_scroll is False
        view._render()
        qapp.processEvents()
        # Stayed near the top (where the user was reading), not the bottom.
        assert view.verticalScrollBar().value() < sb.maximum() // 2

    def test_clear_resets_pin(self, view, qapp):
        view._render()
        qapp.processEvents()
        view.verticalScrollBar().setValue(0)
        qapp.processEvents()
        assert view._auto_scroll is False
        view.clear()
        assert view._auto_scroll is True


# ────────────────────────────────────────────────────────────────────
# Mid-turn assistant narration (GUI)
# ────────────────────────────────────────────────────────────────────

class TestAssistantPreambleRendering:
    """Narration attached to a tool_calls message must render, and render first.

    It was persisted but never appended to the live view, so it appeared out of
    nowhere when the tab was reopened — and the reload path put it *after* the
    tool cards, which is the reverse of the order the model wrote it in.
    """

    class _RecordingView:
        def __init__(self):
            self.calls = []

        def append_message(self, role, content, **kwargs):
            self.calls.append((role, content))

    @staticmethod
    def _tool_call_message(content):
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": [{
                "id": "tc1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "/etc/hostname"}'},
            }],
        }

    def _render(self, msg):
        from pengy.ui.main_window import MainWindow
        view = self._RecordingView()
        # _render_message never touches self, so an unbound call avoids
        # standing up a whole MainWindow.
        MainWindow._render_message(None, view, msg)
        return view.calls

    def test_narration_renders_before_tool_cards(self):
        calls = self._render(self._tool_call_message("Let me check that."))
        roles = [role for role, _ in calls]
        assert roles == ["assistant", "tool_request"], (
            f"narration must precede the tool card; got {roles}"
        )
        assert calls[0][1] == "Let me check that."

    def test_no_narration_renders_only_the_tool_card(self):
        calls = self._render(self._tool_call_message(""))
        assert [role for role, _ in calls] == ["tool_request"]

    def test_plain_assistant_message_still_renders(self):
        calls = self._render({"role": "assistant", "content": "done"})
        assert [role for role, _ in calls] == ["assistant"]


# ────────────────────────────────────────────────────────────────────
# Redact Last button
# ────────────────────────────────────────────────────────────────────

class TestRedactLast:
    """MainWindow._redact_last drops the active tab's last message and
    rebuilds the ChatView from what remains — the GUI's context-pruning undo.

    Uses the same unbound-call trick as TestAssistantPreambleRendering:
    _redact_last only touches attributes it's handed, so a lightweight stand-in
    avoids constructing a whole MainWindow (config, LLM client, tabs, ...).
    """

    class _RecordingView:
        def __init__(self):
            self.calls = []

        def clear(self):
            self.calls.append(("clear",))

        def append_message(self, role, content, **kwargs):
            self.calls.append(("append", role))

        def render_now(self):
            self.calls.append(("render_now",))

    class _Session:
        def __init__(self, chat, chat_view, worker=None):
            self.chat = chat
            self.chat_view = chat_view
            self.worker = worker

    class _FakeWindow:
        def __init__(self, session):
            from pengy.ui.main_window import MainWindow
            import types
            self.active_chat_id = session.chat["id"]
            self._session = session
            self._render_message = types.MethodType(MainWindow._render_message, self)

        def _tab_for_chat(self, chat_id):
            return self._session if chat_id == self.active_chat_id else None

    def _run(self, messages, worker=None):
        from pengy.core.chat_manager import create_chat, save_chat
        from pengy.ui.main_window import MainWindow

        chat = create_chat()
        chat["messages"] = messages
        save_chat(chat)
        view = self._RecordingView()
        session = self._Session(chat, view, worker=worker)
        window = self._FakeWindow(session)
        MainWindow._redact_last(window)
        return chat, view

    def test_redact_removes_last_and_rerenders(self, tmp_cfg):
        from pengy.core.chat_manager import get_chat

        chat, view = self._run([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        assert chat["messages"] == [{"role": "user", "content": "hi"}]
        assert get_chat(chat["id"])["messages"] == chat["messages"]
        assert ("clear",) in view.calls
        assert ("render_now",) in view.calls

    def test_redact_repeatable_to_empty(self, tmp_cfg):
        chat, _ = self._run([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        chat2, _ = self._run(chat["messages"])
        assert chat2["messages"] == []
        # Redacting an already-empty chat must not raise.
        chat3, view3 = self._run([])
        assert chat3["messages"] == []
        assert view3.calls == []

    def test_redact_blocked_while_worker_active(self, tmp_cfg, monkeypatch):
        from pengy.ui import main_window as main_window_mod

        informed = []
        monkeypatch.setattr(
            main_window_mod.QMessageBox, "information",
            lambda *a, **k: informed.append(a) or QMessageBox.StandardButton.Ok,
        )
        chat, view = self._run([{"role": "user", "content": "hi"}], worker=object())

        assert chat["messages"] == [{"role": "user", "content": "hi"}]
        assert view.calls == []
        assert informed, "must warn the user instead of silently doing nothing"


# ────────────────────────────────────────────────────────────────────
# Cumulative token usage
# ────────────────────────────────────────────────────────────────────

class TestCumulativeTokens:
    """MainWindow._handle_final_response must accumulate chat['usage'] across
    turns rather than overwrite it with the last turn's numbers, and reflect
    that running total in the sidebar label — the same "how much context has
    this chat burned" signal that tells you when to /compact or redact.
    """

    class _RecordingView:
        def append_message(self, *a, **k):
            pass

    class _RecordingHistory:
        def __init__(self):
            self.calls = []

        def update_token_usage(self, prompt, completion):
            self.calls.append((prompt, completion))

    class _Session:
        def __init__(self, chat):
            self.chat = chat
            self.chat_view = TestCumulativeTokens._RecordingView()
            self.prompt_tokens = 0
            self.completion_tokens = 0

    class _FakeWindow:
        def __init__(self, session):
            self.active_chat_id = session.chat["id"]
            self.chat_history = TestCumulativeTokens._RecordingHistory()
            self._session = session

        def _tab_for_chat(self, chat_id):
            return self._session if chat_id == self.active_chat_id else None

    def test_accumulates_across_turns_and_updates_sidebar(self, tmp_cfg):
        from pengy.core.chat_manager import create_chat, save_chat, get_chat
        from pengy.ui.main_window import MainWindow

        chat = create_chat()
        save_chat(chat)
        session = self._Session(chat)
        window = self._FakeWindow(session)

        MainWindow._handle_final_response(window, session, {
            "content": "hi", "message": {"role": "assistant", "content": "hi"},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })
        assert session.chat["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        assert window.chat_history.calls[-1] == (10, 5)
        assert get_chat(chat["id"])["usage"]["total_tokens"] == 15

        MainWindow._handle_final_response(window, session, {
            "content": "again", "message": {"role": "assistant", "content": "again"},
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        })
        assert session.chat["usage"] == {"prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43}
        assert window.chat_history.calls[-1] == (30, 13)

    def test_reopening_a_chat_seeds_cumulative_display(self, tmp_cfg, qapp):
        """_add_tab must read chat['usage'] back in, not reset the tab to 0."""
        from pengy.core.chat_manager import create_chat, save_chat
        from pengy.ui.main_window import MainWindow

        chat = create_chat()
        chat["usage"] = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
        save_chat(chat)

        # _add_tab needs enough of a real MainWindow to build a ChatView and a
        # QTabWidget; construct just those pieces rather than the whole app.
        from PySide6.QtWidgets import QTabWidget
        from pengy.ui.theme import get_theme

        class _MinimalWindow:
            _theme = get_theme()
            tab_widget = QTabWidget()
            open_tabs = {}
            _render_message = MainWindow._render_message
            _install_tab_close_button = MainWindow._install_tab_close_button
            _save_open_tabs = lambda self: None

        window = _MinimalWindow()
        session = MainWindow._add_tab(window, chat, switch_to=False)
        assert session.prompt_tokens == 100
        assert session.completion_tokens == 40


# ────────────────────────────────────────────────────────────────────
# New Chat sidebar performance
# ────────────────────────────────────────────────────────────────────

class TestNewChatSidebarPerf:
    """create_new_chat() must not pay for a full sidebar rebuild.

    ChatHistoryWidget.load_chats() tears down and rebuilds every row (one
    QWidget with icon-bearing Save/Delete buttons per *existing* chat), which
    was previously called on every single "New Chat" click — the visible
    delay scaled with total chat count. add_chat() inserts just the one new
    row instead.
    """

    def test_add_chat_inserts_without_touching_existing_rows(self, qapp):
        sidebar = ChatHistoryWidget()
        try:
            sidebar.load_chats([
                {"id": "old1", "title": "Old One", "created_at": "", "msg_count": 0, "preview": ""},
                {"id": "old2", "title": "Old Two", "created_at": "", "msg_count": 0, "preview": ""},
            ])
            existing_widgets = [
                sidebar.chat_list.itemWidget(sidebar.chat_list.item(i))
                for i in range(sidebar.chat_list.count())
            ]

            sidebar.add_chat("new1", "Brand New Chat")

            assert sidebar.chat_list.count() == 3
            # The new chat lands at the top (chats sort newest-first)...
            assert sidebar.chat_list.item(0).data(Qt.ItemDataRole.UserRole) == "new1"
            # ...and the two pre-existing rows were never rebuilt.
            assert [
                sidebar.chat_list.itemWidget(sidebar.chat_list.item(i))
                for i in (1, 2)
            ] == existing_widgets
        finally:
            sidebar.deleteLater()

    def test_create_new_chat_uses_add_chat_not_full_reload(self, tmp_cfg, monkeypatch):
        """Locks in the fix: create_new_chat must call add_chat(), and must
        NOT call the full-rebuild load_chat_list() on this path."""
        from pengy.core.chat_manager import create_chat
        from pengy.ui.main_window import MainWindow

        add_chat_calls = []
        reload_calls = []

        class _FakeChatHistory:
            def add_chat(self, chat_id, title):
                add_chat_calls.append((chat_id, title))

            def select_chat_by_id(self, chat_id):
                pass

        class _FakeWindow:
            open_tabs = {}
            active_chat_id = None
            chat_history = _FakeChatHistory()

            def _add_tab(self, chat, switch_to=True):
                class _Session:
                    pass
                s = _Session()
                s.chat = chat
                return s

            def _update_quick_settings_for(self, session):
                pass

            def load_chat_list(self):
                reload_calls.append(True)

        monkeypatch.setattr("pengy.ui.main_window.create_chat", create_chat)
        window = _FakeWindow()
        MainWindow.create_new_chat(window)

        assert len(add_chat_calls) == 1
        assert reload_calls == []


# ────────────────────────────────────────────────────────────────────
# Closing an empty "New Chat" tab must drop its sidebar row too
# ────────────────────────────────────────────────────────────────────

class TestCloseEmptyNewChatSidebarSync:
    """Regression test for a real incident: create_new_chat() switched from a
    full sidebar rebuild (load_chat_list()) to an incremental add_chat() row
    insert. _close_tab() and _load_into_new_tab() were both already deleting
    an abandoned empty "New Chat" from disk without ever removing its sidebar
    row -- previously masked because the *next* create_new_chat() click
    happened to do a full disk-synced rebuild anyway. Once that rebuild was
    replaced with a targeted insert, the masking stopped: closing an empty
    "New Chat" tab and clicking New Chat again left a permanent ghost row
    behind, once per repeat -- "endless New Chats in history".
    """

    class _FakeWindow:
        """Just enough of MainWindow for _close_tab() to run for real."""

        def __init__(self, tab_widget, open_tabs, chat_history):
            self.tab_widget = tab_widget
            self.open_tabs = open_tabs
            self.chat_history = chat_history

        def _abandon_worker_for(self, session):
            pass

        def _save_open_tabs(self):
            pass

        def create_new_chat(self):
            pass  # only reached if the last tab closes; not exercised here

    def test_closing_empty_new_chat_removes_its_sidebar_row(self, tmp_cfg, qapp):
        from PySide6.QtWidgets import QTabWidget, QWidget
        from pengy.core.chat_manager import create_chat, get_chat
        from pengy.ui.main_window import MainWindow, _TabSession

        sidebar = ChatHistoryWidget()
        tab_widget = QTabWidget()

        # A real chat (stays open) plus a never-typed-into "New Chat" (about
        # to be closed) -- exactly create_new_chat()'s output shape.
        real_chat = create_chat()
        real_chat["title"] = "Real Chat"
        real_chat["messages"] = [{"role": "user", "content": "hi"}]
        blank_chat = create_chat()  # title defaults to "New Chat", no messages

        sidebar.add_chat(real_chat["id"], real_chat["title"])
        sidebar.add_chat(blank_chat["id"], blank_chat["title"])

        real_session = _TabSession(chat=real_chat, chat_view=QWidget())
        blank_session = _TabSession(chat=blank_chat, chat_view=QWidget())
        tab_widget.addTab(real_session.chat_view, "Real Chat")
        blank_index = tab_widget.addTab(blank_session.chat_view, "New Chat")

        open_tabs = {real_chat["id"]: real_session, blank_chat["id"]: blank_session}
        window = self._FakeWindow(tab_widget, open_tabs, sidebar)

        MainWindow._close_tab(window, blank_index)

        # Sidebar row for the closed blank chat is gone; the real chat's stays.
        assert sidebar.chat_list.count() == 1
        assert sidebar.chat_list.item(0).data(Qt.ItemDataRole.UserRole) == real_chat["id"]
        # And the underlying file is actually deleted, not just hidden.
        assert get_chat(blank_chat["id"]) is None

        sidebar.deleteLater()

    def test_repeated_close_and_recreate_does_not_accumulate_ghost_rows(self, tmp_cfg, qapp):
        """The literal reported symptom: close a blank New Chat, click New
        Chat again, repeat -- the sidebar must stay at one row, not grow."""
        from PySide6.QtWidgets import QTabWidget, QWidget
        from pengy.core.chat_manager import create_chat
        from pengy.ui.main_window import MainWindow, _TabSession

        sidebar = ChatHistoryWidget()
        tab_widget = QTabWidget()
        open_tabs = {}
        window = self._FakeWindow(tab_widget, open_tabs, sidebar)

        for _ in range(4):
            chat = create_chat()  # what create_new_chat() does
            sidebar.add_chat(chat["id"], chat.get("title", "New Chat"))
            session = _TabSession(chat=chat, chat_view=QWidget())
            index = tab_widget.addTab(session.chat_view, "New Chat")
            open_tabs[chat["id"]] = session

            MainWindow._close_tab(window, index)  # "click off it" (close)

        assert sidebar.chat_list.count() == 0
        assert open_tabs == {}

        sidebar.deleteLater()


# ────────────────────────────────────────────────────────────────────
# ChatWorker question-request plumbing
# ────────────────────────────────────────────────────────────────────

class TestChatWorkerQuestionEvent:
    """Regression test: ChatWorker.__init__ never set self._question_event
    (or self._pending_question_response), even though run(), cancel(), and
    send_question_response() all reference them. cancel() unconditionally
    calls self._question_event.set(), so this crashed on every worker
    cancellation; a model call to the ask_user_question tool crashed run()
    itself at self._question_event.clear(), which run()'s except Exception
    handler turned into exactly the reported 'Assistant: Error: ChatWorker
    object has no attribute _question_event' message.
    """

    class _FakeLLMClient:
        """A .chat() that mimics LLMClient's generator protocol just enough
        to drive ChatWorker.run() through a question_request round trip."""

        def chat(self, messages, tool_confirmation, **kwargs):
            answer = yield {"type": "question_request", "tool_call_id": "q1"}
            yield {"type": "final_response", "content": f"got:{answer}", "usage": {}}

    def test_worker_initializes_question_event(self, qapp):
        from pengy.ui.chat_worker import ChatWorker

        worker = ChatWorker(object(), messages=[])
        assert isinstance(worker._question_event, threading.Event)
        assert worker._pending_question_response is None

    def test_cancel_before_run_does_not_raise(self, qapp):
        """cancel() (Stop button, tab close, sending a new message mid-run)
        unconditionally touches _question_event even if no question was ever
        asked this turn."""
        from pengy.ui.chat_worker import ChatWorker

        worker = ChatWorker(object(), messages=[])
        worker.cancel()  # must not raise AttributeError

    def test_question_request_round_trip(self, qapp):
        """Runs synchronously (no background QThread) so the signal/slot
        connections are direct calls on this thread, exercising the exact
        clear()/wait()/set() sequence that crashed before the fix."""
        from pengy.ui.chat_worker import ChatWorker

        worker = ChatWorker(self._FakeLLMClient(), messages=[{"role": "user", "content": "hi"}])
        errors = []
        finals = []
        worker.error.connect(errors.append)
        worker.question_requested.connect(lambda q: worker.send_question_response({
            "answered": True, "tool_call_id": q["tool_call_id"], "answers": {"x": "y"},
        }))
        worker.response.connect(
            lambda r: finals.append(r) if r.get("type") == "final_response" else None
        )

        worker.run()

        assert errors == []
        assert len(finals) == 1
        assert "answered" in finals[0]["content"]
