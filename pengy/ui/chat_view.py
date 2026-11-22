"""Chat view with markdown rendering for Pengy."""
import base64
import html
import json
import os
import re
import threading
import urllib.request
from PySide6.QtWidgets import QTextBrowser, QMenu
from PySide6.QtCore import Qt, QPoint, QTimer, QUrl, Signal
from PySide6.QtGui import (QDesktopServices, QImage, QMouseEvent, QTextCursor,
                           QTextDocument)

from pengy.ui.image_preview import ImagePreview
from pengy.ui.image_save import save_image_as

import markdown
from pygments import highlight
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.formatters import HtmlFormatter

from pengy.ui.theme import chat_font, get_theme
from pengy.core.config import DEFAULTS as _CONFIG_DEFAULTS


def _default_role_labels() -> tuple[str, str]:
    """The labels a default config renders, with no machine named.

    A ChatView built without MainWindow (tests, embedders) uses this, so the
    fallback is the shipped default -- glyph included, because the renderer adds
    nothing of its own any more.
    """
    return tuple(
        _CONFIG_DEFAULTS[key].replace("{username}", "You")
        .replace("{hostname}", "").strip()
        for key in ("user_label", "assistant_label")
    )


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
.attachment-gallery {{ display:flex; flex-wrap:wrap; gap:6px; margin:4px 0; }}
.attachment-card {{ width:96px; height:96px; object-fit:cover; border-radius:6px; }}
.attachment-unavailable {{ color:{theme['muted']}; font-size:0.85em; padding:6px; border:1px dashed {theme['border']}; }}
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
.notice-card {{ border:1px solid {theme['notice_border']}; padding:6px 10px; margin:6px 0; background-color:{theme['notice_bg']}; color:{theme['notice_fg']}; }}
.search-hit {{ background-color:{theme['selection']}; font-weight:bold; }}
.error-card {{ border:1px solid {theme['error_border']}; padding:6px 10px; margin:6px 0; background-color:{theme['error_bg']}; color:{theme['error_fg']}; }}
h1 {{ font-size:1.4em; }}
h2 {{ font-size:1.3em; }}
h3 {{ font-size:1.1em; }}
h4, h5, h6 {{ font-size:1em; }}
"""


# Within this many pixels of the bottom the view counts as "at the end of the
# feed", so scrolling back to the bottom resumes following instead of leaving
# the view stranded a few pixels short of it.
_BOTTOM_MARGIN = 30

# Scrollbar actions that move the view *towards* older output (QAbstractSlider
# .SliderAction: SingleStepSub=2, PageStepSub=4, SliderToMinimum=5).  They are
# read as "the user wants to read history", immediately, because
# actionTriggered() fires *before* the new value is applied.
_UPWARD_ACTIONS = (2, 4, 5)

# Keys the view handles by scrolling itself; only these can move the viewport.
# (Space pages down in this read-only view, Shift+Space pages up.)
_SCROLL_KEYS = frozenset({
    Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
    Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_Space,
})

# Splits rendered HTML into tags and text, so search words are only marked
# in the text between tags.
_TAG_RE = re.compile(r"(<[^>]*>)")


def mark_terms(html_text: str, terms: list[str]) -> str:
    """Wrap each search word in rendered *html_text* in a search-hit span.

    Words are matched case-insensitively against the escaped text, so a word
    holding ``&`` or ``<`` still finds its entity form.  A word split across
    tags (e.g. pygments tokens in a code block) is not marked.
    """
    # Markdown output leaves quotes bare; user messages escape them as &quot;.
    escaped = {e for t in terms if t
               for e in (html.escape(t, quote=False), html.escape(t).replace("&#x27;", "'"))}
    if not escaped:
        return html_text
    pattern = re.compile("|".join(re.escape(t) for t in sorted(escaped, key=len, reverse=True)),
                         re.IGNORECASE)
    parts = _TAG_RE.split(html_text)
    for i in range(0, len(parts), 2):  # even parts are text, odd are tags
        parts[i] = pattern.sub(lambda m: f'<span class="search-hit">{m.group(0)}</span>', parts[i])
    return "".join(parts)


class ChatView(QTextBrowser):
    """Markdown-rendering chat view with collapsible tool call blocks."""

    _image_loaded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # QTextBrowser sets acceptDrops by default; a read-only one declines
        # the drag in that case, so drops already bubble up to MainWindow.
        # Turn it off explicitly anyway: that fallback is Qt-internal, and an
        # editable transcript would otherwise start swallowing them.
        self.setAcceptDrops(False)
        self._messages = []
        # Rendered HTML per message, parallel to _messages (None = needs render).
        # _build_html() re-ran markdown + pygments over the *entire* history on
        # every append, making a conversation O(n^2) to type into. Markdown and
        # pygments are ~96% of render cost (Qt's setHtml layout is the other 4%),
        # so memoising per message removes almost all of it.
        self._html_cache: list[str | None] = []
        self._theme = get_theme()
        # Role labels, set by MainWindow from config (see apply_identity). The
        # fallback is what a default config renders, glyph included -- the
        # renderer adds nothing of its own any more.
        self._role_labels = _default_role_labels()
        self._expanded_tools = set()
        self._expanded_reasoning: set[int] = set()
        # Search hits being shown: view index -> words to mark (see reveal_hits).
        self._hit_marks: dict[int, list[str]] = {}
        self._image_cache: dict[str, bytes] = {}   # url -> raw bytes (b"" = failed)
        self._image_pending: set[str] = set()
        self._image_lock = threading.Lock()
        self._image_loaded.connect(self._render)
        self._preview = None
        # ── follow-the-feed state ─────────────────────────────────────
        # Intent comes from *real input events only* (wheel, keys, scrollbar
        # drag).  setHtml() replaces the whole document: it resets the bar,
        # Qt reports valueChanged() for that reset, and the maximum keeps
        # moving for several layout passes afterwards — so the scrollbar's
        # geometry is not evidence of what the user wants.  The previous
        # design inferred intent from it (is value() near maximum()?) and the
        # transient reset-to-0 was read as "the user scrolled up": the pin was
        # cleared, and every render after that restored a stale pixel offset,
        # leaving the view stranded in history while new output arrived.
        self._follow = True
        # When not following: (document position, sub-line offset) of the
        # character at the top of the viewport, re-applied after each render so
        # the reading spot holds still while output grows below it.
        self._pending_anchor: tuple[int, int] | None = None
        self._settle_queued = False
        self._dragging = False         # user is holding the scrollbar thumb
        self._render_pending = False   # a render was deferred by that drag
        bar = self.verticalScrollBar()
        bar.actionTriggered.connect(self._on_scroll_action)
        bar.rangeChanged.connect(self._on_range_changed)
        bar.sliderPressed.connect(self._on_slider_pressed)
        bar.sliderMoved.connect(self._on_slider_moved)
        bar.sliderReleased.connect(self._on_slider_released)
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

    def apply_identity(self, user: str, assistant: str):
        """Set the human / assistant role labels and re-render.

        Labels belong to the view and the node, never to the chat file (see
        pengy.core.config.chat_identity for the derivation). Cached HTML has to
        be dropped because a label is part of every user and assistant card.
        """
        labels = (str(user), str(assistant))
        if labels == self._role_labels:
            return
        self._role_labels = labels
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

    def _image_at(self, pos):
        """Return the source only when the click is within an image's layout rect."""
        cursor = self.cursorForPosition(pos)
        for offset in (0, -1):
            position = cursor.position() + offset
            if position < 0:
                continue
            candidate = self.textCursor()
            candidate.setPosition(position)
            fmt = candidate.charFormat()
            if not fmt.isImageFormat():
                continue
            start = self.cursorRect(candidate)
            if position + 1 >= self.document().characterCount():
                continue
            after = self.textCursor()
            after.setPosition(position + 1)
            end = self.cursorRect(after)
            if start.y() <= pos.y() < start.y() + start.height() and start.x() <= pos.x() < end.x():
                return fmt.toImageFormat().name()
        return ""

    def _open_image_preview(self, source: str):
        url = QUrl(source)
        image = QImage()
        if url.isLocalFile() or os.path.isabs(source):
            path = url.toLocalFile() if url.isLocalFile() else source
            if path.endswith("/thumbnail-256-v1.jpg"):
                display = os.path.join(os.path.dirname(path), "image-display-v1.jpg")
                if os.path.isfile(display):
                    path = display
            image.load(path)
        elif source.startswith(("http://", "https://")):
            with self._image_lock:
                data = self._image_cache.get(source)
            if data:
                image.loadFromData(data)
        elif source.startswith("data:"):
            resource = self.loadResource(QTextDocument.ResourceType.ImageResource, url)
            if isinstance(resource, QImage):
                image = resource
        if image.isNull():
            return
        if self._preview is not None:
            self._preview.close()
        self._preview = ImagePreview(image, self, source=source, cache=self._image_cache)
        self._preview.show()

    def contextMenuEvent(self, event):
        source = self._image_at(event.pos())
        if not source:
            super().contextMenuEvent(event)
            return
        menu = QMenu(self)
        save_action = menu.addAction("Save Image As…")
        if menu.exec(event.globalPos()) == save_action:
            save_image_as(source, self._image_cache, self)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            source = self._image_at(event.position().toPoint())
            if source:
                self._open_image_preview(source)
                return
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

    def append_message(self, role: str, content, *, reasoning_content: str = None,
                       render: bool = True, source: int | None = None):
        """Add one message.  *source* is the user/assistant message's index in
        ``chat["messages"]``, which search hits point at (see reveal_hits)."""
        if role == "user":
            attachments = []
            if isinstance(content, dict):
                attachments = content.get("attachments") or []
                content = content.get("content", "")
            self._messages.append({"role": "user", "content": content, "attachments": attachments,
                                   "source": source})
            self._html_cache.append(None)
            if render:
                # Sending a message ends a search jump: back to following the chat.
                self._clear_hit_marks()
                self._follow = True
                self._pending_anchor = None
        elif role == "assistant":
            if content:
                msg = {"role": "assistant", "content": content, "source": source}
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
        elif role in ("notice", "error"):
            # A harness card (info notice / failed-turn error), not something the
            # model said. Rendered on its own surface so it does not carry the
            # "Assistant" label and read as an answer.
            if content:
                self._messages.append({"role": role, "content": content})
                self._html_cache.append(None)
        if render:
            self._render()

    def render_now(self):
        """Force a full re-render. Use after a batch of append_message(render=False).

        ``force`` skips the scrollbar-drag deferral: a caller that re-renders on
        purpose (tab opened, chat redacted) must not be told to wait.
        """
        self._render(force=True)

    def clear(self):
        if self._preview is not None:
            self._preview.close()
            self._preview = None
        self._messages = []
        self._html_cache = []
        self._expanded_tools = set()
        self._expanded_reasoning = set()
        self._hit_marks = {}
        self._follow = True
        self._pending_anchor = None
        self._render_pending = False
        super().clear()

    # ── search hits ───────────────────────────────────────────────────

    def reveal_hits(self, sources: list[int], terms: list[str]) -> bool:
        """Mark *terms* in the messages whose chat index is in *sources* and
        scroll the first one into view.  False if none of them is shown here
        (e.g. the chat was redacted since the search ran)."""
        wanted = set(sources)
        found = [i for i, m in enumerate(self._messages)
                 if m.get("source") is not None and m["source"] in wanted]
        self._clear_hit_marks()
        if not found:
            return False
        for idx in found:
            self._hit_marks[idx] = list(terms)
            self._invalidate(idx)
        # Stay put on later renders instead of snapping back to the bottom.
        self._follow = False
        self._pending_anchor = None
        self._render()
        # The anchor's position is only known once Qt has laid out the new
        # document, and this must run after any pending settle.
        QTimer.singleShot(0, self._scroll_to_hit)
        return True

    def _scroll_to_hit(self):
        self.scrollToAnchor("pengy-hit")
        # A little of the previous message above it, so the hit isn't flush
        # against the top edge.
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() - 24)
        # Hold this spot (not the bottom) on the renders that follow.
        self._pending_anchor = self._anchor_top()

    def _clear_hit_marks(self):
        for idx in self._hit_marks:
            self._invalidate(idx)
        self._hit_marks = {}

    # ── follow the feed / hold the reading spot ───────────────────────

    def wheelEvent(self, event):
        # A wheel notch is a genuine scroll: super() applies the new value
        # synchronously, so intent can be read straight afterwards. The settle
        # runs first so the notch lands on the position the reader is actually
        # on — a render leaves the bar at the top until that runs.
        self._end_drag()             # a lost slider release must not freeze the view
        self._flush_pending_scroll()
        super().wheelEvent(event)
        self._note_user_scroll()

    def keyPressEvent(self, event):
        self._end_drag()
        self._flush_pending_scroll()
        super().keyPressEvent(event)
        if event.key() in _SCROLL_KEYS:
            self._note_user_scroll()

    def _on_scroll_action(self, action: int):
        """Scrollbar arrows / trough clicks.

        Fires *before* the new value is applied, so the resulting position is
        only known on the next tick.  An upward action stops following
        immediately — otherwise a render landing in that gap would drag the
        view back to the bottom and undo the click.
        """
        if action in _UPWARD_ACTIONS:
            self._follow = False
            self._pending_anchor = self._anchor_top()
        QTimer.singleShot(0, self._note_user_scroll)

    def _flush_pending_scroll(self):
        """Run a queued settle before input reads or scrolls the viewport.

        A render leaves the bar at the top until the settle runs. Scrolling from
        there — or capturing the reading spot there — is what sent the view to
        the top of the chat: the wheel moved from 0, and the anchor became the
        document start.
        """
        if self._settle_queued:
            self._settle()

    def _on_slider_pressed(self):
        # Holding the thumb: do not rebuild the document under it. setHtml()
        # resets the bar mid-drag, which both fights the drag (the thumb jumps
        # away from the pointer) and loses the drag's own value. Catch up once
        # the user lets go.
        self._end_drag()
        self._flush_pending_scroll()
        self._dragging = True

    def _on_slider_moved(self, value: int):
        # sliderMoved() carries the value the drag is heading for, so it can be
        # judged without waiting for the bar to settle.
        self._note_user_scroll(value)

    def _on_slider_released(self):
        # Read intent and the reading spot while the bar still holds the drag's
        # value: the catch-up render in _end_drag() resets it.
        self._note_user_scroll()
        self._end_drag()

    def _end_drag(self):
        """The thumb is no longer held: apply whatever output arrived meanwhile."""
        if not self._dragging:
            return
        self._dragging = False
        if self._render_pending:
            self._render_pending = False
            self._render()

    def _note_user_scroll(self, value: int | None = None):
        """A real input event moved the viewport: (re)decide whether to follow.

        Following resumes as soon as the user brings the view back to the
        bottom — "give me the live feed again" — and stops the moment they
        move away from it.
        """
        bar = self.verticalScrollBar()
        position = bar.value() if value is None else value
        self._follow = position >= bar.maximum() - _BOTTOM_MARGIN
        if self._follow:
            self._pending_anchor = None
            self._schedule_settle()      # close the last few pixels
        else:
            self._pending_anchor = self._anchor_top()

    def _anchor_top(self) -> tuple[int, int] | None:
        """Document position and sub-line offset of the character at the top of
        the viewport.  None without a laid-out viewport (a hidden tab), where
        the geometry means nothing and must not be recorded."""
        if not self.isVisible() or self.viewport().height() <= 0:
            return None
        cursor = self.cursorForPosition(QPoint(0, 0))
        return (cursor.position(), self.cursorRect(cursor).y())

    def _apply_anchor(self, anchor: tuple[int, int]):
        """Put *anchor* back at the top of the viewport."""
        position, offset = anchor
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        delta = self.cursorRect(cursor).y() - offset
        if delta:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + delta)

    def _schedule_settle(self):
        """Re-assert the scroll position once Qt has finished laying out.

        setHtml() relayouts asynchronously and the scrollbar maximum changes
        over several passes (measured: 0 -> 2986 -> 3307 -> 3266 for one
        append), so a jump to the bottom issued immediately afterwards lands on
        a stale maximum and leaves the view short of the end.
        """
        if not self._settle_queued:
            self._settle_queued = True
            QTimer.singleShot(0, self._settle)

    def _settle(self):
        self._settle_queued = False
        if self._dragging:
            return                       # the user owns the bar right now
        if self._follow:
            bar = self.verticalScrollBar()
            if bar.value() != bar.maximum():
                bar.setValue(bar.maximum())
        elif self._pending_anchor is not None:
            self._apply_anchor(self._pending_anchor)

    def _on_range_changed(self, _minimum: int, _maximum: int):
        # The document is still settling (images resolving, tables reflowing):
        # re-anchor to the position already decided instead of trusting the
        # maximum seen one tick ago.
        if self._follow or self._pending_anchor is not None:
            self._schedule_settle()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_settle()

    def resizeEvent(self, event):
        # Qt re-lays the document out at the new width: keep following the end,
        # or hold the reading spot, instead of letting the resize decide.
        super().resizeEvent(event)
        self._schedule_settle()

    def _render(self, force: bool = False):
        if self._dragging and not force:
            self._render_pending = True  # rebuilt on slider release
            return
        if self._follow:
            self._pending_anchor = None
        # Not following: keep the anchor already held. NEVER re-read the viewport
        # here — setHtml() below resets the bar to the top, so a second render in
        # the same burst (a turn emits several cards back to back) would capture
        # the document start and park the view there. The reading spot is
        # captured when the user moves the bar and nowhere else.
        self.setHtml(self._build_html())
        self._schedule_settle()

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
                if self._hit_marks and idx == min(self._hit_marks):
                    cached = '<a name="pengy-hit"></a>' + cached
                self._html_cache[idx] = cached
            parts.append(cached)
        parts.append("</body></html>")
        return "".join(parts)

    def _render_msg(self, msg: dict, idx: int = 0) -> str:
        role = msg["role"]
        if role == "user":
            body = self._escape_html(str(msg.get("content", ""))).replace("\n", "<br>")
            if idx in self._hit_marks:
                body = mark_terms(body, self._hit_marks[idx])
            cards = []
            for ref in msg.get("attachments", []) or []:
                if not isinstance(ref, dict):
                    continue
                aid = str(ref.get("id", ""))
                if ref.get("kind") == "image" and aid.startswith("sha256:"):
                    from pengy.core.attachments import derivative_path
                    try:
                        thumb = derivative_path(aid, "thumbnail-256-v1.jpg")
                        if thumb.is_file():
                            cards.append(f'<a href="{QUrl.fromLocalFile(str(thumb)).toString()}" title="{self._escape_html(str(ref.get("name", "Image")))}"><img class="attachment-card" src="{QUrl.fromLocalFile(str(thumb)).toString()}" alt="{self._escape_html(str(ref.get("name", "Image")))}"></a>')
                        else:
                            cards.append(f'<span class="attachment-unavailable">Attachment unavailable: {self._escape_html(str(ref.get("name", "Image")))}</span>')
                    except Exception:
                        cards.append('<span class="attachment-unavailable">Attachment unavailable</span>')
                else:
                    cards.append(f'<span class="attachment-unavailable">Unsupported attachment: {self._escape_html(str(ref.get("name", "Attachment")))}</span>')
            gallery = f'<div class="attachment-gallery">{"".join(cards)}</div>' if cards else ""
            return (
                f'<p class="role-user">{self._escape_html(self._role_labels[0])}</p>'
                f'{gallery}<p style="margin:2px 0 10px 0;">{body}</p>'
            )
        if role == "assistant":
            parts = []
            reasoning = msg.get("reasoning_content")
            if reasoning:
                parts.append(self._render_reasoning_block(idx, reasoning))
            html_content = self._render_markdown(msg["content"])
            if idx in self._hit_marks:
                html_content = mark_terms(html_content, self._hit_marks[idx])
            parts.append(
                f'<p class="role-assistant">{self._escape_html(self._role_labels[1])}</p>'
                f'<div style="margin:2px 0 10px 0;">{html_content}</div>'
            )
            return "".join(parts)
        if role == "tool_block":
            return self._render_tool_block(msg)
        if role in ("notice", "error"):
            # Harness cards share one shape; only the class (and so the theme
            # colours) differs between an info notice and a failed turn.
            return (
                f'<div class="{role}-card">'
                f'{self._escape_html(str(msg.get("content", "")))}'
                '</div>'
            )
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
