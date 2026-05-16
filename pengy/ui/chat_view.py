"""Chat view with markdown rendering for Pengy."""
import re
from PySide6.QtWidgets import QTextBrowser
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor, QTextCharFormat, QTextBlockFormat, QBrush, QColor

import markdown
from pygments import highlight
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.formatters import HtmlFormatter


class ChatView(QTextBrowser):
    """Markdown-rendering chat view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_formatting()

    def setup_formatting(self):
        """Configure the text browser appearance."""
        font = QFont("Monospace", 10)
        self.setFont(font)

        self.setStyleSheet("""
            QTextBrowser {
                background-color: #ffffff;
                color: #1e1e2e;
                border: none;
                padding: 0;
            }
        """)

        self.md_extensions = [
            "fenced_code",
            "codehilite",
            "tables",
            "footnotes",
        ]

    def append_message(self, role: str, content):
        """Append a message to the chat view."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if role == "user":
            self._append_user_message(cursor, content)
        elif role == "assistant":
            self._append_assistant_message(cursor, content)
        elif role == "tool":
            self._append_tool_message(cursor, content)
        elif role == "tool_request":
            self._append_tool_request(cursor, content)

        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _append_user_message(self, cursor: QTextCursor, content: str):
        """Append a user message."""
        cursor.insertBlock()
        label_fmt = QTextCharFormat()
        label_fmt.setForeground(Qt.GlobalColor.darkBlue)
        label_fmt.setFontWeight(QFont.Weight.Bold)
        label_fmt.setFontPointSize(9)
        cursor.insertText("You 🧑", label_fmt)

        cursor.insertBlock()
        body_fmt = QTextCharFormat()
        body_fmt.setForeground(Qt.GlobalColor.black)
        body_fmt.setFont(self.font())
        cursor.insertText(content.replace("\n", "\n"), body_fmt)

    def _append_assistant_message(self, cursor: QTextCursor, content: str):
        """Append an assistant message with markdown rendering."""
        if not content:
            return
        cursor.insertBlock()
        label_fmt = QTextCharFormat()
        label_fmt.setForeground(Qt.GlobalColor.darkGreen)
        label_fmt.setFontWeight(QFont.Weight.Bold)
        label_fmt.setFontPointSize(9)
        cursor.insertText("Assistant 🤖", label_fmt)

        cursor.insertBlock()
        html_content = self._render_markdown(content)
        cursor.insertHtml(html_content)
        cursor.insertBlock(QTextBlockFormat())

    def _append_tool_request(self, cursor: QTextCursor, tool_info: dict):
        """Show a tool request notification."""
        cursor.insertBlock()
        cursor.insertBlock()
        name = tool_info.get("name", "unknown")
        args = tool_info.get("args", {})
        args_str = ", ".join(f"{k}={v}" for k, v in args.items())
        if len(args_str) > 40:
            args_str = args_str[:39] + "…"
        label = f"🔧 Using tool: {name} [{args_str}]" if args_str else f"🔧 Using tool: {name}"
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(160, 80, 0))
        fmt.setFontWeight(QFont.Weight.Bold)
        cursor.insertText(label, fmt)

    def _append_tool_message(self, cursor: QTextCursor, content: str):
        """Append a tool result message."""
        cursor.insertBlock()
        cursor.insertBlock()
        fmt = QTextCharFormat()
        fmt.setForeground(Qt.GlobalColor.black)
        fmt.setFontItalic(True)
        cursor.insertText("Tool output", fmt)

        cursor.insertBlock()
        code_fmt = QTextCharFormat()
        code_fmt.setFontFamily("monospace")
        code_fmt.setForeground(Qt.GlobalColor.black)
        cursor.insertText(content.replace("\n", "\n"), code_fmt)

    def _render_markdown(self, text: str) -> str:
        """Convert markdown to HTML with syntax highlighting."""
        md = markdown.Markdown(extensions=self.md_extensions)
        html = md.convert(text)
        html = self._highlight_code(html)
        return html

    def _highlight_code(self, html: str) -> str:
        """Add syntax highlighting to code blocks."""
        def highlight_code(match):
            lang = match.group(1) or "text"
            code = match.group(2)
            try:
                if lang and lang != "text":
                    lexer = get_lexer_by_name(lang, stripnl=False)
                else:
                    lexer = TextLexer()
                formatter = HtmlFormatter(
                    style="monokai",
                    noclasses=True,
                    nobackground=True,
                )
                highlighted = highlight(code, lexer, formatter)
                return f'<pre style="background:#f5f5f5;padding:10px;border-radius:6px;margin:6px 0;overflow-x:auto;">{highlighted}</pre>'
            except Exception:
                return f'<pre style="background:#f5f5f5;padding:10px;border-radius:6px;margin:6px 0;overflow-x:auto;color:#333;">{self._escape_html(code)}</pre>'

        html = re.sub(
            r'<code[^>]*class="language-([^"]*)"[^>]*>(.*?)</code>',
            lambda m: f'<pre style="background:#f5f5f5;padding:10px;border-radius:6px;margin:6px 0;overflow-x:auto;color:#333;">{self._escape_html(m.group(2))}</pre>',
            html,
            flags=re.DOTALL,
        )

        html = re.sub(
            r'<pre><code[^>]*class="language-([^"]*)"[^>]*>(.*?)</code></pre>',
            highlight_code,
            html,
            flags=re.DOTALL,
        )

        html = re.sub(
            r'<pre><code>(.*?)</code></pre>',
            lambda m: f'<pre style="background:#f5f5f5;padding:10px;border-radius:6px;margin:6px 0;overflow-x:auto;color:#333;">{self._escape_html(m.group(1))}</pre>',
            html,
            flags=re.DOTALL,
        )

        return html

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
