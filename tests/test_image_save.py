"""Saving chat images from the Qt context menu uses full-size, available bytes."""
import base64

import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QContextMenuEvent, QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu

from pengy.ui.chat_view import ChatView
from pengy.ui.image_save import image_bytes, save_image_as


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_save_image_preserves_bytes_and_uses_display_derivative(qapp, tmp_path, monkeypatch):
    thumb = tmp_path / "thumbnail-256-v1.jpg"
    display = tmp_path / "image-display-v1.jpg"
    assert QImage(100, 50, QImage.Format.Format_RGB32).save(str(thumb))
    assert QImage(900, 450, QImage.Format.Format_RGB32).save(str(display))
    saved = tmp_path / "saved.jpg"
    names = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (names.append(args[2]) or str(saved), ""))
    assert save_image_as(thumb.as_uri(), {}, None)
    assert saved.read_bytes() == display.read_bytes()
    assert names == ["image-display-v1.jpg"]

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    assert not save_image_as(thumb.as_uri(), {}, None)
    assert saved.read_bytes() == display.read_bytes()


def test_save_remote_uses_cache_and_data_uri(qapp, tmp_path):
    image = tmp_path / "source.png"
    assert QImage(10, 10, QImage.Format.Format_RGB32).save(str(image))
    data = image.read_bytes()
    remote = "https://example.org/photo?download=1"
    assert image_bytes(remote, {remote: data}) == (data, "photo.png")
    assert image_bytes(remote, {}) is None  # no network access on right-click
    uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    assert image_bytes(uri, {}) == (data, "image.png")
    assert image_bytes("data:image/png;base64,broken!", {}) is None


def test_right_click_only_over_image_offers_save(qapp, tmp_path, monkeypatch):
    source = tmp_path / "wide.png"
    assert QImage(1000, 500, QImage.Format.Format_RGB32).save(str(source))
    view = ChatView()
    view.resize(700, 600)
    view.show()
    view.append_message("assistant", f"![Picture]({source.as_uri()})")
    qapp.processEvents()
    selected = []

    # Shiboken resolves QMenu.exec on instances as a C++ method, bypassing
    # monkeypatching the class. Replace QMenu in both modules instead.
    class FakeMenu:
        def __init__(self, *_):
            self.actions = []

        def addAction(self, label):
            self.actions.append(label)
            return label

        def exec(self, *_):
            selected.append(list(self.actions))
            return self.actions[0]

    monkeypatch.setattr("pengy.ui.chat_view.QMenu", FakeMenu)
    monkeypatch.setattr("pengy.ui.image_preview.QMenu", FakeMenu)
    destination = tmp_path / "output.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(destination), ""))
    assert view._image_at(QPoint(100, 60)) == source.as_uri()
    view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(100, 60)))
    assert selected == [["Save Image As…"]]
    assert destination.read_bytes() == source.read_bytes()
    assert view._preview is None
    view._open_image_preview(source.as_uri())
    preview = view._preview
    preview.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10)))
    assert preview.isVisible()
    assert len(selected) == 2
    preview.close()
    view.close()
