"""Portable, theme-aware SVG icons for the Qt desktop UI.

The source SVGs intentionally use a plain ``__COLOR__`` placeholder instead of
CSS or an icon theme.  The same assets can therefore be rendered with
QSvgRenderer in PySide6 and Qt C++, without relying on platform fonts or a
system icon theme.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
_RENDER_SIZES = (16, 20, 24, 32, 48)


def icon_path(name: str) -> Path:
    """Return the source path for a bundled icon, raising for unknown names."""
    path = _ICON_DIR / f"{name}.svg"
    if not path.is_file():
        raise ValueError(f"Unknown Pengy icon: {name}")
    return path


@lru_cache(maxsize=256)
def _svg_bytes(name: str, color: str) -> bytes:
    source = icon_path(name).read_text(encoding="utf-8")
    # SVG/CSS colors use #RRGGBB (or #RRGGBBAA), while QColor.HexArgb emits
    # Qt's #AARRGGBB ordering. QSvgRenderer accepts the file but treats that
    # value inconsistently across platforms—notably producing blank icons on
    # macOS. All current theme icon colors are opaque, so use portable RGB.
    svg_color = QColor(color).name(QColor.NameFormat.HexRgb)
    return source.replace("__COLOR__", svg_color).encode("utf-8")


def _pixmap(name: str, color: str, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(_svg_bytes(name, color)))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap


@lru_cache(maxsize=256)
def _build_themed_icon(name: str, color: str, active_color: str, disabled_color: str) -> QIcon:
    """The actual multi-size (5 sizes x 3 states = 15 QPixmap renders) build.

    Cached because callers like the chat-history sidebar call themed_icon()
    with the *same* (name, color) for every row's Save/Delete button and
    rebuild the whole list on every chat create/delete/rename — without this
    cache that's 15 fresh QSvgRenderer + QPainter passes per icon per row,
    every time, which is the actual cost behind "New Chat feels slow" once a
    sidebar has more than a handful of chats. A QIcon is safe to share: after
    construction nothing else in this module (or its callers) mutates one.
    """
    icon = QIcon()
    for size in _RENDER_SIZES:
        icon.addPixmap(_pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(_pixmap(name, active_color, size), QIcon.Mode.Active, QIcon.State.Off)
        icon.addPixmap(_pixmap(name, disabled_color, size), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


def themed_icon(
    name: str,
    color: str,
    *,
    active_color: str | None = None,
    disabled_color: str | None = None,
) -> QIcon:
    """Build a multi-size QIcon with normal, active, and disabled colors."""
    active_color = active_color or color
    disabled_color = disabled_color or QColor(color).lighter(135).name()
    return _build_themed_icon(name, color, active_color, disabled_color)


def apply_button_icon(
    button,
    name: str,
    theme: dict[str, str],
    *,
    size: int = 16,
    color_role: str = "fg",
    active_role: str = "primary",
) -> None:
    """Apply a named icon and record its name for tests/re-theming."""
    button.setProperty("pengyIcon", name)
    button.setIcon(themed_icon(
        name,
        theme[color_role],
        active_color=theme[active_role],
        disabled_color=theme["muted"],
    ))
    button.setIconSize(QSize(size, size))
