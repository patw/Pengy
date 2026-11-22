"""Attached text files are capped against the model's context window.

Why the cap exists: attached text goes into the prompt verbatim, so an
uncapped 5 MB drop is ~1.3M tokens against a 256K window - a guaranteed
context-limit error, and reactive recovery then spends its 4 summarisation
attempts on a prompt that could never fit.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

from pengy.core import text_attach as ta
from pengy.ui.chat_input import ChatInputWidget


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_budget_is_a_quarter_of_the_context_window():
    # 256K is the fleet default; the rule must scale with the model, not be a
    # constant that is right for one window and wrong for the others.
    assert ta.embed_budget_bytes(256_000) == int(256_000 * ta.BYTES_PER_TOKEN * ta.CONTEXT_SHARE)
    assert ta.embed_budget_bytes(1_000_000) == int(1_000_000 * ta.BYTES_PER_TOKEN * ta.CONTEXT_SHARE)
    # Floors and ceilings: never under 32 KB, never over the file ceiling.
    assert ta.embed_budget_bytes(1) == ta.MIN_EMBED_BYTES
    assert ta.embed_budget_bytes(8_000_000) == ta.MAX_EMBED_BYTES
    # An unknown window (context_window: 0) is assumed 256K, not zero.
    assert ta.context_tokens_for({"context_window": 0}, "m") == ta.UNKNOWN_CONTEXT_TOKENS
    assert ta.context_tokens_for({"context_window": 131072}, "m") == 131072
    assert ta.context_tokens_for({"context_window": 0, "context_windows": {"m": 32768}}, "m") == 32768


def test_bytes_per_token_fudge_is_the_pessimistic_side():
    # Measured on the fleet engine: 4.13 bytes/token prose, 3.97 source.  The
    # conversion used for a safety limit must sit below the measured values.
    assert ta.BYTES_PER_TOKEN < 3.97


def test_read_text_attachment_caps_and_announces(tmp_path):
    small = tmp_path / "small.txt"
    small.write_bytes(b"hello")
    content, note = ta.read_text_attachment(small, 1024)
    assert content == "hello" and note == ""

    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (ta.MIN_EMBED_BYTES + 5000))
    content, note = ta.read_text_attachment(big, ta.MIN_EMBED_BYTES)
    assert len(content.encode()) == ta.MIN_EMBED_BYTES
    assert "truncated" in note and f"{len(big.read_bytes()):,}" in note
    assert content.startswith("xxxx")


def test_oversized_reason():
    assert ta.oversized_reason(ta.MAX_FILE_BYTES) is None
    assert ta.oversized_reason(ta.MAX_FILE_BYTES + 1)
    assert "MB" in ta.oversized_reason(5 * 1024 * 1024)


def test_bytes_variant_caps_the_same():
    data = b"y" * (ta.MIN_EMBED_BYTES + 1)
    content, note = ta.read_text_attachment_bytes(data, ta.MIN_EMBED_BYTES)
    assert len(content.encode()) == ta.MIN_EMBED_BYTES and "truncated" in note


def test_gui_drop_refuses_an_oversized_file(qapp, tmp_path, monkeypatch):
    """The refusal happens at attach time, naming the file, not at the endpoint."""
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (ta.MAX_FILE_BYTES + 1024))
    small = tmp_path / "small.txt"
    small.write_bytes(b"tiny")

    asked = []
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning",
                        lambda *a, **k: asked.append(a[2]))

    widget = ChatInputWidget()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(big)), QUrl.fromLocalFile(str(small))])
    event = QDropEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget._edit.dropEvent(event)

    assert [p.name for p in widget._attachments] == ["small.txt"], "oversized file not attached"
    assert any("big.txt" in m for m in asked), f"user was not told: {asked}"


def test_gui_embeds_a_large_file_truncated(qapp, tmp_path):
    """A file under the ceiling but over the budget goes in capped, with a note."""
    widget = ChatInputWidget()
    budget = ta.budget_for_model()
    mid = tmp_path / "mid.txt"
    mid.write_bytes(b"z" * (budget + 4096))
    widget._attachments = [mid]
    widget._edit.setPlainText("question")

    sent = []
    widget.message_sent.connect(lambda text, images: sent.append(text))
    widget._on_submit()

    assert len(sent) == 1
    body = sent[0]
    assert "[File: mid.txt]" in body and "truncated" in body
    # The embedded run is capped; the whole file is not in the prompt.
    assert body.count("z") <= budget
    assert "question" in body
