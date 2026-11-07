"""Tests for Edit Chat: title generation (core), and its GUI/web/CLI surfaces.

Covers ``pengy.core.chat_title`` (source text, reply cleanup, the request it
makes), the web ``/title/generate`` route, ``/title`` slash command and the
sidebar edit button, the CLI ``/title`` command, and the Qt sidebar's Edit
button plus ``EditChatDialog``.

Run with:  python -m pytest tests/test_chat_title.py -v
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pengy.core import chat_title
from pengy.core.chat_title import clean_title, generate_title, title_source
from pengy.core.config import get_config_dir, load_config, save_config, set_config_dir
from pengy.core.chat_manager import create_chat, get_chat, save_chat


@pytest.fixture
def tmp_cfg():
    with tempfile.TemporaryDirectory(prefix="pengy-title-") as cfg_dir:
        set_config_dir(cfg_dir)
        assert get_config_dir() != Path.home() / ".config" / "pengy"
        config = load_config()
        config["base_url"] = "http://127.0.0.1:9/v1"
        config["api_key"] = "test"
        config["model"] = "test-model"
        save_config(config)
        yield Path(cfg_dir)
    set_config_dir(None)


def make_chat(messages=None, title="Old Title"):
    chat = create_chat()
    chat["title"] = title
    chat["messages"] = messages if messages is not None else [
        {"role": "user", "content": "How do I mount an NFS share at boot?"},
        {"role": "assistant", "content": "Add a line to /etc/fstab."},
    ]
    save_chat(chat)
    return chat


class _FakeClient:
    """Stands in for LLMClient; records the completion request."""

    reply = "NFS Mount at Boot"
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.base_url = kwargs.get("base_url", "")
        self.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=self._create)))

    def _create(self, **kwargs):
        _FakeClient.calls.append(kwargs)
        message = SimpleNamespace(content=_FakeClient.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def fake_llm(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.reply = "NFS Mount at Boot"
    monkeypatch.setattr(chat_title, "LLMClient", _FakeClient)
    return _FakeClient


# ── core ────────────────────────────────────────────────────────────────────────

class TestCleanTitle:
    @pytest.mark.parametrize("raw, expected", [
        ("NFS Mount at Boot", "NFS Mount at Boot"),
        ('"NFS Mount at Boot."', "NFS Mount at Boot"),
        ("Title: NFS Mount at Boot", "NFS Mount at Boot"),
        ("**NFS Mount at Boot**", "NFS Mount at Boot"),
        ("\n\n  NFS Mount\nsecond line", "NFS Mount"),
        ("<think>hmm, about NFS</think>\nNFS Mount", "NFS Mount"),
        ("<think>never finished", ""),
        ("NFS\x1b[31m Mount", "NFS[31m Mount"),
        ("", ""),
    ])
    def test_cleanup(self, raw, expected):
        assert clean_title(raw) == expected

    def test_long_reply_is_capped(self):
        title = clean_title("word " * 100)
        assert len(title) <= chat_title.TITLE_MAX_CHARS
        assert title.endswith("...")


class TestTitleSource:
    def test_skips_tool_traffic_and_reads_list_content(self):
        chat = {"messages": [
            {"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "SECRET TOOL OUTPUT"},
            {"role": "assistant", "content": "done"},
        ]}
        source = title_source(chat)
        assert source == "User: look\n\nAssistant: done"

    def test_truncated(self):
        chat = {"messages": [{"role": "user", "content": "x" * 50000}]}
        assert len(title_source(chat)) == chat_title.SOURCE_MAX_CHARS


class TestGenerateTitle:
    def test_sends_conversation_and_cleans_reply(self, fake_llm):
        fake_llm.reply = '"NFS Mount at Boot."'
        chat = {"messages": [{"role": "user", "content": "mount nfs"}]}
        title = generate_title({"model": "m", "base_url": "http://x/v1"}, chat)
        assert title == "NFS Mount at Boot"
        (call,) = fake_llm.calls
        assert call["model"] == "m"
        assert call["messages"][0]["role"] == "system"
        assert call["messages"][1] == {"role": "user", "content": "User: mount nfs"}
        assert "tools" not in call

    def test_thinking_off_follows_the_model_setting(self, fake_llm):
        config = {"model": "m", "empty_reply_thinking_off_models": {"m": "enable_thinking"}}
        generate_title(config, {"messages": [{"role": "user", "content": "hi"}]})
        assert fake_llm.calls[0]["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}}

    def test_empty_chat_raises_without_calling(self, fake_llm):
        with pytest.raises(ValueError, match="no messages"):
            generate_title({"model": "m"}, {"messages": []})
        assert fake_llm.calls == []

    def test_empty_reply_raises(self, fake_llm):
        fake_llm.reply = "<think>...</think>"
        with pytest.raises(ValueError, match="empty title"):
            generate_title({"model": "m"}, {"messages": [{"role": "user", "content": "hi"}]})


# ── web ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_cfg):
    from pengy.web.app import app
    return app.test_client()


class TestWebEditChat:
    def test_generate_returns_title_without_saving(self, client, fake_llm):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/title/generate")
        assert resp.status_code == 200
        assert resp.get_json() == {"title": "NFS Mount at Boot"}
        assert get_chat(chat["id"])["title"] == "Old Title"
        assert fake_llm.calls[0]["model"] == "test-model"

    def test_generate_empty_chat_400(self, client, fake_llm):
        chat = make_chat(messages=[])
        resp = client.post(f"/chat/{chat['id']}/title/generate")
        assert resp.status_code == 400
        assert "no messages" in resp.get_json()["error"]

    def test_generate_unknown_chat_404(self, client):
        assert client.post("/chat/nope/title/generate").status_code == 404

    def test_generate_api_failure_502(self, client, fake_llm, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("endpoint down")
        monkeypatch.setattr(fake_llm, "_create", staticmethod(boom))
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/title/generate")
        assert resp.status_code == 502
        assert "endpoint down" in resp.get_json()["error"]

    def test_title_command_renames(self, client, fake_llm):
        chat = make_chat()
        resp = client.post(f"/chat/{chat['id']}/command", json={"command": "/title"})
        assert resp.get_json() == {"type": "rename", "title": "NFS Mount at Boot"}
        assert get_chat(chat["id"])["title"] == "NFS Mount at Boot"

    def test_sidebar_rows_have_edit_button_and_modal(self, client):
        chat = make_chat()
        other = make_chat(title="Other")
        html = client.get(f"/chat/{chat['id']}").get_data(as_text=True)
        assert f"openEditChat('{other['id']}')" in html
        assert 'id="generateTitleBtn"' in html
        assert 'id="editChatExport"' in html


# ── CLI ─────────────────────────────────────────────────────────────────────────

class TestCliTitle:
    def _cli(self, chat):
        from pengy.cli.main import PengyCLI
        cli = PengyCLI(no_save=True)
        cli.current_chat = chat
        return cli

    def test_title_renames_and_saves(self, tmp_cfg, fake_llm, capsys):
        chat = make_chat()
        cli = self._cli(chat)
        cli._handle_slash("/title")
        assert chat["title"] == "NFS Mount at Boot"
        assert get_chat(chat["id"])["title"] == "NFS Mount at Boot"
        assert "Renamed" in capsys.readouterr().out

    def test_failure_keeps_title(self, tmp_cfg, fake_llm, capsys):
        chat = make_chat(messages=[])
        cli = self._cli(chat)
        cli._handle_slash("/title")
        assert get_chat(chat["id"])["title"] == "Old Title"
        assert "Couldn't generate a title" in capsys.readouterr().out


# ── Qt ──────────────────────────────────────────────────────────────────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="PySide6 not installed")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


class TestQtEditChat:
    def test_sidebar_row_has_edit_not_save(self, tmp_cfg, qapp):
        from PySide6.QtWidgets import QPushButton
        from pengy.ui.chat_history import ChatHistoryWidget

        widget = ChatHistoryWidget()
        widget.add_chat("abc", "A chat")
        row = widget.chat_list.itemWidget(widget.chat_list.item(0))
        icons = [b.property("pengyIcon") for b in row.findChildren(QPushButton)]
        assert icons == ["edit", "delete"]

        requested = []
        widget.edit_requested.connect(requested.append)
        row.findChildren(QPushButton)[0].click()
        assert requested == ["abc"]
        widget.deleteLater()

    def test_dialog_generate_fills_field(self, tmp_cfg, qapp, fake_llm):
        from pengy.ui.edit_chat_dialog import EditChatDialog

        chat = make_chat()
        dialog = EditChatDialog(chat, load_config())
        assert dialog.title() == "Old Title"
        dialog._generate()
        for _ in range(200):
            qapp.processEvents()
            if dialog.generate_btn.isEnabled():
                break
            import time
            time.sleep(0.01)
        assert dialog.title() == "NFS Mount at Boot"
        # Generating only fills the field; nothing is saved until Save.
        assert get_chat(chat["id"])["title"] == "Old Title"
        dialog.deleteLater()

    def test_chat_markdown(self):
        pytest.importorskip("PySide6", reason="PySide6 not installed")
        from pengy.ui.edit_chat_dialog import chat_markdown

        md = chat_markdown({"title": "T", "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [{}]},
            {"role": "tool", "content": "out"},
            {"role": "assistant", "content": "a"},
        ]})
        assert md.startswith("# T")
        assert "**You**" in md and "q" in md and "a" in md
        assert "out" not in md
        assert md.count("**Assistant**") == 1


# ── Bulk rename ─────────────────────────────────────────────────────────────────

class TestSetChatTitle:
    def test_changes_only_the_title_of_the_current_disk_copy(self, tmp_cfg):
        from pengy.core.chat_manager import set_chat_title

        chat = make_chat()
        stale = get_chat(chat["id"])
        chat["messages"].append({"role": "user", "content": "added elsewhere"})
        save_chat(chat)
        assert set_chat_title(stale["id"], "New")
        on_disk = get_chat(chat["id"])
        assert on_disk["title"] == "New"
        assert on_disk["messages"][-1]["content"] == "added elsewhere"

    def test_missing_chat(self, tmp_cfg):
        from pengy.core.chat_manager import set_chat_title
        assert set_chat_title("nope", "x") is False


def _run_worker(qapp, worker):
    import time
    done = []
    worker.finished.connect(done.append)
    worker.start()
    for _ in range(500):
        qapp.processEvents()
        if done:
            return done[0]
        time.sleep(0.01)
    raise AssertionError("bulk rename did not finish")


class TestBulkRename:
    def test_renames_every_chat_with_messages(self, tmp_cfg, qapp, fake_llm):
        from pengy.ui.bulk_rename import BulkRenameWorker, bulk_rename_targets

        a, b = make_chat(title="A"), make_chat(title="B")
        empty = make_chat(messages=[], title="New Chat")
        ids = bulk_rename_targets()
        assert set(ids) == {a["id"], b["id"]}

        renamed, progress = [], []
        worker = BulkRenameWorker(load_config(), ids)
        worker.renamed.connect(lambda cid, t: renamed.append(cid))
        worker.progress.connect(lambda d, t, title: progress.append((d, t)))
        summary = _run_worker(qapp, worker)

        assert summary["renamed"] == 2 and summary["failed"] == 0
        assert set(renamed) == {a["id"], b["id"]}
        assert progress[-1] == (2, 2)
        assert get_chat(a["id"])["title"] == "NFS Mount at Boot"
        assert get_chat(empty["id"])["title"] == "New Chat"

    def test_aborts_after_repeated_failures(self, tmp_cfg, qapp, fake_llm, monkeypatch):
        from pengy.ui import bulk_rename

        def boom(**kwargs):
            raise RuntimeError("endpoint down")
        monkeypatch.setattr(fake_llm, "_create", staticmethod(boom))
        ids = [make_chat(title=f"C{i}")["id"] for i in range(bulk_rename.MAX_CONSECUTIVE_FAILURES + 3)]
        summary = _run_worker(qapp, bulk_rename.BulkRenameWorker(load_config(), ids))
        assert summary["aborted"]
        assert summary["failed"] == bulk_rename.MAX_CONSECUTIVE_FAILURES
        assert "endpoint down" in bulk_rename.summary_text(summary)
        assert all(get_chat(i)["title"].startswith("C") for i in ids)

    def test_cancel_stops_before_the_next_chat(self, tmp_cfg, qapp, fake_llm):
        from pengy.ui.bulk_rename import BulkRenameWorker

        ids = [make_chat(title=f"C{i}")["id"] for i in range(3)]
        worker = BulkRenameWorker(load_config(), ids)
        worker.cancel()
        summary = _run_worker(qapp, worker)
        assert summary["cancelled"] and summary["renamed"] == 0

    def test_settings_ui_tab_has_the_button(self, tmp_cfg, qapp):
        from pengy.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(load_config())
        fired = []
        dialog.bulk_rename_requested.connect(lambda: fired.append(True))
        dialog.bulk_rename_btn.click()
        assert fired == [True]
        dialog.deleteLater()

    def test_confirm_no_model(self, tmp_cfg, qapp, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        from pengy.ui import bulk_rename

        asked = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: asked.append(a[2]))
        assert bulk_rename.confirm_bulk_rename(None, {"model": ""}) is None
        assert "No default model" in asked[-1]

    @pytest.mark.parametrize("answer, tick, expected", [
        ("Cancel", False, None), ("Yes", False, False), ("Yes", True, True),
    ])
    def test_confirm_returns_redo_choice(self, tmp_cfg, qapp, monkeypatch, answer, tick, expected):
        from PySide6.QtWidgets import QMessageBox
        from pengy.ui import bulk_rename

        make_chat()
        make_chat()
        shown = []

        def fake_exec(box):
            shown.append(box.text())
            box.checkBox().setChecked(tick)
            return getattr(QMessageBox.StandardButton, answer)
        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        result = bulk_rename.confirm_bulk_rename(None, {"model": "m", "base_url": "u"})
        assert result is expected
        assert "m at u" in shown[0] and "2 chats still have their automatic title" in shown[0]


class TestTitleSource:
    def _run(self, qapp, redo=False):
        from pengy.ui.bulk_rename import BulkRenameWorker, bulk_rename_targets
        return _run_worker(qapp, BulkRenameWorker(load_config(), bulk_rename_targets(redo), redo))

    def test_bulk_marks_and_a_second_run_resumes(self, tmp_cfg, qapp, fake_llm):
        from pengy.ui.bulk_rename import bulk_rename_targets

        a, b = make_chat(title="A"), make_chat(title="B")
        assert self._run(qapp)["renamed"] == 2
        assert get_chat(a["id"])["title_source"] == "model"
        assert bulk_rename_targets() == []

        c = make_chat(title="C")  # new since the last run
        assert bulk_rename_targets() == [c["id"]]
        fake_llm.calls = []
        assert self._run(qapp)["renamed"] == 1
        assert len(fake_llm.calls) == 1

    def test_unchanged_title_is_still_marked(self, tmp_cfg, qapp, fake_llm):
        chat = make_chat(title="NFS Mount at Boot")
        summary = self._run(qapp)
        assert summary["unchanged"] == 1
        assert get_chat(chat["id"])["title_source"] == "model"

    def test_user_titles_are_never_renamed(self, tmp_cfg, qapp, fake_llm):
        from pengy.ui.bulk_rename import bulk_rename_targets

        mine = make_chat(title="Mine")
        mine["title_source"] = "user"
        save_chat(mine)
        model = make_chat(title="Model")
        model["title_source"] = "model"
        save_chat(model)
        assert bulk_rename_targets() == []
        assert bulk_rename_targets(redo=True) == [model["id"]]
        self._run(qapp, redo=True)
        assert get_chat(mine["id"])["title"] == "Mine"
        assert get_chat(model["id"])["title"] == "NFS Mount at Boot"

    def test_stale_index_entry_falls_back_to_the_file(self, tmp_cfg, qapp, fake_llm):
        """An index entry without title_source (old index, other edition) is
        queued, but the chat file's own marker still protects it."""
        import json
        from pengy.core import chat_manager

        mine = make_chat(title="Mine")
        mine["title_source"] = "user"
        save_chat(mine)
        index = json.loads(chat_manager._index_path().read_text())
        for entry in index["chats"]:
            entry.pop("title_source", None)
        chat_manager._index_path().write_text(json.dumps(index))

        summary = self._run(qapp)
        assert summary["kept"] == 1 and fake_llm.calls == []
        assert get_chat(mine["id"])["title"] == "Mine"

    def test_save_chat_progress_keeps_a_mid_run_title_source(self, tmp_cfg):
        from pengy.core.chat_manager import save_chat_progress, set_chat_title

        chat = make_chat()
        snapshot = get_chat(chat["id"])
        set_chat_title(chat["id"], "Renamed", "user")
        snapshot["messages"].append({"role": "assistant", "content": "more"})
        save_chat_progress(snapshot)
        on_disk = get_chat(chat["id"])
        assert (on_disk["title"], on_disk["title_source"]) == ("Renamed", "user")
        assert on_disk["messages"][-1]["content"] == "more"

    def test_web_rename_sources(self, client):
        chat = make_chat()
        client.post(f"/chat/{chat['id']}/rename", json={"title": "Typed"})
        assert get_chat(chat["id"])["title_source"] == "user"
        client.post(f"/chat/{chat['id']}/rename", json={"title": "Gen", "source": "model"})
        assert get_chat(chat["id"])["title_source"] == "model"
        client.post(f"/chat/{chat['id']}/rename", json={"title": "X", "source": "bogus"})
        assert get_chat(chat["id"])["title_source"] == "user"
        client.post(f"/chat/{chat['id']}/command", json={"command": "/rename Hand"})
        assert get_chat(chat["id"])["title_source"] == "user"

    def test_web_title_command_marks_model(self, client, fake_llm):
        chat = make_chat()
        client.post(f"/chat/{chat['id']}/command", json={"command": "/title"})
        assert get_chat(chat["id"])["title_source"] == "model"

    def test_cli_sources(self, tmp_cfg, fake_llm):
        from pengy.cli.main import PengyCLI

        chat = make_chat()
        cli = PengyCLI(no_save=True)
        cli.current_chat = chat
        cli._handle_slash("/title")
        assert get_chat(chat["id"])["title_source"] == "model"
        cli._handle_slash("/rename By Hand")
        assert get_chat(chat["id"])["title_source"] == "user"

    def test_dialog_source(self, tmp_cfg, qapp):
        from pengy.ui.edit_chat_dialog import EditChatDialog

        dialog = EditChatDialog({"title": "Auto", "messages": []}, {})
        assert dialog.title_source() is None          # untouched: keep (automatic)
        dialog._on_generated("Generated")
        assert dialog.title_source() == "model"
        dialog.title_input.setText("Generated, edited")
        assert dialog.title_source() == "user"
        dialog.deleteLater()
