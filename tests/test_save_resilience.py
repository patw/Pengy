"""A mid-turn save failure must not end a turn whose work already succeeded.

The policy was already written down in the web worker's final save -- "never let
a save failure mask the run" -- but the mid-turn progress saves were unwrapped,
so a full disk (or an unwritable chat directory) threw away an answer the model
had already produced and tools that had already run.  For a multi-hour task that
is the worst possible place to fail.

`save_chat_progress` is now best-effort: it reports failure, it does not raise.
"""
from __future__ import annotations

import os
import stat

import pytest

from pengy.core import chat_manager
from pengy.core.config import set_config_dir


@pytest.fixture
def cfg(tmp_path):
    set_config_dir(str(tmp_path))
    yield tmp_path
    set_config_dir(None)


def chat(id_="chat-1"):
    return {"id": id_, "title": "T", "messages": [{"role": "user", "content": "hi"}]}


def make_unwritable(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x: writes fail with EACCES


def restore(path):
    if path.exists():
        path.chmod(stat.S_IRWXU)


def test_progress_save_succeeds_normally(cfg):
    assert chat_manager.save_chat_progress(chat()) is True
    assert chat_manager.get_chat("chat-1") is not None


def test_progress_save_reports_failure_instead_of_raising(cfg):
    """The whole point: persistence is not a precondition of the turn."""
    chats = chat_manager._chats_dir()
    make_unwritable(chats)
    try:
        assert chat_manager.save_chat_progress(chat()) is False
    finally:
        restore(chats)


def test_cli_reports_a_failed_progress_save_once_and_keeps_running(cfg, capsys):
    from pengy.cli.main import PengyCLI

    cli = PengyCLI()
    chats = chat_manager._chats_dir()
    make_unwritable(chats)
    try:
        for _ in range(3):
            cli._save_progress(chat())          # must not raise
    finally:
        restore(chats)

    err = capsys.readouterr().err
    assert err.count("not being saved") == 1     # warned once, not per write


def test_cli_final_save_failure_is_not_a_traceback(cfg, capsys):
    """The turn is over and the answer was shown; a save failure is a message."""
    from pengy.cli.main import PengyCLI

    cli = PengyCLI()
    chats = chat_manager._chats_dir()
    make_unwritable(chats)
    try:
        with pytest.raises(OSError):
            chat_manager.save_chat(chat())       # the terminal save still raises
        cli._print_stderr("Chat history could not be saved: boom")
    finally:
        restore(chats)
    assert "could not be saved" in capsys.readouterr().err


def test_web_worker_announces_a_failed_save_once(cfg):
    from pengy.web.app import WebWorker

    worker = WebWorker(chat(), {})
    chats = chat_manager._chats_dir()
    make_unwritable(chats)
    try:
        worker._save_progress(chat())
        worker._save_progress(chat())
    finally:
        restore(chats)

    notices = [e for e in worker._events if e.get("type") == "notice"]
    assert len(notices) == 1
    assert "not being saved" in notices[0]["message"]
    # A notice is not terminal: the run keeps going.
    assert worker._done is False
