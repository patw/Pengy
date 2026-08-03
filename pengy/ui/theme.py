"""Theme helpers for the Pengy Qt UI."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication


THEME_MODES = ("system", "light", "dark")
ACCENT_NAMES = ("default", "blue", "teal", "green", "orange", "red", "pink", "purple")


BASE_THEMES: dict[str, dict[str, str]] = {
    "light": {
        "mode": "light",
        "bg": "#ffffff",
        "fg": "#1e1e2e",
        "panel": "#f8f9fb",
        "panel_2": "#f0f2f5",
        "panel_3": "#e8edf5",
        "input_bg": "#ffffff",
        "input_fg": "#1e1e2e",
        "border": "#c9ced6",
        "border_soft": "#dde2ea",
        "muted": "#667085",
        "code_bg": "#f5f7fa",
        "code_fg": "#27313f",
        "hover": "#edf2fa",
        "selection": "#e8f0fe",
        "tool_bg": "#fafbfc",
        "tool_arg_bg": "#f0f2f5",
        "reasoning_bg": "#fff9f0",
        "reasoning_border": "#e0caa0",
        "reasoning_fg": "#8b6914",
        "user_label": "#0b3d91",
        "assistant_label": "#0f6b3f",
        "pygments_style": "friendly",
    },
    "dark": {
        "mode": "dark",
        "bg": "#1e1e2e",
        "fg": "#cdd6f4",
        "panel": "#181825",
        "panel_2": "#313244",
        "panel_3": "#45475a",
        "input_bg": "#11111b",
        "input_fg": "#cdd6f4",
        "border": "#45475a",
        "border_soft": "#313244",
        "muted": "#a6adc8",
        "code_bg": "#11111b",
        "code_fg": "#cdd6f4",
        "hover": "#313244",
        "selection": "#25324a",
        "tool_bg": "#181825",
        "tool_arg_bg": "#11111b",
        "reasoning_bg": "#1e1608",
        "reasoning_border": "#4a3812",
        "reasoning_fg": "#d4a835",
        "user_label": "#89b4fa",
        "assistant_label": "#a6e3a1",
        "pygments_style": "monokai",
    },
}


ACCENTS: dict[str, dict[str, str]] = {
    "default": {
        "accent_name": "default",
        "primary": "#1e66f5",
        "primary_hover": "#4478f7",
        "primary_fg": "#ffffff",
        "secondary": "#89b4fa",
        "link": "#1e66f5",
        "focus": "#89b4fa",
    },
    "blue": {
        "accent_name": "blue",
        "primary": "#1e66f5",
        "primary_hover": "#4478f7",
        "primary_fg": "#ffffff",
        "secondary": "#89b4fa",
        "link": "#1e66f5",
        "focus": "#89b4fa",
    },
    "teal": {
        "accent_name": "teal",
        "primary": "#179299",
        "primary_hover": "#1fa9b1",
        "primary_fg": "#ffffff",
        "secondary": "#94e2d5",
        "link": "#179299",
        "focus": "#94e2d5",
    },
    "green": {
        "accent_name": "green",
        "primary": "#40a02b",
        "primary_hover": "#56b641",
        "primary_fg": "#ffffff",
        "secondary": "#a6e3a1",
        "link": "#40a02b",
        "focus": "#a6e3a1",
    },
    "orange": {
        "accent_name": "orange",
        "primary": "#df8e1d",
        "primary_hover": "#fea82f",
        "primary_fg": "#ffffff",
        "secondary": "#fab387",
        "link": "#df8e1d",
        "focus": "#fab387",
    },
    "red": {
        "accent_name": "red",
        "primary": "#d20f39",
        "primary_hover": "#e64553",
        "primary_fg": "#ffffff",
        "secondary": "#f38ba8",
        "link": "#d20f39",
        "focus": "#f38ba8",
    },
    "pink": {
        "accent_name": "pink",
        "primary": "#ea76cb",
        "primary_hover": "#f18bd4",
        "primary_fg": "#2b1224",
        "secondary": "#f5c2e7",
        "link": "#c94eb3",
        "focus": "#f5c2e7",
    },
    "purple": {
        "accent_name": "purple",
        "primary": "#8839ef",
        "primary_hover": "#9b5cf6",
        "primary_fg": "#ffffff",
        "secondary": "#cba6f7",
        "link": "#8839ef",
        "focus": "#cba6f7",
    },
}


# Accent-tinted surfaces make each accent feel like a full theme rather than
# only changing button/link highlights.  These intentionally stay desaturated
# enough to preserve readability while giving the whole window a distinct mood.
ACCENT_SURFACES: dict[str, dict[str, dict[str, str]]] = {
    "light": {
        "default": {},
        "blue": {
            "bg": "#eef5ff", "panel": "#e4efff", "panel_2": "#d8e8ff", "panel_3": "#c6dcff",
            "input_bg": "#fbfdff", "border": "#aac7f6", "border_soft": "#c7dbfb",
            "hover": "#dceaff", "selection": "#cfe1ff", "tool_bg": "#f4f8ff", "tool_arg_bg": "#e4efff",
            "code_bg": "#eaf3ff", "reasoning_bg": "#fffaf2", "reasoning_border": "#e2cca2", "reasoning_fg": "#8b6914",
            "user_label": "#174ea6", "assistant_label": "#1c6a55",
        },
        "teal": {
            "bg": "#ecfdfb", "panel": "#dff8f5", "panel_2": "#cef0ec", "panel_3": "#b8e6e0",
            "input_bg": "#fbfffe", "border": "#95d5ce", "border_soft": "#b9e6e1",
            "hover": "#d6f4f0", "selection": "#c3ece7", "tool_bg": "#f2fbfa", "tool_arg_bg": "#dff8f5",
            "code_bg": "#e7f7f5", "reasoning_bg": "#fefaf1", "reasoning_border": "#ddd09e", "reasoning_fg": "#8b6914",
            "user_label": "#126a70", "assistant_label": "#277252",
        },
        "green": {
            "bg": "#f0faec", "panel": "#e4f5df", "panel_2": "#d6edcf", "panel_3": "#c4e2bb",
            "input_bg": "#fcfffb", "border": "#a8d39d", "border_soft": "#c4e4bd",
            "hover": "#ddf2d7", "selection": "#cfebc7", "tool_bg": "#f5fbf2", "tool_arg_bg": "#e4f5df",
            "code_bg": "#ecf7e8", "reasoning_bg": "#fdf9ee", "reasoning_border": "#dcd09a", "reasoning_fg": "#8b6914",
            "user_label": "#2b6d1f", "assistant_label": "#2f7a3e",
        },
        "orange": {
            "bg": "#fff4e5", "panel": "#ffe9cf", "panel_2": "#ffddb5", "panel_3": "#f6ca94",
            "input_bg": "#fffdf9", "border": "#e7b06a", "border_soft": "#f2cf9e",
            "hover": "#ffe6c5", "selection": "#ffd8a3", "tool_bg": "#fff8ef", "tool_arg_bg": "#ffe9cf",
            "code_bg": "#fff0da", "reasoning_bg": "#fff6e8", "reasoning_border": "#e8cc96", "reasoning_fg": "#8b6914",
            "user_label": "#9a4d00", "assistant_label": "#5f6f1f",
        },
        "red": {
            "bg": "#fff0f2", "panel": "#ffe2e7", "panel_2": "#ffd3dc", "panel_3": "#f6bbc8",
            "input_bg": "#fffafa", "border": "#e6a0ad", "border_soft": "#f0c1ca",
            "hover": "#ffe0e6", "selection": "#ffcbd6", "tool_bg": "#fff6f7", "tool_arg_bg": "#ffe2e7",
            "code_bg": "#ffedf0", "reasoning_bg": "#fef9f0", "reasoning_border": "#e4cda2", "reasoning_fg": "#8b6914",
            "user_label": "#a30d2d", "assistant_label": "#30704c",
        },
        "pink": {
            "bg": "#fff0fa", "panel": "#ffe3f5", "panel_2": "#ffd4ef", "panel_3": "#f4bde2",
            "input_bg": "#fffafd", "border": "#df9aca", "border_soft": "#efbfdf",
            "hover": "#ffe1f4", "selection": "#ffcdec", "tool_bg": "#fff6fc", "tool_arg_bg": "#ffe3f5",
            "code_bg": "#ffedf8", "reasoning_bg": "#fefaf2", "reasoning_border": "#e4cfa4", "reasoning_fg": "#8b6914",
            "user_label": "#9d2a78", "assistant_label": "#2d7054",
        },
        "purple": {
            "bg": "#f7f0ff", "panel": "#efe4ff", "panel_2": "#e4d4ff", "panel_3": "#d3bdf6",
            "input_bg": "#fdfaff", "border": "#b89ce6", "border_soft": "#d2c0f0",
            "hover": "#eadfff", "selection": "#dfd0ff", "tool_bg": "#fbf7ff", "tool_arg_bg": "#efe4ff",
            "code_bg": "#f3ebff", "reasoning_bg": "#fdf8ef", "reasoning_border": "#e2cf9e", "reasoning_fg": "#8b6914",
            "user_label": "#6d2bbd", "assistant_label": "#32704e",
        },
    },
    "dark": {
        "default": {},
        "blue": {
            "bg": "#071225", "panel": "#0b1b33", "panel_2": "#11284a", "panel_3": "#173762",
            "input_bg": "#050b17", "border": "#25466f", "border_soft": "#163151",
            "hover": "#122c52", "selection": "#173b70", "tool_bg": "#0a172b", "tool_arg_bg": "#06101f",
            "code_bg": "#050b17", "reasoning_bg": "#1f1708", "reasoning_border": "#4c3a12", "reasoning_fg": "#d4a835",
            "user_label": "#89b4fa", "assistant_label": "#94e2d5",
        },
        "teal": {
            "bg": "#061a1a", "panel": "#092626", "panel_2": "#103a3a", "panel_3": "#155151",
            "input_bg": "#041111", "border": "#1d5c5c", "border_soft": "#123f3f",
            "hover": "#103535", "selection": "#164949", "tool_bg": "#0a2020", "tool_arg_bg": "#061515",
            "code_bg": "#041111", "reasoning_bg": "#1e1709", "reasoning_border": "#4b3a13", "reasoning_fg": "#d4a835",
            "user_label": "#94e2d5", "assistant_label": "#a6e3a1",
        },
        "green": {
            "bg": "#081807", "panel": "#0d240b", "panel_2": "#163613", "panel_3": "#204b1c",
            "input_bg": "#050f04", "border": "#2b5d27", "border_soft": "#1b4018",
            "hover": "#173315", "selection": "#21491d", "tool_bg": "#0b1d09", "tool_arg_bg": "#071406",
            "code_bg": "#050f04", "reasoning_bg": "#1e1707", "reasoning_border": "#4a3911", "reasoning_fg": "#d4a835",
            "user_label": "#a6e3a1", "assistant_label": "#94e2d5",
        },
        "orange": {
            "bg": "#211306", "panel": "#301b08", "panel_2": "#47290e", "panel_3": "#623813",
            "input_bg": "#160c03", "border": "#7a4a1e", "border_soft": "#51300f",
            "hover": "#3e250d", "selection": "#5a3411", "tool_bg": "#261607", "tool_arg_bg": "#1b0f04",
            "code_bg": "#160c03", "reasoning_bg": "#201505", "reasoning_border": "#4c360c", "reasoning_fg": "#d4a835",
            "user_label": "#fab387", "assistant_label": "#a6e3a1",
        },
        "red": {
            "bg": "#24080f", "panel": "#350c17", "panel_2": "#4c1220", "panel_3": "#66182c",
            "input_bg": "#170409", "border": "#78263a", "border_soft": "#551828",
            "hover": "#42111d", "selection": "#61192b", "tool_bg": "#2a0912", "tool_arg_bg": "#1b050b",
            "code_bg": "#170409", "reasoning_bg": "#1e1608", "reasoning_border": "#4a3812", "reasoning_fg": "#d4a835",
            "user_label": "#f38ba8", "assistant_label": "#a6e3a1",
        },
        "pink": {
            "bg": "#250719", "panel": "#360b25", "panel_2": "#501237", "panel_3": "#6b184a",
            "input_bg": "#170410", "border": "#7b285a", "border_soft": "#58193f",
            "hover": "#46102f", "selection": "#631845", "tool_bg": "#2b091d", "tool_arg_bg": "#1b0513",
            "code_bg": "#170410", "reasoning_bg": "#1e1509", "reasoning_border": "#4b3813", "reasoning_fg": "#d4a835",
            "user_label": "#f5c2e7", "assistant_label": "#94e2d5",
        },
        "purple": {
            "bg": "#170b2b", "panel": "#21123d", "panel_2": "#321b5a", "panel_3": "#45257a",
            "input_bg": "#0f071c", "border": "#5b3a8e", "border_soft": "#3a2264",
            "hover": "#2c184f", "selection": "#402371", "tool_bg": "#1b0e33", "tool_arg_bg": "#110820",
            "code_bg": "#0f071c", "reasoning_bg": "#1e1609", "reasoning_border": "#4b3912", "reasoning_fg": "#d4a835",
            "user_label": "#cba6f7", "assistant_label": "#a6e3a1",
        },
    },
}


SEMANTIC = {
    "danger": "#d20f39",
    "danger_hover": "#e64553",
    "success": "#40a02b",
    "success_soft": "#a6e3a1",
    "warning": "#df8e1d",
    "warning_hover": "#fea82f",
    "running": "#fab387",
    "declined": "#d20f39",
}


def _is_dark_color(color: QColor) -> bool:
    # Perceived luminance; QColor.lightness() is HSL lightness and less reliable here.
    return (0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()) < 128


def resolve_theme_mode(mode: str | None) -> str:
    """Resolve 'system' to the current Qt palette's light/dark mode."""
    if mode not in THEME_MODES:
        mode = "system"
    if mode in ("light", "dark"):
        return mode

    app = QApplication.instance()
    if app is None:
        return "light"
    window_color = app.palette().color(QPalette.ColorRole.Window)
    return "dark" if _is_dark_color(window_color) else "light"


def ui_scale_factor(config_or_theme: dict | None = None) -> float:
    """Return Pengy's UI preference as a platform-independent multiplier.

    OS display scaling is Qt's responsibility. In particular, an externally
    supplied QT_SCALE_FACTOR is intentionally neither read nor cancelled here.
    """
    raw_scale = (config_or_theme or {}).get("ui_scale", 100)
    try:
        scale = float(raw_scale)
    except (TypeError, ValueError):
        scale = 100.0
    # Keep manually-entered config values sane while preserving the Settings
    # dialog's normal 75–200% range.
    scale = max(50.0, min(scale, 300.0))
    return scale / 100.0


def _valid_point_size(font: QFont, fallback: float = 10.0) -> float:
    """Return a usable platform font point size."""
    point_size = font.pointSizeF()
    return point_size if point_size > 0 else fallback


def scaled_font_size(base_pt: float, config_or_theme: dict | None = None) -> float:
    """Scale an explicit point size by Pengy's UI preference."""
    return max(1.0, base_pt * ui_scale_factor(config_or_theme))


def ui_font(config_or_theme: dict | None = None) -> QFont:
    """Return the platform general font scaled by Pengy's preference."""
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font.setPointSizeF(_valid_point_size(font) * ui_scale_factor(config_or_theme))
    return font


def chat_font(config_or_theme: dict | None = None) -> QFont:
    """Return the platform fixed font scaled by Pengy's preference."""
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSizeF(_valid_point_size(font) * ui_scale_factor(config_or_theme))
    return font


def small_ui_font(config_or_theme: dict | None = None) -> QFont:
    font = ui_font(config_or_theme)
    font.setPointSizeF(max(1.0, font.pointSizeF() * 0.9))
    return font


def heading_font(config_or_theme: dict | None = None) -> QFont:
    font = ui_font(config_or_theme)
    font.setPointSizeF(max(1.0, font.pointSizeF() * 1.4))
    font.setBold(True)
    return font


def scaled_size(base_px: int, config_or_theme: dict | None = None) -> int:
    """Scale a pixel-size UI value by the configured UI scale."""
    return max(1, round(base_px * ui_scale_factor(config_or_theme)))


def get_theme(config_or_mode: dict | str | None = None, accent: str | None = None) -> dict[str, str]:
    """Return a composed theme from config or explicit mode/accent values."""
    if isinstance(config_or_mode, dict):
        requested_mode = config_or_mode.get("theme_mode", "system")
        requested_accent = config_or_mode.get("theme_accent", "default")
        requested_scale = config_or_mode.get("ui_scale", 100)
    else:
        requested_mode = config_or_mode or "system"
        requested_accent = accent or "default"
        requested_scale = 100

    resolved_mode = resolve_theme_mode(requested_mode)
    if requested_accent not in ACCENTS:
        requested_accent = "default"

    theme = dict(BASE_THEMES[resolved_mode])
    theme.update(ACCENT_SURFACES[resolved_mode][requested_accent])
    theme.update(ACCENTS[requested_accent])
    theme.update(SEMANTIC)
    theme["requested_mode"] = requested_mode if requested_mode in THEME_MODES else "system"
    theme["ui_scale"] = str(requested_scale)
    return theme


def qt_app_stylesheet(theme: dict[str, str]) -> str:
    """Application/window-level Qt stylesheet for common widgets.

    The explicit point size is intentional: macOS's native Qt style does not
    consistently propagate QApplication.setFont() into every styled control.
    QSS makes the application typography role deterministic while Qt still
    handles physical DPI and text rasterization.
    """
    pad_v = scaled_size(5, theme)
    pad_h = scaled_size(10, theme)
    app_font = ui_font(theme)
    app_pt = f"{app_font.pointSizeF():.2f}".rstrip("0").rstrip(".")
    return f"""
    QMainWindow, QWidget {{
        background-color: {theme['bg']};
        color: {theme['fg']};
        font-family: "{app_font.family()}";
        font-size: {app_pt}pt;
    }}
    QSplitter::handle {{
        background-color: {theme['border_soft']};
    }}
    QFrame {{
        color: {theme['fg']};
        border-color: {theme['border']};
    }}
    QLabel {{
        color: {theme['fg']};
    }}
    QListWidget {{
        background-color: {theme['panel']};
        color: {theme['fg']};
        border: 1px solid {theme['border_soft']};
        border-radius: 6px;
        outline: none;
    }}
    QListWidget::item {{
        color: {theme['fg']};
        padding: 4px;
        border-radius: 6px;
    }}
    QListWidget::item:selected {{
        background-color: {theme['selection']};
        color: {theme['fg']};
    }}
    QListWidget::item:hover {{
        background-color: {theme['hover']};
    }}
    QPushButton {{
        background-color: {theme['panel_2']};
        color: {theme['fg']};
        border: 1px solid {theme['border']};
        border-radius: 8px;
        padding: {pad_v}px {pad_h}px;
    }}
    QPushButton:hover {{
        background-color: {theme['hover']};
        border-color: {theme['primary']};
    }}
    QPushButton:pressed {{
        background-color: {theme['selection']};
    }}
    QPushButton:disabled {{
        color: {theme['muted']};
        background-color: {theme['panel']};
        border-color: {theme['border_soft']};
    }}
    QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
        background-color: {theme['input_bg']};
        color: {theme['input_fg']};
        border: 1px solid {theme['border']};
        border-radius: 6px;
        padding: 4px 6px;
        selection-background-color: {theme['primary']};
        selection-color: {theme['primary_fg']};
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border-color: {theme['focus']};
    }}
    QDialog {{
        background-color: {theme['bg']};
        color: {theme['fg']};
    }}
    QMenu {{
        background-color: {theme['panel']};
        color: {theme['fg']};
        border: 1px solid {theme['border']};
    }}
    QMenu::item:selected {{
        background-color: {theme['selection']};
    }}
    QTabWidget::pane {{
        background-color: {theme['bg']};
        border: none;
    }}
    QTabWidget::tab-bar {{
        alignment: left;
    }}
    QTabBar::tab {{
        background-color: {theme['panel']};
        color: {theme['fg']};
        border: 1px solid {theme['border_soft']};
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        padding: {pad_v}px {pad_h}px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background-color: {theme['bg']};
        color: {theme['primary']};
        border-bottom: 2px solid {theme['primary']};
    }}
    QTabBar::tab:hover {{
        background-color: {theme['hover']};
    }}
    QTabBar::tab:!selected {{
        color: {theme['muted']};
    }}
    """
