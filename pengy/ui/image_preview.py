"""Click-to-dismiss, screen-bounded preview for images in the Qt chat view."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMenu, QVBoxLayout

from pengy.ui.image_save import save_image_as


class ImagePreview(QDialog):
    def __init__(self, image, parent=None, *, source="", cache=None):
        super().__init__(parent)
        self.setObjectName("pengyImagePreview")
        self._source = source
        self._cache = cache if cache is not None else {}
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet("QDialog { background: #171a20; } QLabel { background: transparent; }")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        screen = (parent.screen() if parent is not None else None) or QApplication.primaryScreen()
        bounds = screen.availableGeometry().size() if screen else image.size()
        size = image.size().scaled(max(1, int(bounds.width() * 0.85)),
                                   max(1, int(bounds.height() * 0.85)),
                                   Qt.AspectRatioMode.KeepAspectRatio)
        picture = QLabel(self)
        picture.setObjectName("pengyImagePreviewPicture")
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        picture.setPixmap(QPixmap.fromImage(image).scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        picture.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(picture)
        self.setToolTip("Click to close image preview")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.close()
            event.accept()
        else:
            super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        save_action = menu.addAction("Save Image As…")
        if menu.exec(event.globalPos()) == save_action:
            save_image_as(self._source, self._cache, self)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
        else:
            super().keyPressEvent(event)
