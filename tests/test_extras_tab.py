"""Tests for Settings → Extras (the starter-skills tab).

Runs headless on the offscreen Qt platform, like tests/test_ui.py. Every test
points the dialog at a temp folder — `~/skills` is never created, filled or
read as an install target here.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication, QTabWidget

from pengy.core import skills_install as si
from pengy.core.config import DEFAULTS
from pengy.ui.settings_dialog import SettingsDialog
from pengy.ui.theme import get_theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def make_dialog(qapp, tmp_path):
    """Factory: a SettingsDialog whose skills folder is a temp directory."""
    def build(*, exists=False, system_message=None):
        target = tmp_path / "skills"
        if exists:
            si.install({si.SKILLS_DIR_KEY: str(target)})
        config = dict(DEFAULTS)
        config[si.SKILLS_DIR_KEY] = str(target)
        if system_message is not None:
            config["system_message"] = system_message
        dialog = SettingsDialog(config)
        return dialog, target
    return build


def _install_button(dialog):
    return dialog.install_skills_btn


def _is_lit(dialog, button):
    return bool(button.styleSheet()) and get_theme(dialog._theme)["primary"] in button.styleSheet()


# ── the tab itself ───────────────────────────────────────────────────────────

def test_extras_tab_sits_between_tools_and_about(make_dialog):
    dialog, _ = make_dialog()
    tabs = dialog.findChild(QTabWidget)
    names = [tabs.tabText(i) for i in range(tabs.count())]
    assert names == ["UI", "LLM", "Tools", "Extras", "About"]


def test_missing_folder_lights_up_the_install_button(make_dialog):
    dialog, target = make_dialog()
    assert not target.exists()

    button = _install_button(dialog)
    assert button.isEnabled()
    assert button.text() == "Install Skills"
    assert _is_lit(dialog, button)
    assert "No skills folder" in dialog.skills_status_label.text()
    # nothing to point at yet, so the pointer action stays out of the way
    assert dialog.skills_pointer_btn.isHidden()


def test_installed_folder_dims_the_button_and_names_the_skills(make_dialog):
    dialog, _ = make_dialog(exists=True)

    button = _install_button(dialog)
    assert not button.isEnabled()
    assert button.text() == "Skills Installed"
    assert not _is_lit(dialog, button)
    assert "5 skills" in dialog.skills_status_label.text()
    detail = dialog.skills_detail_label.text()
    for name in ("user_profile", "memory", "pengy_bio", "botskills", "sysinfo"):
        assert name in detail


# ── installing ───────────────────────────────────────────────────────────────

def test_install_writes_the_tree_and_announces_it_in_the_system_message(make_dialog):
    dialog, target = make_dialog()
    before = dialog.system_message_input.toPlainText()
    assert si.INDEX_NAME not in before

    dialog._install_skills()

    assert (target / si.INDEX_NAME).is_file()
    assert (target / "botskills" / "botskills_skill.md").is_file()

    message = dialog.system_message_input.toPlainText()
    assert si.INDEX_NAME in message
    assert str(target / si.INDEX_NAME) in message
    assert message.startswith(before.strip())

    assert not _install_button(dialog).isEnabled()
    assert not _is_lit(dialog, _install_button(dialog))
    assert "Installed" in dialog.skills_detail_label.text()


def test_install_is_idempotent_through_the_dialog(make_dialog):
    dialog, _ = make_dialog()
    dialog._install_skills()
    first = dialog.system_message_input.toPlainText()

    dialog._install_skills()  # a second press cannot duplicate the pointer

    assert dialog.system_message_input.toPlainText() == first


def test_install_respects_an_unchecked_pointer_checkbox(make_dialog):
    dialog, target = make_dialog()
    dialog.skills_pointer_check.setChecked(False)
    before = dialog.system_message_input.toPlainText()

    dialog._install_skills()

    assert (target / si.INDEX_NAME).is_file()
    assert dialog.system_message_input.toPlainText() == before


def test_install_leaves_an_edited_file_alone(make_dialog):
    dialog, target = make_dialog()
    target.mkdir()
    mine = target / si.INDEX_NAME
    mine.write_text("# my index\n")

    dialog._install_skills()

    assert mine.read_text() == "# my index\n"
    assert "untouched" in dialog.skills_detail_label.text()


# ── the follow-on actions ────────────────────────────────────────────────────

def test_profile_interview_button_is_off_until_the_skill_is_installed(make_dialog):
    dialog, _ = make_dialog()
    assert not dialog.skills_profile_btn.isEnabled()
    assert not _is_lit(dialog, dialog.skills_profile_btn)


def test_the_interview_is_the_last_thing_lit_up(make_dialog):
    """One action at a time: install → interview → nothing to press."""
    dialog, target = make_dialog()

    dialog._install_skills()
    # installed, profile still a blank template → the interview is the next step
    assert dialog.skills_profile_btn.isEnabled()
    assert _is_lit(dialog, dialog.skills_profile_btn)
    assert not _is_lit(dialog, dialog.install_skills_btn)
    assert "blank template" in dialog.skills_detail_label.text()

    profile = target / si.PROFILE_SKILL / si.PROFILE_FILE
    profile.write_text(profile.read_text(encoding="utf-8").replace(
        si.PROFILE_TODO, "Answers given."), encoding="utf-8")
    dialog._refresh_skills_state()

    assert dialog.skills_profile_btn.isEnabled()   # still re-runnable
    assert not _is_lit(dialog, dialog.skills_profile_btn)
    assert "blank template" not in dialog.skills_detail_label.text()


def test_profile_button_queues_one_prompt_and_closes(qapp, tmp_path):
    """The button sends a prompt naming the skill file, then accepts the dialog."""
    import pengy.ui.settings_dialog as settings_dialog

    target = tmp_path / "skills"
    si.install({si.SKILLS_DIR_KEY: str(target)})
    config = dict(DEFAULTS)
    config[si.SKILLS_DIR_KEY] = str(target)
    dialog = SettingsDialog(config)

    prompts: list[str] = []
    dialog.prompt_requested.connect(prompts.append)

    assert dialog.skills_profile_btn.isEnabled()
    dialog.skills_profile_btn.click()

    assert len(prompts) == 1
    prompt = prompts[0]
    assert "user_profile/user_profile_skill.md" in prompt
    assert "user_profile/profile.md" in prompt
    assert "one question at a time" in prompt
    # the dialog closes accepted, so the settings are saved and the chat is free
    assert dialog.result() == dialog.DialogCode.Accepted
    assert settings_dialog.SKILL_REGISTRY_URL == "https://skills.catbee.ca"


# ── the pointer-only path (skills already there, message doesn't say so) ─────

def test_existing_skills_without_a_pointer_offer_the_pointer_button(make_dialog):
    dialog, target = make_dialog(exists=True)
    button = dialog.skills_pointer_btn

    assert not button.isHidden()
    assert button.isEnabled()
    assert _is_lit(dialog, button)

    button.click()

    assert f"{target}/{si.INDEX_NAME}" in dialog.system_message_input.toPlainText()
    assert not button.isEnabled()
    assert not _is_lit(dialog, button)

    after = dialog.system_message_input.toPlainText()
    dialog._add_skills_pointer()
    assert dialog.system_message_input.toPlainText() == after


# ── config round-trip ────────────────────────────────────────────────────────
def test_get_config_saves_a_custom_skills_folder(make_dialog, tmp_path):
    dialog, target = make_dialog()
    out = dialog.get_config()
    assert out[si.SKILLS_DIR_KEY] == str(target)


def test_get_config_keeps_the_default_folder_unset(qapp, tmp_path):
    config = dict(DEFAULTS)
    config[si.SKILLS_DIR_KEY] = ""
    config["system_message"] = "You are Pengy."
    dialog = SettingsDialog(config)
    dialog.skills_path_input.setText(si.DEFAULT_SKILLS_DIR)

    assert dialog.get_config()[si.SKILLS_DIR_KEY] == ""


# ── main-window wiring: the prompt is sent, but never while the dialog is up ──

def test_profile_prompt_reaches_the_chat_after_settings_close(qapp, tmp_path, monkeypatch):
    import tempfile

    from pengy.core.config import set_config_dir
    from pengy.ui.main_window import MainWindow
    from pengy.ui import main_window as mw_mod

    with tempfile.TemporaryDirectory(prefix="pengy-extras-") as cfg_dir:
        set_config_dir(cfg_dir)
        monkeypatch.setattr(mw_mod.LLMClient, "chat", lambda *a, **kw: (_ for _ in ()))
        monkeypatch.setattr(mw_mod.LLMClient, "__init__", lambda self, *a, **kw: None)
        window = MainWindow()
        qapp.processEvents()
        try:
            # Never let the dialog look at the real ~/skills on the test host.
            window.config[si.SKILLS_DIR_KEY] = str(tmp_path / "skills")
            sent: list[str] = []
            monkeypatch.setattr(window, "send_message", sent.append)
            monkeypatch.setattr(window, "update_llm_client", lambda *a, **kw: None)

            def fake_exec(dialog_self):
                # What the "Fill In My Profile" button does, then OK.
                dialog_self.prompt_requested.emit("interview me please")
                return dialog_self.DialogCode.Accepted

            monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
            window.open_settings()
            assert sent == ["interview me please"]
        finally:
            window.close()
            qapp.processEvents()
            set_config_dir(None)
