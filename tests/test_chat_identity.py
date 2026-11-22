"""Chat role labels are two complete strings, derived from this node's config.

Three rules the tests pin:

1. The label IS the string that gets rendered. The renderer appends no emoji of
   its own, so removing the emoji is a settings edit, not a code edit.
2. Labels come from the *config of the machine rendering*, never from the chat
   file, and nothing is written into chat files. Existing chats are relabelled by
   the same change, with no migration.
3. There are exactly two settings, one per role. The human side is not stored on
   its own, because it is just {username} inside the label -- a separate display
   name would only ever feed a placeholder in the field next to it.
"""
import os
import getpass

import pytest

pytest.importorskip("PySide6", reason="GUI tests require the optional GUI extra")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PENGY_NO_NUDGE", "1")

from PySide6.QtWidgets import QApplication

from pengy.core.config import DEFAULTS, chat_identity
from pengy.ui.chat_view import ChatView


@pytest.fixture(scope="module", autouse=True)
def qapp():
    """Qt aborts on QWidget construction with no QApplication -- fatal, not a skip."""
    return QApplication.instance() or QApplication([])

USER_GLYPH = "\U0001F9D1"
ASSISTANT_GLYPH = "\U0001F916"
USER_DEFAULT = DEFAULTS["user_label"]
ASSISTANT_DEFAULT = DEFAULTS["assistant_label"]


def test_the_shipped_defaults_carry_their_own_glyphs():
    assert USER_GLYPH in USER_DEFAULT
    assert ASSISTANT_GLYPH in ASSISTANT_DEFAULT
    # The node-naming token is in the assistant side, and only there.
    assert "{hostname}" in ASSISTANT_DEFAULT
    assert "{hostname}" not in USER_DEFAULT


def test_labels_are_two_fields_and_nothing_feeds_a_third():
    """No display_name setting: the human name is the {username} token itself."""
    assert "display_name" not in DEFAULTS
    assert {"user_label", "assistant_label", "show_node_in_label"} <= set(DEFAULTS)


def test_default_config_labels_the_human_and_names_the_answering_node():
    user, assistant = chat_identity({**DEFAULTS})
    assert user == f"You {USER_GLYPH}"
    assert assistant == f"Pengy ({os.uname().nodename}) {ASSISTANT_GLYPH}"


def test_the_username_token_expands_to_the_login_name():
    """The one way to name the human, spelled as the token in the label."""
    login = getpass.getuser()   # the same source chat_identity uses
    user, _ = chat_identity({"user_label": "{username}", "assistant_label": "Pengy"})
    assert user == login
    user, _ = chat_identity({"user_label": "{username} \U0001F9D1",
                             "assistant_label": "Pengy"})
    assert user == f"{login} {USER_GLYPH}"


def test_removing_the_emoji_is_a_settings_edit():
    """The emoji the view used to append is now just text in the label."""
    login = getpass.getuser()
    user, assistant = chat_identity({"user_label": "{username}",
                                     "assistant_label": "Pengy"})
    assert USER_GLYPH not in user
    assert ASSISTANT_GLYPH not in assistant
    assert user == login
    assert USER_GLYPH not in user
    assert ASSISTANT_GLYPH not in assistant
    assert assistant == "Pengy"


def test_the_glyph_can_lead_the_name():
    user, assistant = chat_identity({"user_label": f"\U0001F9D1 {{username}}",
                                     "assistant_label": f"\U0001F916 Pengy"})
    assert user.startswith(USER_GLYPH)
    assert assistant.startswith(ASSISTANT_GLYPH)


def test_show_node_in_label_toggles_the_host_in_the_assistant_label():
    _, with_host = chat_identity({"show_node_in_label": True,
                                  "assistant_label": "Pengy{hostname}"})
    _, without = chat_identity({"show_node_in_label": False,
                                "assistant_label": "Pengy{hostname}"})
    assert with_host == f"Pengy ({os.uname().nodename})"
    assert without == "Pengy"
    # Dropping the token from the label omits the host even when enabled.
    _, tokenless = chat_identity({"show_node_in_label": True,
                                  "assistant_label": "Pengy"})
    assert tokenless == "Pengy"


def test_a_blank_field_restores_the_shipped_default():
    """A cleared settings field must not be able to render a label-less chat."""
    empty = chat_identity({"user_label": "", "assistant_label": ""})
    assert empty == chat_identity({**DEFAULTS})
    # Whitespace is blank too: it would otherwise strip down to nothing.
    assert chat_identity({"user_label": "   ", "assistant_label": " \t "}) == empty
    # A label of only the host slot must not render as empty either.
    assert chat_identity({"user_label": "{hostname}",
                          "assistant_label": "{hostname}"})[0].strip() != ""


def test_view_renders_the_labels_verbatim_with_no_glyph_added():
    """The renderer's job is escaping, not composing: the label arrives finished."""
    view = ChatView()
    view.apply_identity("Pat \U0001F9D1", "Pengy (behopc) \U0001F916")
    view.append_message("user", "question")
    view.append_message("assistant", "answer")
    html = "".join(view._html_cache)

    assert f'class="role-user">Pat {USER_GLYPH}</p>' in html
    assert f'class="role-assistant">Pengy (behopc) {ASSISTANT_GLYPH}</p>' in html
    assert "You \U0001F9D1" not in html
    # The class names are a contract: themes and tests select on them.
    assert "role-user" in html and "role-assistant" in html


def test_view_never_appends_glyphs_of_its_own():
    """Label text is the only source of glyphs, so this is the anti-regression net.

    The shipped default carries its glyph, and so does the view built without
    MainWindow (tests, embedders) -- both paths are asserted, since that fallback
    is what a bare ChatView shows.
    """
    view = ChatView()
    view.append_message("user", "q1")
    view.append_message("assistant", "a1")
    assert html_has_label("".join(view._html_cache), USER_GLYPH, ASSISTANT_GLYPH)
    view.apply_identity("Pat", "Pengy (node)")
    view.append_message("user", "q2")
    view.append_message("assistant", "a2")
    # Glyph-free labels stay glyph-free -- the view cannot re-add what config removed.
    assert f"Pat {USER_GLYPH}" not in "".join(view._html_cache)
    assert f"Pengy (node) {ASSISTANT_GLYPH}" not in "".join(view._html_cache)
    # An HTML-escaping edge: a display name is config-sourced, not trusted input.
    view.apply_identity("Pat <b>x</b>", "Pengy & Co")
    view.append_message("user", "q3")
    assert "<b>x</b>" not in "".join(view._html_cache)
    assert "Pat &lt;b&gt;x&lt;/b&gt;" in "".join(view._html_cache)


def html_has_label(html, user_glyph, assistant_glyph):
    return f"{user_glyph}</p>" in html and f"{assistant_glyph}</p>" in html


def test_no_schema_change_and_no_write_to_the_chat(tmp_path):
    """Rendering with a label set must not touch the chat file, and must not
    persist a label into the message records. The proposal's promise is asserted,
    not assumed: existing chats are relabelled by the same change.
    """
    import json

    chat = {"id": "identity-test", "title": "t", "messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]}
    path = tmp_path / "chat.json"
    path.write_text(json.dumps(chat), encoding="utf-8")
    before = path.read_bytes()

    view = ChatView()
    view.apply_identity("Pat", "Pengy (node)")
    for message in chat["messages"]:
        view.append_message(message["role"], message["content"])

    assert path.read_bytes() == before, "rendering must not write to the chat"
    for message in chat["messages"]:
        assert "Pat" not in message.get("content", "")
        assert "node" not in message.get("content", "")
        assert "actor" not in message, "the actor field is a separate issue"


def test_settings_fields_open_showing_the_default_string():
    """The boxes must show the full default, not sit empty.

    An unset setting is stored as "" (so the shipped default stays the default),
    which would leave the field blank and the default shape invisible. The dialog
    prefills with the value actually used, and saving that text back stores ""
    rather than pinning today's default into the config file.
    """
    from pengy.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog({**DEFAULTS})
    assert dialog.user_label_input.text() == USER_DEFAULT
    assert dialog.assistant_label_input.text() == ASSISTANT_DEFAULT
    assert USER_GLYPH in dialog.user_label_input.text()
    assert ASSISTANT_GLYPH in dialog.assistant_label_input.text()

    # Saving the prefilled defaults stores empty: the default is not pinned.
    saved = dialog.get_config()
    assert saved["user_label"] == ""
    assert saved["assistant_label"] == ""

    # An edited label is stored verbatim, and round-trips into the field.
    dialog2 = SettingsDialog({**DEFAULTS})
    dialog2.user_label_input.setText("Pat")
    dialog2.assistant_label_input.setText("Pengy")
    saved = dialog2.get_config()
    assert saved["user_label"] == "Pat"
    assert saved["assistant_label"] == "Pengy"
    reopened = SettingsDialog(saved)
    assert reopened.user_label_input.text() == "Pat"
    assert reopened.assistant_label_input.text() == "Pengy"


def test_settings_has_no_separate_display_name_field():
    """Two fields, one per role -- there is no third name to reconcile."""
    from pengy.ui.settings_dialog import SettingsDialog
    dialog = SettingsDialog({**DEFAULTS})
    assert not hasattr(dialog, "display_name_input")


def test_the_frontends_that_show_labels_share_one_derivation():
    """Desktop and CLI derive labels from one function, so they cannot drift.

    The web UI is deliberately out of scope: its transcript renders message
    bubbles with no role label at all, so there is nothing there to derive --
    asserted, so that a future web label adds the derivation on purpose.
    """
    from pengy.core.config import chat_identity as derive

    cfg = {"user_label": "Me", "assistant_label": "Bot", "show_node_in_label": False}
    assert derive(cfg) == ("Me", "Bot")

    from pengy.cli import main as cli_main
    assert cli_main.chat_identity is derive

    from pengy.web import app as web_app
    assert not hasattr(web_app, "chat_identity"), "web has no role labels to derive"
