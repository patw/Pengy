"""Chat view with markdown rendering for Pengy."""
import base64
import json
import os
import re
import threading
import urllib.request
from PySide6.QtWidgets import QTextBrowser
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QMouseEvent, QTextDocument

import markdown
from pygments import highlight
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.formatters import HtmlFormatter

from pengy.ui.theme import chat_font, get_theme


def _build_css(theme: dict[str, str] | None = None) -> str:
    theme = theme or get_theme()
    fixed = chat_font(theme).family()
    return f"""
body {{
    font-family: "{fixed}";
    font-size: 1em;
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
.role-user {{ color:{theme['user_label']}; font-weight:bold; font-size:0.9em; margin:8px 0 2px 0; }}
.role-assistant {{ color:{theme['assistant_label']}; font-weight:bold; font-size:0.9em; margin:8px 0 2px 0; }}
.tool-card {{ border:1px solid {theme['border_soft']}; padding:4px 8px; margin:6px 0; background-color:{theme['tool_bg']}; }}
.tool-link {{ color:{theme['link']}; text-decoration:none; font-weight:bold; }}
.tool-pre {{ background-color:{theme['tool_arg_bg']}; color:{theme['code_fg']}; padding:4px; margin:2px 0; font-size:0.9em; }}
.code-pre {{ background-color:{theme['code_bg']}; color:{theme['code_fg']}; padding:10px; margin:6px 0; }}
.muted {{ color:{theme['muted']}; }}
.declined {{ color:{theme['danger']}; }}
.reasoning-card {{ border:1px solid {theme['reasoning_border']}; padding:6px 10px; margin:6px 0; background-color:{theme['reasoning_bg']}; }}
.reasoning-link {{ color:{theme['reasoning_fg']}; text-decoration:none; font-weight:bold; }}
.reasoning-body {{ color:{theme['muted']}; font-size:0.85em; white-space:pre-wrap; word-wrap:break-word; margin-top:4px; }}
h1 {{ font-size:1.4em; }}
h2 {{ font-size:1.3em; }}
h3 {{ font-size:1.1em; }}
h4, h5, h6 {{ font-size:1em; }}
"""


# Within this many pixels of the bottom, treat the view as "pinned" so new
# content auto-scrolls into view instead of pushing the user's reading spot.
_BOTTOM_MARGIN = 30


class ChatView(QTextBrowser):
    """Markdown-rendering chat view with collapsible tool call blocks."""

    _image_loaded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages = []
        # Rendered HTML per message, parallel to _messages (None = needs render).
        # _build_html() re-ran markdown + pygments over the *entire* history on
        # every append, making a conversation O(n^2) to type into. Markdown and
        # pygments are ~96% of render cost (Qt's setHtml layout is the other 4%),
        # so memoising per message removes almost all of it.
        self._html_cache: list[str | None] = []
        self._theme = get_theme()
        self._expanded_tools = set()
        self._expanded_reasoning: set[int] = set()
        self._image_cache: dict[str, bytes] = {}   # url -> raw bytes (b"" = failed)
        self._image_pending: set[str] = set()
        self._image_lock = threading.Lock()
        self._image_loaded.connect(self._render)
        # Auto-scroll tracking. setHtml() replaces the whole document and
        # resets the scrollbar to the top, so scrollbar.value() right after a
        # render is 0 — *not* a reliable "the user scrolled here" signal. We
        # keep an explicit _auto_scroll flag updated only by genuine user
        # scrolling (see _on_scroll_changed), and guard the spurious reset.
        self._auto_scroll = True
        self._rendering = False
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self.md_extensions = ["fenced_code", "codehilite", "tables", "footnotes"]
        # One reusable parser; constructing a fresh Markdown() per message is
        # ~6x slower and this method is called for every message on every render.
        self._md = markdown.Markdown(extensions=self.md_extensions)
        # Cache the Pygments HtmlFormatter (depends only on theme, not code).
        # Recreating it per code block was wasteful — it runs for every code
        # block in every message on every render.
        self._pygments_formatter = None
        self._lexer_cache: dict[str, object] = {}
        self._apply_font(self._theme)
        self.apply_theme(self._theme)
        self.setOpenLinks(False)

    def _apply_font(self, theme: dict[str, str]):
        font = chat_font(theme)
        self.setFont(font)
        self.document().setDefaultFont(font)

    def apply_theme(self, theme: dict[str, str]):
        """Apply a theme and re-render existing messages."""
        self._theme = theme
        self._apply_font(theme)
        # Rebuild the Pygments formatter only when the theme changes,
        # not on every code block highlight.
        self._pygments_formatter = HtmlFormatter(
            style=self._theme.get("pygments_style", "friendly"),
            noclasses=True, nobackground=True,
        )
        self.setStyleSheet(
            f"QTextBrowser {{ background-color: {theme['bg']}; color: {theme['fg']}; border: none; padding: 0; }}"
        )
        # A new theme means a new pygments formatter, so every cached code
        # block is stale — not just the CSS in the <head>.
        self._invalidate_all()
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

            # ── Local file images ─────────────────────────────────
            # Skills normally emit file:/// URLs, but an LLM may wrap the
            # returned absolute macOS path directly as <img src="/Users/...">.
            # QTextDocument passes that through as a scheme-less QUrl, so
            # accept it explicitly.
            if url.isLocalFile() or os.path.isabs(url_str):
                local_path = url.toLocalFile() if url.isLocalFile() else url_str
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
                self._invalidate(self._tool_block_index(tool_id))
                self._render()
                return
            if anchor.startswith("reasoning://"):
                idx = int(anchor[len("reasoning://"):])
                if idx in self._expanded_reasoning:
                    self._expanded_reasoning.discard(idx)
                else:
                    self._expanded_reasoning.add(idx)
                self._invalidate(idx)
                self._render()
                return
            if anchor.startswith(("http://", "https://")):
                QDesktopServices.openUrl(QUrl(anchor))
                return
        super().mousePressEvent(event)

    def append_message(self, role: str, content, *, reasoning_content: str = None, render: bool = True):
        if role == "user":
            self._messages.append({"role": "user", "content": content})
            self._html_cache.append(None)
        elif role == "assistant":
            if content:
                msg = {"role": "assistant", "content": content}
                if reasoning_content:
                    msg["reasoning_content"] = reasoning_content
                self._messages.append(msg)
                self._html_cache.append(None)
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
            self._html_cache.append(None)
        elif role == "tool_result":
            tool_call_id = content.get("tool_call_id")
            idx = self._tool_block_index(tool_call_id)
            if idx >= 0:
                msg = self._messages[idx]
                msg["result"] = content.get("content", "")
                msg["declined"] = content.get("declined", False)
                # Mutated in place: its "(running…)" header is now stale.
                self._invalidate(idx)
        if render:
            self._render()

    def render_now(self):
        """Force a full re-render. Use after a batch of append_message(render=False)."""
        self._render()

    def clear(self):
        self._messages = []
        self._html_cache = []
        self._expanded_tools = set()
        self._expanded_reasoning = set()
        self._auto_scroll = True
        super().clear()

    def _on_scroll_changed(self, value: int):
        """Update the pinned-to-bottom flag from a *genuine* user scroll.

        Suppressed while _rendering: setHtml() programmatically resets the bar
        to 0, which is not a user action and must not clear our auto-scroll
        intent (the bug that made the view snap back up to old history).
        """
        if self._rendering:
            return
        sb = self.verticalScrollBar()
        self._auto_scroll = value >= sb.maximum() - _BOTTOM_MARGIN

    def _render(self):
        scrollbar = self.verticalScrollBar()
        prev_pos = scrollbar.value()
        # setHtml() rebuilds the document and resets the scrollbar to the top.
        # Guard valueChanged for the setHtml + position-restore so that spurious
        # reset-to-0 isn't read as "the user scrolled up." The deferred
        # scroll-to-bottom below runs *after* the guard lifts, so its
        # valueChanged correctly re-arms _auto_scroll.
        self._rendering = True
        self.setHtml(self._build_html())
        if self._auto_scroll:
            self._rendering = False
            # maximum() is stale until Qt lays out the new document, so defer.
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(
                self.verticalScrollBar().maximum()))
        else:
            scrollbar.setValue(prev_pos)
            self._rendering = False

    # ── render cache ──────────────────────────────────────────────────
    # Anything that changes a message's *rendered output* must invalidate it:
    # content mutation (a tool result arriving), expand/collapse toggles, and
    # theme changes (which alter the pygments formatter, not just the CSS).

    def _invalidate(self, idx: int):
        """Mark one message as needing a re-render."""
        if 0 <= idx < len(self._html_cache):
            self._html_cache[idx] = None

    def _invalidate_all(self):
        """Mark every message as needing a re-render."""
        self._html_cache = [None] * len(self._messages)

    def _tool_block_index(self, tool_call_id: str) -> int:
        """Index of the tool_block with *tool_call_id*, or -1."""
        for i in range(len(self._messages) - 1, -1, -1):
            msg = self._messages[i]
            if (msg.get("role") == "tool_block"
                    and msg.get("tool_call_id") == tool_call_id):
                return i
        return -1

    def _build_html(self) -> str:
        # Keep the cache aligned with _messages even if a caller appended
        # directly (tests do this), so a stale short cache can't misindex.
        if len(self._html_cache) != len(self._messages):
            self._html_cache += [None] * (len(self._messages) - len(self._html_cache))
            del self._html_cache[len(self._messages):]

        parts = [f"<html><head><meta charset='utf-8'><style>{_build_css(self._theme)}</style></head><body>"]
        for idx, msg in enumerate(self._messages):
            cached = self._html_cache[idx]
            if cached is None:
                cached = self._render_msg(msg, idx)
                self._html_cache[idx] = cached
            parts.append(cached)
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
                if lang and lang != "text":
                    if lang not in self._lexer_cache:
                        self._lexer_cache[lang] = get_lexer_by_name(lang, stripnl=False)
                    lexer = self._lexer_cache[lang]
                else:
                    lexer = TextLexer()
                formatter = self._pygments_formatter or HtmlFormatter(
                    style=self._theme.get("pygments_style", "friendly"),
                    noclasses=True, nobackground=True,
                )
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
