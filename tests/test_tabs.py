"""Tests for the tabbed-chat MainWindow.

Run with:  python -m pytest tests/test_tabs.py -v
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTabBar

from pengy.ui.main_window import MainWindow, _TabSession


# ── fixtures ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tmp_cfg():
    from pengy.core.config import set_config_dir

    with tempfile.TemporaryDirectory(prefix="pengy-tabtest-") as cfg_dir:
        set_config_dir(cfg_dir)
        yield Path(cfg_dir)
    set_config_dir(None)


@pytest.fixture
def window(qapp, tmp_cfg, monkeypatch):
    """A fresh MainWindow pointed at a temp config directory, no real LLM."""
    from pengy.ui import main_window as mw_mod
    monkeypatch.setattr(mw_mod.LLMClient, "chat", lambda *a, **kw: (_ for _ in ()))
    monkeypatch.setattr(mw_mod.LLMClient, "__init__", lambda self, *a, **kw: None)

    w = MainWindow()
    qapp.processEvents()
    yield w
    # Clean teardown
    for cid, session in list(w.open_tabs.items()):
        if session.chat:
            from pengy.core.chat_manager import save_chat
            save_chat(session.chat)
    w.open_tabs.clear()
    while w.tab_widget.count() > 0:
        w.tab_widget.removeTab(0)
    w.close()
    qapp.processEvents()


# ── helpers ─────────────────────────────────────────────────────────

def _tab_count(window: MainWindow) -> int:
    return window.tab_widget.count()


def _active_session(window: MainWindow) -> _TabSession | None:
    return window._tab_for_chat(window.active_chat_id) if window.active_chat_id else None


def _dirty_chat(session: _TabSession):
    """Make a chat non-empty so create_new_chat creates a real new tab."""
    session.chat["messages"] = [{"role": "user", "content": "hi"}]
    session.chat["title"] = "Dirty Chat"
    from pengy.core.chat_manager import save_chat
    save_chat(session.chat)


# ── tests ───────────────────────────────────────────────────────────

class TestTabLifecycle:
    def test_starts_with_one_tab(self, window):
        assert _tab_count(window) == 1
        assert window.active_chat_id is not None

    def test_new_chat_adds_tab(self, window):
        """create_new_chat reuses an empty 'New Chat', so dirty it first."""
        _dirty_chat(_active_session(window))
        before = _tab_count(window)
        window.create_new_chat()
        assert _tab_count(window) == before + 1

    def test_close_tab_removes_it(self, window):
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        second_id = window.active_chat_id
        before = _tab_count(window)

        for i in range(window.tab_widget.count()):
            if window.tab_widget.widget(i) is window.open_tabs[second_id].chat_view:
                window._close_tab(i)
                break

        assert _tab_count(window) == before - 1
        assert second_id not in window.open_tabs

    def test_empty_new_chat_tab_is_reused(self, window):
        """Loading a sidebar chat should replace an empty 'New Chat' tab."""
        assert _tab_count(window) == 1
        only_id = next(iter(window.open_tabs))
        assert window.open_tabs[only_id].chat["title"] == "New Chat"
        assert not window.open_tabs[only_id].chat.get("messages")

        from pengy.core.chat_manager import create_chat, save_chat
        chat = create_chat()
        chat["title"] = "Real Chat"
        chat["messages"] = [{"role": "user", "content": "hello"}]
        save_chat(chat)

        window._load_into_new_tab(chat["id"])
        assert _tab_count(window) == 1
        assert window.active_chat_id == chat["id"]
        assert only_id not in window.open_tabs

    def test_close_last_tab_creates_new(self, window):
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        while _tab_count(window) > 1:
            window._close_tab(0)

        window._close_tab(0)
        assert _tab_count(window) >= 1
        assert window.active_chat_id is not None


class TestTabAppearance:
    def test_tabs_use_natural_width(self, window):
        assert not window.tab_widget.tabBar().expanding()

    def test_close_button_is_custom_and_right_aligned(self, window):
        bar = window.tab_widget.tabBar()
        right = bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
        left = bar.tabButton(0, QTabBar.ButtonPosition.LeftSide)
        assert right is not None
        assert right.property("pengyIcon") == "close"
        assert left is None

    def test_close_button_tracks_tab_after_reordering(self, window):
        _dirty_chat(_active_session(window))
        first_widget = window.tab_widget.widget(0)
        window.create_new_chat()
        bar = window.tab_widget.tabBar()
        bar.moveTab(0, 1)
        moved_index = window.tab_widget.indexOf(first_widget)
        close_button = bar.tabButton(moved_index, QTabBar.ButtonPosition.RightSide)
        before = window.tab_widget.count()
        close_button.click()
        assert window.tab_widget.count() == before - 1
        assert window.tab_widget.indexOf(first_widget) == -1


class TestTabIndependence:
    def test_tabs_have_separate_chat_views(self, window):
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        assert _tab_count(window) == 2
        views = {s.chat_view for s in window.open_tabs.values()}
        assert len(views) == 2

    def test_tabs_have_separate_chat_data(self, window):
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        chats = {s.chat["id"] for s in window.open_tabs.values()}
        assert len(chats) == 2

    def test_switching_tabs_updates_active_id(self, window):
        first_id = window.active_chat_id
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        second_id = window.active_chat_id
        assert second_id != first_id

        # Switch back to first tab
        for i in range(window.tab_widget.count()):
            if window.tab_widget.widget(i) is window.open_tabs[first_id].chat_view:
                window.tab_widget.setCurrentIndex(i)
                break
        assert window.active_chat_id == first_id


class TestSidebarInteraction:
    def test_clicking_open_chat_switches_tab(self, window):
        first_id = window.active_chat_id
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        second_id = window.active_chat_id

        window._on_chat_selected(first_id)
        assert window.active_chat_id == first_id
        assert _tab_count(window) == 2

    def test_clicking_closed_chat_opens_tab(self, window):
        from pengy.core.chat_manager import create_chat, save_chat
        chat = create_chat()
        chat["title"] = "External Chat"
        chat["messages"] = [{"role": "user", "content": "hi"}]
        save_chat(chat)
        window.load_chat_list()

        # Dirty the empty New Chat so it doesn't get replaced
        _dirty_chat(_active_session(window))
        before = _tab_count(window)

        window._on_chat_selected(chat["id"])
        assert _tab_count(window) == before + 1
        assert window.active_chat_id == chat["id"]


class TestTabRestore:
    def test_open_tabs_persisted_to_config(self, window):
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        saved = window.config.get("open_tabs", [])
        assert len(saved) == 2
        assert all(cid in window.open_tabs for cid in saved)

    def test_open_tabs_restored_on_startup(self, qapp, tmp_cfg, monkeypatch):
        from pengy.ui import main_window as mw_mod
        monkeypatch.setattr(mw_mod.LLMClient, "chat", lambda *a, **kw: (_ for _ in ()))
        monkeypatch.setattr(mw_mod.LLMClient, "__init__", lambda self, *a, **kw: None)

        w1 = MainWindow()
        qapp.processEvents()
        _dirty_chat(_active_session(w1))
        w1.create_new_chat()
        ids_before = list(w1.open_tabs.keys())
        w1.close()
        qapp.processEvents()

        w2 = MainWindow()
        qapp.processEvents()
        assert _tab_count(w2) == 2
        assert set(w2.open_tabs.keys()) == set(ids_before)

        # cleanup w2
        for cid in list(w2.open_tabs):
            from pengy.core.chat_manager import save_chat, delete_chat
            save_chat(w2.open_tabs[cid].chat)
        w2.open_tabs.clear()
        while w2.tab_widget.count() > 0:
            w2.tab_widget.removeTab(0)
        w2.close()
        qapp.processEvents()


class TestRuntimeScale:
    def test_saved_scale_waits_for_restart_but_theme_changes_live(self, window):
        startup_scale = window._runtime_ui_scale
        window.config["ui_scale"] = 200 if startup_scale != 200 else 75
        window.config["theme_mode"] = "dark"
        window.apply_theme()
        assert float(window._theme["ui_scale"]) == float(startup_scale)
        assert window._theme["mode"] == "dark"


class TestStopButton:
    def test_stop_clears_thinking_flag(self, window):
        session = _active_session(window)
        assert session is not None
        session.thinking = True

        window._stop_worker()

        assert not session.thinking

    def test_stop_button_hidden_initially(self, window):
        """Stop button is hidden when no worker is running."""
        assert not window._stop_btn.isVisible()


class TestQuickSettings:
    def test_updates_on_tab_switch(self, window):
        first_id = window.active_chat_id
        _dirty_chat(_active_session(window))
        window.create_new_chat()
        second_id = window.active_chat_id

        window.open_tabs[first_id].prompt_tokens = 100
        window.open_tabs[first_id].completion_tokens = 50
        window.open_tabs[second_id].prompt_tokens = 200
        window.open_tabs[second_id].completion_tokens = 100

        # Find the index of the first tab and switch to it
        for i in range(window.tab_widget.count()):
            if window.tab_widget.widget(i) is window.open_tabs[first_id].chat_view:
                window.tab_widget.setCurrentIndex(i)
                break

        assert "100" in window.chat_history.tokens_label.text()
        assert "50" in window.chat_history.tokens_label.text()


class TestCloseEvent:
    def test_close_cancels_running_workers(self, window, qapp):
        """closeEvent must cancel live workers so no QThread is destroyed mid-run."""
        from PySide6.QtGui import QCloseEvent

        class _FakeWorker:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        class _FakeThread:
            def isRunning(self):
                return False  # nothing to actually wait on

            def quit(self):
                pass

            def wait(self, ms):
                return True

        session = _active_session(window)
        worker = _FakeWorker()
        session.worker = worker
        session.worker_thread = _FakeThread()

        # Also stage an abandoned worker to prove it is cancelled too.
        abandoned = _FakeWorker()
        window._abandoned_workers.append((_FakeThread(), abandoned))

        window.closeEvent(QCloseEvent())

        assert worker.cancelled
        assert abandoned.cancelled

class TestQuestionDialog:
    def test_dialog_creation(self, window, qapp):
        """QuestionDialog can be created and shows questions."""
        from pengy.ui.main_window import QuestionDialog
        questions = [
            {
                "header": "Auth",
                "question": "Which auth method?",
                "options": [
                    {"label": "JWT", "description": "Stateless tokens"},
                    {"label": "Session", "description": "Cookie-based"},
                ],
            },
        ]
        dlg = QuestionDialog("Test Tab", questions, window._theme, window)
        assert dlg.answers == []
        assert dlg._button_groups
        assert len(dlg._button_groups) == 1

    def test_dialog_multi_question(self, window, qapp):
        """QuestionDialog with multiple questions creates multiple button groups."""
        from pengy.ui.main_window import QuestionDialog
        questions = [
            {"header": "Q1", "question": "First?", "options": [{"label": "A", "description": "a"}]},
            {"header": "Q2", "question": "Second?", "options": [{"label": "B", "description": "b"}]},
        ]
        dlg = QuestionDialog("Test", questions, window._theme, window)
        assert len(dlg._button_groups) == 2

    def test_dialog_first_option_checked_by_default(self, window, qapp):
        """First radio button in each group should be checked by default."""
        from pengy.ui.main_window import QuestionDialog
        questions = [
            {"header": "Q", "question": "?", "options": [
                {"label": "First", "description": "f"},
                {"label": "Second", "description": "s"},
            ]},
        ]
        dlg = QuestionDialog("Test", questions, window._theme, window)
        # First option auto-checked
        assert dlg._button_groups[0].checkedButton() is not None
        assert "First" in dlg._button_groups[0].checkedButton().text()

    def test_question_queuing_state(self, window):
        """MainWindow has question queuing state initialized."""
        assert hasattr(window, '_pending_questions')
        assert hasattr(window, '_question_dialog_open')
        assert window._pending_questions == []
        assert window._question_dialog_open is False

