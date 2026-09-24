"""Qt chat image previews: exact hit target, full-resolution source and dismissal."""
import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel

from pengy.ui.chat_view import ChatView


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_chat_image_opens_full_resolution_and_click_closes(qapp, tmp_path):
    source = tmp_path / "wide image.png"
    assert QImage(1000, 500, QImage.Format.Format_RGB32).save(str(source))
    view = ChatView()
    view.resize(700, 600)
    view.show()
    view.append_message("assistant", f"![Picture]({source.as_uri()})")
    qapp.processEvents()
    assert view._image_at(QPoint(100, 60)) == source.as_uri()
    assert view._image_at(QPoint(630, 60)) == ""

    click = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(100, 60),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(click)
    assert view._preview.isVisible()
    picture = view._preview.findChild(QLabel, "pengyImagePreviewPicture")
    assert picture.pixmap().width() > 600  # unlike the 600px inline resource
    view._preview.mousePressEvent(click)
    assert not view._preview.isVisible()
    view.close()


def test_attachment_uses_display_derivative_not_thumbnail(qapp, tmp_path):
    parent = tmp_path / "sha256" / "aa"
    parent.mkdir(parents=True)
    thumb = parent / "thumbnail-256-v1.jpg"
    display = parent / "image-display-v1.jpg"
    assert QImage(100, 50, QImage.Format.Format_RGB32).save(str(thumb))
    assert QImage(1000, 500, QImage.Format.Format_RGB32).save(str(display))
    view = ChatView()
    view._open_image_preview(thumb.as_uri())
    picture = view._preview.findChild(QLabel, "pengyImagePreviewPicture")
    assert picture.pixmap().width() > 100
    view.clear()
    assert view._preview is None
    view.close()


def test_invalid_image_does_not_open_preview(qapp, tmp_path):
    view = ChatView()
    view._open_image_preview((tmp_path / "missing.png").as_uri())
    assert view._preview is None
    view.close()
