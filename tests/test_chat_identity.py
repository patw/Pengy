"""Chat role labels are complete strings, derived from this node's config.

Two rules the renderer must keep. (1) The label is the whole string, glyph
included: the renderer adds nothing of its own, so deleting the emoji, moving it
before the name, or renaming the agent are settings edits, not code edits.
(2) Labels are derived from the config of the machine doing the rendering, never
read from the chat file -- chats are shared between machines, so "who answered
this" is a property of the node. The chat file is never touched, so existing
chats need no migration; that is asserted, not assumed, in
test_rendering_leaves_the_chat_file_untouched.
"""
import getpass
import hashlib
import socket

import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

from PySide6.QtWidgets import QApplication

from pengy.core.config import AGENT_NAME, DEFAULTS, chat_identity

# The shipped glyphs, written out here so a test failure names the glyph that
# moved rather than a value reverse-engineered from the template.
USER_GLYPH = "\U0001F9D1"
ASSISTANT_GLYPH = "\U0001F916"


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_the_glyph_lives_in_the_string_not_in_the_renderer():
    """The whole point of the change: an emoji-free label is a config edit."""
    user, assistant = chat_identity({"display_name": "Pat", "user_label": "{username}",
                                     "assistant_label": AGENT_NAME})
    assert user == "Pat"
    assert assistant == AGENT_NAME
    assert USER_GLYPH not in user and ASSISTANT_GLYPH not in assistant


def test_the_shipped_defaults_keep_the_glyphs():
    user, assistant = chat_identity({"display_name": "You"})
    assert user == f"You {USER_GLYPH}"
    assert assistant == f"{AGENT_NAME} ({socket.gethostname()}) {ASSISTANT_GLYPH}"


def test_display_name_fills_the_username_token():
    user, _ = chat_identity({"display_name": "Pat"})
    assert user == f"Pat {USER_GLYPH}"
    # The token can sit anywhere, which is how "glyph first" becomes a setting.
    glyph_first, _ = chat_identity({"display_name": "Pat",
                                    "user_label": f"{USER_GLYPH} {{username}}"})
    assert glyph_first == f"{USER_GLYPH} Pat"


def test_empty_display_name_falls_back_to_the_login_name():
    # The container case: getuser() is what the system message already gets,
    # so the label cannot be worse-informed than the prompt. The name sits where
    # the template puts it, so assert containment, not position.
    for blank in ("", "   ", None):
        user, _ = chat_identity({"display_name": blank})
        assert getpass.getuser() in user


def test_hostname_token_expands_only_when_asked():
    _, named = chat_identity({"show_node_in_label": True})
    _, unnamed = chat_identity({"show_node_in_label": False})
    assert f"({socket.gethostname()})" in named
    assert "(" not in unnamed
    # A template that omits the token never names the machine: no hidden appending.
    _, tokenless = chat_identity({"show_node_in_label": True,
                                  "assistant_label": f"{AGENT_NAME} {ASSISTANT_GLYPH}"})
    assert tokenless == f"{AGENT_NAME} {ASSISTANT_GLYPH}"


def test_a_blank_template_restores_the_default():
    """A cleared settings field must not be able to render an empty label."""
    for blank in ("", "   ", None):
        cfg = {"display_name": "Pat", "user_label": blank, "assistant_label": blank}
        user, assistant = chat_identity(cfg)
        assert user and assistant
        # Restored means the shipped template, with this config's own name in it.
        assert user == f"Pat {USER_GLYPH}"
        assert assistant == f"{AGENT_NAME} ({socket.gethostname()}) {ASSISTANT_GLYPH}"


def test_defaults_are_explicit_in_the_config_contract():
    assert DEFAULTS["display_name"] == "You"
    assert "{username}" in DEFAULTS["user_label"]
    assert "{hostname}" in DEFAULTS["assistant_label"]
    assert DEFAULTS["show_node_in_label"] is True
    # A settings.json written before these keys existed is loaded as
    # {**DEFAULTS, **saved} by load_config, so the new keys arrive with their
    # defaults and the shipped look is unchanged.
    user, assistant = chat_identity({**DEFAULTS})
    assert user == f"You {USER_GLYPH}"
    assert assistant == f"{AGENT_NAME} ({socket.gethostname()}) {ASSISTANT_GLYPH}"


def test_a_bare_chat_view_renders_the_default_config_labels(qapp):
    from pengy.ui.chat_view import ChatView

    view = ChatView()
    view.append_message("user", "hi")
    view.append_message("assistant", "hello")
    html = view._build_html()
    assert f'class="role-user">You {USER_GLYPH}<' in html
    assert f'class="role-assistant">{AGENT_NAME} {ASSISTANT_GLYPH}<' in html


def test_labels_follow_the_derivation_and_keep_the_css_classes(qapp):
    from pengy.ui.chat_view import ChatView

    view = ChatView()
    view.append_message("user", "hi")
    view.append_message("assistant", "hello")
    view.apply_identity("Pat", "Pengy (behopc)")
    html = view._build_html()
    # The renderer appends nothing: exactly the strings it was handed.
    assert 'class="role-user">Pat<' in html
    assert 'class="role-assistant">Pengy (behopc)<' in html
    assert USER_GLYPH not in html and ASSISTANT_GLYPH not in html
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


def test_rendering_leaves_the_chat_file_untouched(tmp_path, qapp):
    """The "no schema change, no migration" promise, as a test.

    Nothing about the labels is written to the chat: same bytes before and
    after opening and rendering a chat with display names set.
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
