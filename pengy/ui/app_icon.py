"""Application icon loading with a macOS native-conversion workaround."""
import sys
from pathlib import Path

from PySide6.QtGui import QColorSpace, QIcon, QImage, QPixmap


def application_icon(path: str | Path) -> QIcon:
    if sys.platform == "darwin":
        # Qt 6.11.1/macOS 26.7.3 can crash in QImage::toCGImage when the
        # bundled PNG has a color profile. Drop only metadata, not pixels or
        # alpha, to use Qt's default RGB space for the native icon conversion.
        image = QImage(str(path))
        image.setColorSpace(QColorSpace())
        return QIcon(QPixmap.fromImage(image))
    return QIcon(str(path))
