"""Save full-size Qt chat images without re-downloading or mutating attachments."""
import base64
import binascii
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlsplit

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QMessageBox


def image_bytes(source: str, cache: dict[str, bytes]) -> tuple[bytes, str] | None:
    """Return available original bytes, preferring the display derivative for a card."""
    url = QUrl(source)
    if url.isLocalFile() or Path(source).is_absolute():
        path = Path(url.toLocalFile() if url.isLocalFile() else source)
        if path.name == "thumbnail-256-v1.jpg":
            display = path.with_name("image-display-v1.jpg")
            if display.is_file():
                path = display
        try:
            data = path.read_bytes()
        except OSError:
            return None
        name = path.name
    elif source.startswith(("http://", "https://")):
        data = cache.get(source, b"")
        name = Path(unquote(urlsplit(source).path)).name
    elif source.startswith("data:"):
        try:
            header, payload = source.split(",", 1)
            data = (base64.b64decode(payload, validate=True) if ";base64" in header.lower()
                    else unquote_to_bytes(payload))
        except (ValueError, UnicodeError, binascii.Error):
            return None
        name = "image"
    else:
        return None
    image = QImage()
    if not data or not image.loadFromData(data):
        return None
    # Remote/data names cannot be trusted as extensions. Use the decoded format,
    # not the URL's query string or an arbitrary server-supplied filename.
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        ext = ".gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        ext = ".png"
        from PySide6.QtCore import QBuffer, QIODevice
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            return None
        data = bytes(buffer.data())
    safe_name = Path(name).name or "image"
    if not safe_name.lower().endswith(ext):
        safe_name = Path(safe_name).stem + ext
    return data, safe_name


def save_image_as(source: str, cache: dict[str, bytes], parent=None) -> bool:
    available = image_bytes(source, cache)
    if available is None:
        QMessageBox.warning(parent, "Cannot Save Image", "The image is no longer available.")
        return False
    data, name = available
    destination, _ = QFileDialog.getSaveFileName(parent, "Save Image As", name,
                                                  "Images (*.png *.jpg *.jpeg *.gif *.webp)")
    if not destination:
        return False
    try:
        Path(destination).write_bytes(data)
    except OSError as exc:
        QMessageBox.warning(parent, "Cannot Save Image", f"Could not save image: {exc}")
        return False
    return True
