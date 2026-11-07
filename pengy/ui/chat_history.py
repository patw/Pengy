"""Chat history sidebar for Pengy."""
import html
import math
import threading
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QListWidget,
    QListWidgetItem, QMenu, QFrame, QLabel, QHBoxLayout,
    QMessageBox, QComboBox, QLineEdit, QStackedWidget,
)
from PySide6.QtCore import Signal, Qt, QTimer, QSize
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QTextDocument

from pengy.core.chat_search import parse_query, search_chats
from pengy.ui.theme import get_theme, scaled_size
from pengy.ui.icons import apply_button_icon, themed_icon


class _ResultsList(QListWidget):
    """Search results whose rich-text rows re-wrap when the sidebar is resized."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout_rows()

    def relayout_rows(self):
        for i in range(self.count()):
            self.relayout_row(self.item(i))

    def relayout_row(self, item):
        # QLabel.heightForWidth() undercounts wrapped rich text, so measure the
        # same HTML in a QTextDocument at the width the row will really get.
        width = max(50, self.viewport().width() - 4)
        label = self.itemWidget(item)
        if label is None:
            return
        margins = label.contentsMargins()
        doc = QTextDocument()
        doc.setDefaultFont(label.font())
        doc.setDocumentMargin(0)
        doc.setHtml(label.text())
        doc.setTextWidth(width - 8 - margins.left() - margins.right())
        height = math.ceil(doc.size().height()) + margins.top() + margins.bottom() + 8
        item.setSizeHint(QSize(width, height))


# Result rows keep (hit message indexes, search words) under this role.
_HITS_ROLE = Qt.ItemDataRole.UserRole + 1


def highlight(text: str, spans: list) -> str:
    """HTML-escape *text*, bolding the [start, end) *spans*."""
    out, pos = [], 0
    for start, end in spans:
        if start < pos:
            continue
        out.append(html.escape(text[pos:start]))
        out.append("<b>" + html.escape(text[start:end]) + "</b>")
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out)


class ChatHistoryWidget(QWidget):
    """Widget for managing chat history in the sidebar."""

    chat_selected = Signal(str)
    # A search result was clicked: chat id, the hit messages' indexes in
    # chat["messages"], and the search words to mark in them.
    search_hit_selected = Signal(str, list, list)
    new_chat_requested = Signal()
    settings_requested = Signal()
    tasks_requested = Signal()
    model_changed = Signal(str)
    effort_changed = Signal(str)
    edit_requested = Signal(str)
    # (generation, event) from the search thread; stale generations are dropped.
    _search_event = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_chat_id = None
        self._current_model = ""
        self._theme = get_theme()
        self._search_generation = 0
        self._search_cancel = threading.Event()
        self.setup_ui()
        self.apply_theme(self._theme)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # New Chat, Settings and Tasks share one row (Settings and Tasks as
        # icon buttons), which frees the row the search box sits in.
        top_row = QHBoxLayout()
        top_row.setSpacing(4)
        self.new_chat_btn = QPushButton("New Chat")
        self.new_chat_btn.setFixedHeight(36)
        self.new_chat_btn.clicked.connect(lambda: self.new_chat_requested.emit())
        top_row.addWidget(self.new_chat_btn, 1)

        self.settings_btn = QPushButton()
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setAccessibleName("Settings")
        self.settings_btn.clicked.connect(lambda: self.settings_requested.emit())
        top_row.addWidget(self.settings_btn)

        self.tasks_btn = QPushButton()
        self.tasks_btn.setToolTip("Tasks — run a saved prompt template")
        self.tasks_btn.setAccessibleName("Tasks")
        self.tasks_btn.clicked.connect(lambda: self.tasks_requested.emit())
        top_row.addWidget(self.tasks_btn)
        layout.addLayout(top_row)

        # Search: typing filters the list by title; Enter searches messages.
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search chats (Ctrl+K)")
        self.search_input.setToolTip(
            "Type to filter by title. Press Enter to search message text too.\n"
            "Words can be in any order; \"quote\" an exact phrase.")
        self.search_input.setClearButtonEnabled(True)
        self._search_icon_action = self.search_input.addAction(
            themed_icon("search", self._theme["muted"]), QLineEdit.ActionPosition.LeadingPosition)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.search_input.returnPressed.connect(self.run_search)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search_input,
                  context=Qt.ShortcutContext.WidgetShortcut,
                  activated=self.search_input.clear)
        layout.addWidget(self.search_input)

        layout.addSpacing(2)

        # Chat list, and the search results that replace it while a message
        # search is showing.
        self.list_stack = QStackedWidget()
        self.chat_list = QListWidget()
        self.chat_list.itemClicked.connect(self.on_item_clicked)
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self.show_context_menu)
        self.list_stack.addWidget(self.chat_list)

        results_page = QWidget()
        results_layout = QVBoxLayout(results_page)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(4)
        self.search_status = QLabel("")
        self.search_status.setWordWrap(True)
        results_layout.addWidget(self.search_status)
        self.results_list = _ResultsList()
        self.results_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.results_list.itemClicked.connect(self._on_result_clicked)
        results_layout.addWidget(self.results_list, 1)
        self.list_stack.addWidget(results_page)
        layout.addWidget(self.list_stack, 1)
        self._search_event.connect(self._on_search_event)

        layout.addSpacing(8)

        # Quick Settings panel
        qs_frame = QFrame()
        qs_frame.setFrameShape(QFrame.Shape.StyledPanel)
        qs_frame.setFrameShadow(QFrame.Shadow.Raised)
        qs_layout = QVBoxLayout(qs_frame)
        qs_layout.setContentsMargins(8, 8, 8, 8)
        qs_layout.setSpacing(4)

        status_row = QWidget()
        status_row_layout = QHBoxLayout(status_row)
        status_row_layout.setContentsMargins(0, 0, 0, 0)
        status_row_layout.setSpacing(6)

        self.status_label = QLabel("Status")
        self.status_label.setStyleSheet(f"font-weight: bold; color: {self._theme['fg']};")
        status_row_layout.addWidget(self.status_label)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {self._theme['success_soft']}; font-size: 14px;")
        status_row_layout.addWidget(self.status_dot)

        self.status_text = QLabel("Idle")
        self.status_text.setStyleSheet(f"color: {self._theme['fg']};")
        status_row_layout.addWidget(self.status_text)
        status_row_layout.addStretch()

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(500)
        self._blink_timer.timeout.connect(self._blink_dot)
        self._dot_phase = True

        qs_layout.addWidget(status_row)

        qs_divider = QFrame()
        qs_divider.setFrameShape(QFrame.Shape.HLine)
        qs_divider.setFrameShadow(QFrame.Shadow.Sunken)
        qs_layout.addWidget(qs_divider)

        model_row = QWidget()
        model_row_layout = QHBoxLayout(model_row)
        model_row_layout.setContentsMargins(0, 0, 0, 0)
        model_row_layout.setSpacing(6)

        self.model_label = QLabel("Model:")
        self.model_label.setStyleSheet(f"color: {self._theme['fg']};")
        model_row_layout.addWidget(self.model_label)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setMinimumWidth(scaled_size(120, self._theme))
        self.model_combo.setToolTip(
            "Model used for this tab's messages. Populated from the saved "
            "model list — use Settings → Fetch to refresh it."
        )
        # Both a popup selection and a typed name (Enter / focus-out) commit.
        self.model_combo.activated.connect(lambda _index: self._on_model_commit())
        self.model_combo.lineEdit().editingFinished.connect(self._on_model_commit)
        model_row_layout.addWidget(self.model_combo, 1)

        qs_layout.addWidget(model_row)

        self.model_hint_label = QLabel("")
        self.model_hint_label.setWordWrap(True)
        self.model_hint_label.setStyleSheet(f"color: {self._theme['muted']}; font-size: 9pt;")
        self.model_hint_label.hide()  # shown only when set_models() has no cached list
        qs_layout.addWidget(self.model_hint_label)

        effort_row = QHBoxLayout()
        self.effort_label = QLabel("Effort:")
        effort_row.addWidget(self.effort_label)
        self.effort_combo = QComboBox()
        self.effort_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.effort_combo.setMinimumContentsLength(10)
        for label, value in [("Global setting", "global"), ("Provider default", ""),
                             ("Off / none", "none"), ("Low", "low"), ("Medium", "medium"),
                             ("High", "high"), ("Extra high", "xhigh"), ("Max", "max")]:
            self.effort_combo.addItem(label, value)
        self.effort_combo.setToolTip(
            "Reasoning effort for this tab; saved with the chat. Global setting follows Settings. "
            "Provider default sends no hint. Supported levels depend on the model/provider. "
            "Changes apply to the next message.")
        self.effort_combo.activated.connect(
            lambda _index: self.effort_changed.emit(self.effort_combo.currentData()))
        effort_row.addWidget(self.effort_combo, 1)
        qs_layout.addLayout(effort_row)

        self.tokens_label = QLabel("Tokens: —")
        self.tokens_label.setStyleSheet(f"color: {self._theme['fg']};")
        qs_layout.addWidget(self.tokens_label)
        self.rate_label = QLabel("Last: — tok/s")
        self.rate_label.setToolTip(
            "Last final API response only: API-reported output tokens divided by successful request "
            "wall time. Includes network latency, prompt processing and reasoning; excludes tool "
            "execution, confirmation waits and retry backoff. Not a pure decoding speed. "
            "— means no measurement available.")
        qs_layout.addWidget(self.rate_label)

        layout.addWidget(qs_frame)

    def apply_theme(self, theme: dict[str, str]):
        self._theme = theme
        self.setStyleSheet(f"""
            ChatHistoryWidget {{ background-color: {theme['panel']}; color: {theme['fg']}; }}
            QFrame {{ border-color: {theme['border']}; }}
        """)
        control_h = scaled_size(36, theme)
        self.new_chat_btn.setFixedHeight(control_h)
        for button in (self.settings_btn, self.tasks_btn):
            button.setFixedSize(control_h, control_h)
        self.search_input.setFixedHeight(scaled_size(30, theme))
        self._search_icon_action.setIcon(themed_icon("search", theme["muted"]))
        self.search_status.setStyleSheet(f"color: {theme['muted']};")
        self.status_label.setStyleSheet(f"font-weight: bold; color: {theme['fg']};")
        apply_button_icon(self.new_chat_btn, "new-chat", theme,
                          size=scaled_size(16, theme))
        apply_button_icon(self.settings_btn, "settings", theme,
                          size=scaled_size(16, theme))
        apply_button_icon(self.tasks_btn, "tasks", theme,
                          size=scaled_size(16, theme))
        for label in (self.model_label, self.effort_label, self.tokens_label, self.rate_label):
            label.setStyleSheet(f"color: {theme['fg']};")
        self.model_hint_label.setStyleSheet(f"color: {theme['muted']}; font-size: 9pt;")
        self.load_chats([]) if False else None  # keep method import-safe; rows are restyled on reload

    def _make_item_widget(self, chat_id: str, title: str) -> QWidget:
        """Create the inline widget for a chat list row."""
        row = QWidget()
        row.setStyleSheet(f"background-color: {self._theme['panel']}; color: {self._theme['fg']};")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 2, 2, 2)
        row_layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {self._theme['fg']};")
        title_label.setMinimumWidth(0)
        row_layout.addWidget(title_label, 1)

        btn_style = f"""
            QPushButton {{
                background-color: transparent;
                color: {self._theme['fg']};
                border: none;
                border-radius: 4px;
                font-size: 12px;
            }}
            QPushButton:hover {{ background-color: {self._theme['hover']}; }}
        """

        action_size = scaled_size(24, self._theme)
        edit_btn = QPushButton()
        edit_btn.setFixedSize(action_size, action_size)
        edit_btn.setToolTip("Edit chat (rename, generate title, export)")
        edit_btn.setAccessibleName("Edit chat")
        apply_button_icon(edit_btn, "edit", self._theme,
                          size=scaled_size(14, self._theme))
        edit_btn.setStyleSheet(btn_style)
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(chat_id))
        row_layout.addWidget(edit_btn)

        del_btn = QPushButton()
        del_btn.setFixedSize(action_size, action_size)
        del_btn.setToolTip("Delete chat")
        del_btn.setAccessibleName("Delete chat")
        apply_button_icon(del_btn, "delete", self._theme,
                          size=scaled_size(14, self._theme),
                          active_role="danger")
        del_btn.setStyleSheet(btn_style)
        del_btn.clicked.connect(lambda: self._delete_by_id(chat_id))
        row_layout.addWidget(del_btn)

        return row

    def load_chats(self, chats: list[dict]):
        """Populate the chat list."""
        self.chat_list.clear()
        for chat in chats:
            chat_id = chat["id"]
            title = chat.get("title", "Untitled")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, chat_id)
            widget = self._make_item_widget(chat_id, title)
            item.setSizeHint(widget.sizeHint())
            self.chat_list.addItem(item)
            self.chat_list.setItemWidget(item, widget)
        if self.search_input.text():
            self._apply_title_filter()

    def add_chat(self, chat_id: str, title: str):
        """Insert one new row at the top, without touching the other rows.

        A freshly created chat is always the newest (chats sort newest-first),
        so it always belongs at index 0 — no need to pay load_chats()'s full
        clear-and-rebuild, which costs one QWidget (with icon-bearing buttons)
        per *existing* row and was the dominant cost behind "New Chat feels
        slow" once the sidebar has more than a handful of chats.
        """
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, chat_id)
        widget = self._make_item_widget(chat_id, title)
        item.setSizeHint(widget.sizeHint())
        self.chat_list.insertItem(0, item)
        self.chat_list.setItemWidget(item, widget)

    # ── Search ────────────────────────────────────────────────────────────

    def focus_search(self):
        """Ctrl+K: put the cursor in the search box."""
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_input.selectAll()

    def _on_search_text_changed(self, text: str):
        # Any edit makes shown message results stale: back to the (title-
        # filtered) chat list until Enter is pressed again.
        self._cancel_search()
        self.list_stack.setCurrentIndex(0)
        self._apply_title_filter()

    def _apply_title_filter(self):
        """Hide rows whose title lacks any search word (instant, no file reads)."""
        terms = parse_query(self.search_input.text())
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            title = self._item_title(item).lower()
            self.chat_list.setRowHidden(i, not all(t in title for t in terms))

    def _cancel_search(self):
        self._search_generation += 1
        self._search_cancel.set()

    def run_search(self):
        """Enter: search titles and message text on a background thread."""
        query = self.search_input.text()
        if not parse_query(query):
            return
        self._cancel_search()
        cancel = self._search_cancel = threading.Event()
        generation = self._search_generation
        self.results_list.clear()
        self.search_status.setText("Searching…")
        self.list_stack.setCurrentIndex(1)

        def run():
            try:
                for event in search_chats(query, cancelled=cancel.is_set):
                    if cancel.is_set():
                        return
                    self._search_event.emit(generation, event)
            except Exception as exc:  # noqa: BLE001 - shown in the sidebar
                self._search_event.emit(generation, {"type": "error", "message": str(exc)})

        threading.Thread(target=run, daemon=True).start()

    def _on_search_event(self, generation: int, event: dict):
        if generation != self._search_generation:
            return
        kind = event["type"]
        if kind == "result":
            self._add_result_row(event)
            n = self.results_list.count()
            self.search_status.setText(f"{n} chat{'s' if n != 1 else ''} found, searching…")
        elif kind == "progress":
            n = self.results_list.count()
            self.search_status.setText(
                f"{n} chat{'s' if n != 1 else ''} found, searching… "
                f"{event['scanned']}/{event['total']}")
        elif kind == "done":
            n = event["results"]
            text = f"{n} chat{'s' if n != 1 else ''} found" if n else "No chats match."
            if event.get("truncated"):
                text = f"First {n} chats shown. Add words to narrow the search."
            self.search_status.setText(text + "  Esc to go back.")
        elif kind == "error":
            self.search_status.setText(f"Search failed: {event['message']}")

    def _add_result_row(self, result: dict):
        terms = parse_query(self.search_input.text())
        title = result.get("title") or "Untitled"
        title_spans = []
        low = title.lower()
        for term in terms:
            i = low.find(term)
            if i >= 0 and len(low) == len(title):
                title_spans.append([i, i + len(term)])
        lines = [f"<b>{highlight(title, sorted(title_spans)) if title_spans else html.escape(title)}</b>"]
        when = str(result.get("created_at", ""))[:10]
        muted = self._theme["muted"]
        if when:
            lines[0] += f" <span style='color:{muted};'>{html.escape(when)}</span>"
        for hit in result.get("hits", []):
            who = "You" if hit["role"] == "user" else "AI"
            lines.append(f"<span style='color:{muted};'>{who}:</span> "
                         f"{highlight(hit['snippet'], hit['spans'])}")
        label = QLabel("<br>".join(lines))
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setContentsMargins(4, 4, 4, 4)
        label.setStyleSheet(f"color: {self._theme['fg']}; background: transparent;")
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, result["id"])
        item.setData(_HITS_ROLE, ([h["index"] for h in result.get("hits", [])], terms))
        self.results_list.addItem(item)
        self.results_list.setItemWidget(item, label)
        self.results_list.relayout_row(item)

    def _on_result_clicked(self, item: QListWidgetItem):
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        sources, terms = item.data(_HITS_ROLE) or ([], [])
        self.current_chat_id = chat_id
        self.search_hit_selected.emit(chat_id, list(sources), list(terms))

    def on_item_clicked(self, item: QListWidgetItem):
        """Handle chat selection."""
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        self.current_chat_id = chat_id
        self.chat_selected.emit(chat_id)

    def show_context_menu(self, position):
        """Show context menu for chat items."""
        item = self.chat_list.itemAt(position)
        if not item:
            return

        menu = QMenu(self)
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        edit_action = QAction("Edit…", self)
        edit_action.triggered.connect(lambda: self.edit_requested.emit(chat_id))
        menu.addAction(edit_action)
        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self._delete_item(item))
        menu.addAction(delete_action)
        menu.exec(self.chat_list.mapToGlobal(position))

    def _delete_by_id(self, chat_id: str):
        """Delete a chat by ID."""
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                self._delete_item(item)
                return

    def remove_chat(self, chat_id: str):
        """Remove one row by ID, without confirmation or touching storage.

        For callers that already deleted the chat from disk themselves (e.g.
        MainWindow._close_tab() discarding an empty "New Chat" tab) and just
        need the sidebar to stop showing a row for it. A no-op if the chat
        was never shown here (add_chat() wasn't called for it, or it's
        already been removed).
        """
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                self.chat_list.takeItem(i)
                if self.current_chat_id == chat_id:
                    self.current_chat_id = None
                return

    def _item_title(self, item: QListWidgetItem) -> str:
        """Best-effort title for a row, for use in prompts."""
        widget = self.chat_list.itemWidget(item)
        label = widget.findChild(QLabel) if widget else None
        return label.text() if label else "this chat"

    def _delete_item(self, item: QListWidgetItem):
        """Remove a chat item from the list and storage, after confirming."""
        chat_id = item.data(Qt.ItemDataRole.UserRole)

        # Deletion is immediate and unrecoverable, and the 🗑 button sits on
        # every row — one stray click used to be enough to lose a chat.
        reply = QMessageBox.question(
            self,
            "Delete Chat",
            f'Delete "{self._item_title(item)}"?\n\nThis cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        from pengy.core.chat_manager import delete_chat
        delete_chat(chat_id)
        self.chat_list.takeItem(self.chat_list.row(item))
        if self.current_chat_id == chat_id:
            self.current_chat_id = None

    def select_chat_by_id(self, chat_id: str):
        """Select a chat by ID."""
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                self.chat_list.setCurrentItem(item)
                self.current_chat_id = chat_id
                break

    def update_chat_title(self, chat_id: str, title: str):
        """Update the title label of a chat row in place."""
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                widget = self.chat_list.itemWidget(item)
                if widget:
                    label = widget.findChild(QLabel)
                    if label:
                        label.setText(title)
                break

    def set_thinking(self, thinking: bool):
        """Toggle the status dot between idle (green) and thinking (red blinking)."""
        if thinking:
            self._dot_phase = True
            self.status_dot.setStyleSheet(f"color: {self._theme['danger']}; font-size: 14px;")
            self.status_text.setText("Thinking…")
            self._blink_timer.start()
        else:
            self._blink_timer.stop()
            self.status_dot.setStyleSheet(f"color: {self._theme['success_soft']}; font-size: 14px;")
            self.status_text.setText("Idle")

    def set_tool_running(self, running: bool):
        """Show a solid orange dot while a tool is executing."""
        if running:
            self._blink_timer.stop()
            self.status_dot.setStyleSheet(f"color: {self._theme['running']}; font-size: 14px;")
            self.status_text.setText("Running Tool…")
            # Force an immediate synchronous repaint so the orange bubble is
            # visible on screen before the caller unblocks the worker thread.
            # Otherwise QTimer.singleShot(0, …) races with Qt's paint cycle
            # and the dot can flip straight from red (thinking) → red again
            # (tool result) without ever painting the orange state.
            self.status_dot.repaint()
        else:
            # Revert to thinking (red blinking) — caller should have set that
            self._dot_phase = True
            self.status_dot.setStyleSheet(f"color: {self._theme['danger']}; font-size: 14px;")
            self.status_text.setText("Thinking…")
            self._blink_timer.start()

    def set_retrying(self, text: str):
        """Transient status while the LLM backs off and retries (429/529).

        Deliberately not blinking and not "Running Tool…": this is a network
        backoff pause, not local work. The next tool_request/tool_result/
        final_response event overwrites it via the normal status setters.
        """
        self._blink_timer.stop()
        self.status_dot.setStyleSheet(f"color: {self._theme['muted']}; font-size: 14px;")
        self.status_text.setText(text)

    def _blink_dot(self):
        self._dot_phase = not self._dot_phase
        color = self._theme['danger'] if self._dot_phase else "transparent"
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 14px;")

    def set_models(self, models: list[str], current: str):
        """Populate the model dropdown, keeping *current* selected."""
        items = list(models)
        if current and current not in items:
            items.insert(0, current)
        self.model_combo.clear()
        self.model_combo.addItems(items)
        if current:
            self.model_combo.setCurrentText(current)
        self._current_model = current or self.model_combo.currentText()
        # Nudge the user toward Fetch when there's no cached list to pick
        # from. An empty QLabel still claims a line of layout height, so hide
        # it outright once populated instead of just clearing its text --
        # otherwise it leaves a permanent gap above the effort selector.
        if models:
            self.model_hint_label.setText("")
            self.model_hint_label.hide()
        else:
            self.model_hint_label.setText(
                "No cached model list — use Settings → Fetch to populate."
            )
            self.model_hint_label.show()

    def _on_model_commit(self):
        """Emit model_changed when the user commits a (possibly typed) model."""
        model = self.model_combo.currentText().strip()
        if not model or model == self._current_model:
            return
        self._current_model = model
        self.model_changed.emit(model)

    def update_quick_settings(self, model: str, effort: str):
        """Update the quick settings display."""
        if model:
            self.model_combo.setCurrentText(model)
            self._current_model = model
        previous = self.effort_combo.blockSignals(True)
        try:
            index = self.effort_combo.findData(effort)
            self.effort_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.effort_combo.blockSignals(previous)

    def update_response_rate(self, rate):
        valid = isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0
        self.rate_label.setText(f"Last: {rate:.1f} tok/s" if valid else "Last: — tok/s")

    def update_token_usage(self, prompt: int, completion: int):
        """Show the chat's cumulative token usage (summed across all turns)."""
        if prompt or completion:
            self.tokens_label.setText(f"Tokens: {prompt:,} in / {completion:,} out")
        else:
            self.tokens_label.setText("Tokens: —")
