"""Chat view with markdown rendering for Pengy."""
import base64
import json
import re
import threading
import urllib.request
from PySide6.QtWidgets import QTextBrowser
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase, QImage, QMouseEvent, QTextDocument

import markdown
from pygments import highlight
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.formatters import HtmlFormatter

from pengy.ui.theme import get_theme, scaled_font_size


def _fmt_pt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _build_css(theme: dict[str, str] | None = None) -> str:
    theme = theme or get_theme()
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    body_pt = _fmt_pt(scaled_font_size(10, theme))
    label_pt = _fmt_pt(scaled_font_size(9, theme))
    reasoning_label_pt = _fmt_pt(scaled_font_size(8.5, theme))
    return f"""
body {{
    font-family: "{fixed}";
    font-size: {body_pt}pt;
    background-color: {theme['bg']};
    color: {theme['fg']};
    margin: 8px;
}}
a {{ color: {theme['link']}; text-decoration: none; }}
pre {{ white-space: pre-wrap; word-wrap: break-word; }}
table {{
    border: 1px solid {theme['border']};
    margin: 6px 0;
}}
th, td {{
    border: 1px solid {theme['border']};
    padding: 4px 10px;
}}
th {{
    background-color: {theme['panel_2']};
    font-weight: bold;
}}
img {{ max-width: 600px; }}
.role-user {{ color:{theme['user_label']}; font-weight:bold; font-size:{label_pt}pt; margin:8px 0 2px 0; }}
.role-assistant {{ color:{theme['assistant_label']}; font-weight:bold; font-size:{label_pt}pt; margin:8px 0 2px 0; }}
.tool-card {{ border:1px solid {theme['border_soft']}; padding:4px 8px; margin:6px 0; background-color:{theme['tool_bg']}; }}
.tool-link {{ color:{theme['link']}; text-decoration:none; font-weight:bold; }}
.tool-pre {{ background-color:{theme['tool_arg_bg']}; color:{theme['code_fg']}; padding:4px; margin:2px 0; font-size:{label_pt}pt; }}
.code-pre {{ background-color:{theme['code_bg']}; color:{theme['code_fg']}; padding:10px; margin:6px 0; }}
.muted {{ color:{theme['muted']}; }}
.declined {{ color:{theme['danger']}; }}
.reasoning-card {{ border:1px solid {theme['reasoning_border']}; padding:6px 10px; margin:6px 0; background-color:{theme['reasoning_bg']}; }}
.reasoning-link {{ color:{theme['reasoning_fg']}; text-decoration:none; font-weight:bold; }}
.reasoning-body {{ color:{theme['muted']}; font-size:{reasoning_label_pt}pt; white-space:pre-wrap; word-wrap:break-word; margin-top:4px; }}
"""


class ChatView(QTextBrowser):
    """Markdown-rendering chat view with collapsible tool call blocks."""

    _image_loaded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages = []
        self._theme = get_theme()
        self._expanded_tools = set()
        self._expanded_reasoning: set[int] = set()
        self._image_cache: dict[str, bytes] = {}   # url -> raw bytes (b"" = failed)
        self._image_pending: set[str] = set()
        self._image_lock = threading.Lock()
        self._image_loaded.connect(self._render)
        self.md_extensions = ["fenced_code", "codehilite", "tables", "footnotes"]
        # One reusable parser; constructing a fresh Markdown() per message is
        # ~6x slower and this method is called for every message on every render.
        self._md = markdown.Markdown(extensions=self.md_extensions)
        self._apply_font(self._theme)
        self.apply_theme(self._theme)
        self.setOpenLinks(False)

    def _apply_font(self, theme: dict[str, str]):
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSizeF(scaled_font_size(10, theme))
        self.setFont(font)

    def apply_theme(self, theme: dict[str, str]):
        """Apply a theme and re-render existing messages."""
        self._theme = theme
        self._apply_font(theme)
        self.setStyleSheet(
            f"QTextBrowser {{ background-color: {theme['bg']}; color: {theme['fg']}; border: none; padding: 0; }}"
        )
        if self._messages:
            self._render()

    def loadResource(self, type_: int, url: QUrl) -> object:
        """Return cached external images, data URIs, or local files."""
        if type_ == QTextDocument.ResourceType.ImageResource:
            url_str = url.toString()

            # ── HTTP/HTTPS: cached network fetch ──────────────────
            if url_str.startswith(("http://", "https://")):
                should_fetch = False
                with self._image_lock:
                    cached = self._image_cache.get(url_str)
                    if cached is None and url_str not in self._image_pending:
                        self._image_pending.add(url_str)
                        should_fetch = True

                if should_fetch:
                    threading.Thread(
                        target=self._fetch_image, args=(url_str,), daemon=True
                    ).start()

                if cached:
                    image = QImage()
                    image.loadFromData(cached)
                    if not image.isNull():
                        if image.width() > 600:
                            image = image.scaledToWidth(
                                600, Qt.TransformationMode.SmoothTransformation
                            )
                        return image

                return None  # not yet loaded; Qt leaves a blank space until re-render

            # ── Data URIs: decode base64 ourselves ────────────────
            if url_str.startswith("data:"):
                try:
                    # Format: data:[<mediatype>][;base64],<data>
                    header, encoded = url_str.split(",", 1)
                    is_base64 = ";base64" in header
                    if is_base64:
                        raw = base64.b64decode(encoded)
                    else:
                        raw = encoded.encode("utf-8")
                    image = QImage()
                    image.loadFromData(raw)
                    if not image.isNull():
                        if image.width() > 600:
                            image = image.scaledToWidth(
                                600, Qt.TransformationMode.SmoothTransformation
                            )
                        return image
                except Exception:
                    pass
                return None

            # ── Local file:// images ──────────────────────────────
            if url_str.startswith("file://"):
                local_path = url.toLocalFile()
                image = QImage()
                if image.load(local_path):
                    if image.width() > 600:
                        image = image.scaledToWidth(
                            600, Qt.TransformationMode.SmoothTransformation
                        )
                    return image
                return None

        return super().loadResource(type_, url)

    def _fetch_image(self, url_str: str):
        """Fetch an image in a worker thread; on success emit _image_loaded to re-render."""
        try:
            req = urllib.request.Request(
                url_str, headers={"User-Agent": "PengyAgent/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(4 * 1024 * 1024)  # cap at 4 MB
            with self._image_lock:
                self._image_cache[url_str] = data
            self._image_loaded.emit()
        except Exception:
            with self._image_lock:
                self._image_cache[url_str] = b""  # sentinel: don't retry
        finally:
            with self._image_lock:
                self._image_pending.discard(url_str)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            anchor = self.anchorAt(event.pos())
            if anchor.startswith("toggle://"):
                tool_id = anchor[len("toggle://"):]
                if tool_id in self._expanded_tools:
                    self._expanded_tools.discard(tool_id)
                else:
                    self._expanded_tools.add(tool_id)
                self._render()
                return
            if anchor.startswith("reasoning://"):
                idx = int(anchor[len("reasoning://"):])
                if idx in self._expanded_reasoning:
                    self._expanded_reasoning.discard(idx)
                else:
                    self._expanded_reasoning.add(idx)
                self._render()
                return
            if anchor.startswith(("http://", "https://")):
                QDesktopServices.openUrl(QUrl(anchor))
                return
        super().mousePressEvent(event)

    def append_message(self, role: str, content, *, reasoning_content: str = None, render: bool = True):
        if role == "user":
            self._messages.append({"role": "user", "content": content})
        elif role == "assistant":
            if content:
                msg = {"role": "assistant", "content": content}
                if reasoning_content:
                    msg["reasoning_content"] = reasoning_content
                self._messages.append(msg)
        elif role == "tool_request":
            tool_call_id = content.get("tool_call_id", f"tc-{len(self._messages)}")
            self._messages.append({
                "role": "tool_block",
                "tool_call_id": tool_call_id,
                "name": content.get("name", "unknown"),
                "args": content.get("args", {}),
                "result": None,
                "declined": None,
            })
        elif role == "tool_result":
            tool_call_id = content.get("tool_call_id")
            for msg in reversed(self._messages):
                if msg.get("role") == "tool_block" and msg.get("tool_call_id") == tool_call_id:
                    msg["result"] = content.get("content", "")
                    msg["declined"] = content.get("declined", False)
                    break
        if render:
            self._render()

    def render_now(self):
        """Force a full re-render. Use after a batch of append_message(render=False)."""
        self._render()

    def clear(self):
        self._messages = []
        self._expanded_tools = set()
        self._expanded_reasoning = set()
        super().clear()

    def _render(self):
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 30
        prev_pos = scrollbar.value()
        self.setHtml(self._build_html())
        if at_bottom:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))
        else:
            self.verticalScrollBar().setValue(prev_pos)

    def _build_html(self) -> str:
        parts = [f"<html><head><meta charset='utf-8'><style>{_build_css(self._theme)}</style></head><body>"]
        for idx, msg in enumerate(self._messages):
            parts.append(self._render_msg(msg, idx))
        parts.append("</body></html>")
        return "".join(parts)

    def _render_msg(self, msg: dict, idx: int = 0) -> str:
        role = msg["role"]
        if role == "user":
            body = self._escape_html(msg["content"]).replace("\n", "<br>")
            return (
                '<p class="role-user">You &#x1F9D1;</p>'
                f'<p style="margin:2px 0 10px 0;">{body}</p>'
            )
        if role == "assistant":
            parts = []
            reasoning = msg.get("reasoning_content")
            if reasoning:
                parts.append(self._render_reasoning_block(idx, reasoning))
            html_content = self._render_markdown(msg["content"])
            parts.append(
                '<p class="role-assistant">Assistant &#x1F916;</p>'
                f'<div style="margin:2px 0 10px 0;">{html_content}</div>'
            )
            return "".join(parts)
        if role == "tool_block":
            return self._render_tool_block(msg)
        return ""

    def _render_reasoning_block(self, idx: int, reasoning: str) -> str:
        expanded = idx in self._expanded_reasoning
        arrow = "&#9660;" if expanded else "&#9654;"

        # First line preview for collapsed state
        first_line = reasoning.split("\n", 1)[0]
        preview = first_line[:120] + ("&#8230;" if len(first_line) > 120 else "")

        header = (
            f'<a href="reasoning://{idx}" class="reasoning-link">'
            f'{arrow}&nbsp;Reasoning</a>'
        )
        inner = f'<div style="margin-bottom:2px;">{header}</div>'

        if expanded:
            reasoning_escaped = self._escape_html(reasoning)
            inner += (
                f'<div class="reasoning-body">{reasoning_escaped}</div>'
            )
        else:
            inner += (
                f'<div class="reasoning-body muted">'
                f'{self._escape_html(preview)}'
                f'</div>'
            )

        return (
            '<div class="reasoning-card">'
            f'{inner}'
            '</div>'
        )

    def _render_tool_block(self, msg: dict) -> str:
        tool_call_id = msg["tool_call_id"]
        name = msg["name"]
        args = msg.get("args", {})
        result = msg.get("result")
        declined = msg.get("declined")
        expanded = tool_call_id in self._expanded_tools

        arrow = "&#9660;" if expanded else "&#9654;"
        name_safe = self._escape_html(name)

        args_preview = ", ".join(f"{k}={v!r}" for k, v in args.items())
        truncated = len(args_preview) > 60
        if truncated:
            args_preview = args_preview[:59]
        label = f"{arrow}&nbsp;Tool:&nbsp;{name_safe}"
        if args_preview:
            label += f"&nbsp;[{self._escape_html(args_preview)}]"
            if truncated:
                label += "&#8230;"

        if result is None and not declined:
            status = '&nbsp;<i class="muted">(running&#8230;)</i>'
        elif declined:
            status = '&nbsp;<i class="declined">(declined)</i>'
        else:
            status = ""

        header = (
            f'<a href="toggle://{tool_call_id}" class="tool-link">{label}</a>{status}'
        )

        inner = f'<div style="margin-bottom:2px;">{header}</div>'

        if expanded:
            args_json = self._escape_html(json.dumps(args, indent=2))
            inner += (
                '<div style="margin-top:4px;">'
                '<b>Arguments:</b>'
                f'<pre class="tool-pre">{args_json}</pre>'
                '</div>'
            )
            if result is not None:
                result_label = "Result (declined)" if declined else "Result"
                result_escaped = self._escape_html(result)
                inner += (
                    '<div>'
                    f'<b>{result_label}:</b>'
                    f'<pre class="tool-pre">{result_escaped}</pre>'
                    '</div>'
                )

        return (
            '<div class="tool-card">'
            f'{inner}'
            '</div>'
        )

    def _render_markdown(self, text: str) -> str:
        self._md.reset()
        html = self._md.convert(text)
        # Qt doesn't support border-collapse; cellspacing="0" removes inter-cell gaps
        # so the CSS border on each cell reads as a single collapsed border visually.
        html = html.replace("<table>", '<table cellspacing="0">')
        return self._highlight_code(html)

    def _highlight_code(self, html: str) -> str:
        def do_highlight(match):
            lang = match.group(1) or "text"
            code = match.group(2)
            try:
                lexer = get_lexer_by_name(lang, stripnl=False) if lang and lang != "text" else TextLexer()
                formatter = HtmlFormatter(style=self._theme.get("pygments_style", "friendly"), noclasses=True, nobackground=True)
                highlighted = highlight(code, lexer, formatter)
                return f'<pre class="code-pre">{highlighted}</pre>'
            except Exception:
                return f'<pre class="code-pre">{self._escape_html(code)}</pre>'

        html = re.sub(
            r'<pre><code[^>]*class="language-([^"]*)"[^>]*>(.*?)</code></pre>',
            do_highlight,
            html,
            flags=re.DOTALL,
        )
        html = re.sub(
            r'<code[^>]*class="language-([^"]*)"[^>]*>(.*?)</code>',
            lambda m: f'<pre class="code-pre">{self._escape_html(m.group(2))}</pre>',
            html,
            flags=re.DOTALL,
        )
        html = re.sub(
            r'<pre><code>(.*?)</code></pre>',
            lambda m: f'<pre class="code-pre">{self._escape_html(m.group(1))}</pre>',
            html,
            flags=re.DOTALL,
        )
        return html

    @staticmethod
    def _escape_html(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
