"""Chat role labels are derived at render time from this node's config.

The point of the change (issue #3): the transcript should say who is speaking,
and "who" is a property of the machine rendering the chat, not of the stored
message -- chats are shared between machines. So the labels are derived from
config at render time and the chat file is never touched: existing chats are
fixed by the same change and need no migration. That last part is asserted, not
assumed (test_rendering_leaves_the_chat_file_untouched).
"""
import getpass
import hashlib
import socket

import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

from PySide6.QtWidgets import QApplication

from pengy.core.config import AGENT_NAME, DEFAULTS, chat_identity


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_display_name_wins_and_node_is_named_by_default():
    user, assistant = chat_identity({"display_name": "Pat"})
    assert user == "Pat"
    assert assistant == f"{AGENT_NAME} ({socket.gethostname()})"


def test_empty_display_name_falls_back_to_the_login_name():
    # The container case: getuser() is what the system message already gets,
    # so the label cannot be worse-informed than the prompt.
    for blank in ("", "   ", None):
        user, _ = chat_identity({"display_name": blank})
        assert user == getpass.getuser()


def test_node_can_be_left_out_of_the_label():
    _, assistant = chat_identity({"display_name": "Pat", "show_node_in_label": False})
    assert assistant == AGENT_NAME
    assert "(" not in assistant


def test_defaults_are_explicit_in_the_config_contract():
    assert DEFAULTS["display_name"] == ""
    assert DEFAULTS["show_node_in_label"] is True
    # A config file written before this feature existed must behave as before:
    # login name plus the node, and no key errors.
    user, assistant = chat_identity({})
    assert user == getpass.getuser()
    assert assistant == f"{AGENT_NAME} ({socket.gethostname()})"


def test_a_bare_chat_view_keeps_the_shipped_labels(qapp):
    from pengy.ui.chat_view import ChatView

    view = ChatView()
    view.append_message("user", "hi")
    view.append_message("assistant", "hello")
    html = view.test_build_html() if hasattr(view, "test_build_html") else view._build_html()
    assert 'class="role-user">You &#x1F9D1;' in html
    assert 'class="role-assistant">Assistant &#x1F916;' in html


def test_labels_follow_the_derivation_and_keep_the_css_classes(qapp):
    from pengy.ui.chat_view import ChatView

    view = ChatView()
    view.append_message("user", "hi")
    view.append_message("assistant", "hello")
    view.apply_identity("Pat", "Pengy (behopc)")
    html = view._build_html()
    assert 'class="role-user">Pat &#x1F9D1;' in html
    assert 'class="role-assistant">Pengy (behopc) &#x1F916;' in html
    # Themes colour these selectors and a test asserts on them (test_ui.py),
    # so the class names are part of the contract.
    assert ".role-user" in html and ".role-assistant" in html


def test_a_display_name_cannot_inject_html(qapp):
    """The label is config-sourced text rendered into HTML: escape it."""
    from pengy.ui.chat_view import ChatView

    view = ChatView()
    view.append_message("user", "hi")
    view.append_message("assistant", "hello")
    view.apply_identity("<b>Pat</b>", "<i>Pengy</i>")
    html = view._build_html()
    assert "class=\"role-user\"><b>Pat" not in html
    assert "&lt;b&gt;Pat" in html
    assert "&lt;i&gt;Pengy" in html


def test_rendering_leaves_the_chat_file_untouched(tmp_path, monkeypatch, qapp):
    """The "no schema change, no migration" promise, as a test.

    Nothing about the labels is written to the chat: same bytes before and
    after opening and rendering a chat with a display name set.
    """
    import json

    from pengy.ui.chat_view import ChatView

    chat = {"id": "c1", "title": "t", "messages": [
        {"role": "user", "content": "hi", "source": 0},
        {"role": "assistant", "content": "hello", "source": 1},
    ]}
    path = tmp_path / "c1.json"
    path.write_text(json.dumps(chat), encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    view = ChatView()
    view.apply_identity("Pat", "Pengy (behopc)")
    for index, msg in enumerate(chat["messages"]):
        view.append_message(msg["role"], msg["content"], source=msg["source"])
    html = view._build_html()
    assert "Pat" in html and "Pengy (behopc)" in html

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    # And no label ever lands in the message dicts that get saved back out.
    for msg in chat["messages"]:
        assert "Pat" not in json.dumps(msg)
        assert "role_label" not in msg and "actor" not in msg
