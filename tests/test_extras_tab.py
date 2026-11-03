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

from PySide6.QtWidgets import QApplication, QTabWidget, QPushButton, QDialogButtonBox

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


# ── setup-only: no Settings-to-chat path ──────────────────────────────────────
@pytest.mark.parametrize("exists", [False, True])
def test_extras_has_only_setup_and_os_navigation_actions(make_dialog, exists):
    dialog, _ = make_dialog(exists=exists)
    extras = dialog.findChild(QTabWidget).widget(3)
    labels = {b.text() for b in extras.findChildren(QPushButton)}
    assert labels == {"Browse…", "Skills Installed" if exists else "Install Skills",
                      "Add to System Message", "Open Folder", "Browse BotSkills"}
    assert not hasattr(dialog, "prompt_requested")
    assert not hasattr(dialog, "_start_profile_interview")
    assert "interview" not in dialog.skills_detail_label.text().lower()


def test_install_leaves_settings_open_and_profile_template_untouched(make_dialog):
    dialog, target = make_dialog()
    dialog.install_skills_btn.click()
    assert dialog.result() == dialog.DialogCode.Rejected  # never auto-accepted
    assert (target / si.INDEX_NAME).is_file()
    assert "<!-- TODO -->" in (target / "user_profile/profile.md").read_text()
    assert dialog.skills_open_btn.isEnabled()
    assert "interview" not in dialog.skills_detail_label.text().lower()


def test_partial_folder_is_repaired_locally(make_dialog):
    dialog, target = make_dialog(exists=True)
    (target / si.INDEX_NAME).unlink()
    dialog._refresh_skills_state()
    assert dialog.install_skills_btn.isEnabled()
    assert "no skill_index.md" in dialog.skills_status_label.text()
    dialog.install_skills_btn.click()
    assert (target / si.INDEX_NAME).is_file()
    assert dialog.result() == dialog.DialogCode.Rejected


def test_folder_and_registry_buttons_delegate_to_os(make_dialog, monkeypatch):
    from pengy.ui import settings_dialog as mod
    dialog, target = make_dialog(exists=True)
    opened = []
    monkeypatch.setattr(mod.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    dialog.skills_open_btn.click()
    dialog.skills_registry_btn.click()
    assert opened[0].isLocalFile()
    assert opened[0].toLocalFile() == str(target)
    assert opened[1].toString() == "https://skills.catbee.ca"
    assert dialog.result() == dialog.DialogCode.Rejected


def test_accepting_extras_saves_settings_without_sending_chat(qapp, tmp_path, monkeypatch):
    from pengy.core.config import set_config_dir, load_config
    from pengy.ui.main_window import MainWindow

    set_config_dir(str(tmp_path / "config"))
    window = MainWindow()
    target = tmp_path / "skills"
    window.config[si.SKILLS_DIR_KEY] = str(target)
    sent = []
    monkeypatch.setattr(window, "send_message", sent.append)
    monkeypatch.setattr(window, "update_llm_client", lambda: None)

    def install_and_accept(dialog):
        dialog.install_skills_btn.click()
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()
        return dialog.result()

    monkeypatch.setattr(SettingsDialog, "exec", install_and_accept)
    try:
        window.open_settings()
        assert sent == []
        saved = load_config()
        assert saved[si.SKILLS_DIR_KEY] == str(target)
        assert str(target / si.INDEX_NAME) in saved["system_message"]
    finally:
        window.close()
        qapp.processEvents()
        set_config_dir(None)
