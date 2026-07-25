"""Tests for web UI theming (Bootstrap 5.3 ``data-bs-theme``).

The web frontend honours the same ``theme_mode`` setting as the desktop GUI.
Explicit light/dark is rendered server-side so it survives JS being disabled;
``system`` is resolved client-side by an inline head script.

These tests also guard against colour values creeping back into the templates
as hardcoded hex, which is what broke dark mode in the first place.

Run with:  python -m pytest tests/test_web_theme.py -v
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from pengy.web.app import app, _pygments_css, _theme_mode
from pengy.core.config import set_config_dir, get_config_dir, load_config, save_config
from pengy.core.chat_manager import create_chat


TEMPLATES = Path(__file__).resolve().parent.parent / "pengy" / "web" / "templates"


@pytest.fixture
def tmp_cfg():
    with tempfile.TemporaryDirectory(prefix="pengy-theme-") as cfg_dir:
        set_config_dir(cfg_dir)
        assert get_config_dir() != Path.home() / ".config" / "pengy"
        yield Path(cfg_dir)
    set_config_dir(None)


@pytest.fixture
def client(tmp_cfg):
    return app.test_client()


def set_mode(mode: str) -> None:
    config = load_config()
    config["theme_mode"] = mode
    save_config(config)


def html_tag(html: str) -> str:
    match = re.search(r"<html[^>]*>", html)
    assert match, "no <html> tag in response"
    return match.group(0)


# ────────────────────────────────────────────────────────────────────
# _theme_mode()
# ────────────────────────────────────────────────────────────────────

class TestThemeModeHelper:
    @pytest.mark.parametrize("mode", ["system", "light", "dark"])
    def test_valid_modes_pass_through(self, mode):
        assert _theme_mode({"theme_mode": mode}) == mode

    def test_missing_defaults_to_system(self):
        assert _theme_mode({}) == "system"

    @pytest.mark.parametrize("bogus", ["", "solarized", "DARK", None, 0])
    def test_invalid_falls_back_to_system(self, bogus):
        assert _theme_mode({"theme_mode": bogus}) == "system"


# ────────────────────────────────────────────────────────────────────
# Server-rendered theme attribute
# ────────────────────────────────────────────────────────────────────

class TestThemeAttribute:
    def test_dark_mode_renders_dark_attribute(self, client):
        set_mode("dark")
        chat = create_chat()
        tag = html_tag(client.get(f"/chat/{chat['id']}").data.decode())
        assert 'data-bs-theme="dark"' in tag

    def test_light_mode_renders_light_attribute(self, client):
        set_mode("light")
        chat = create_chat()
        tag = html_tag(client.get(f"/chat/{chat['id']}").data.decode())
        assert 'data-bs-theme="light"' in tag

    def test_system_mode_renders_light_as_the_no_js_fallback(self, client):
        """With JS off there's no way to read the OS preference — default light."""
        set_mode("system")
        chat = create_chat()
        tag = html_tag(client.get(f"/chat/{chat['id']}").data.decode())
        assert 'data-bs-theme="light"' in tag

    @pytest.mark.parametrize("mode", ["system", "light", "dark"])
    def test_settings_page_is_themed_too(self, client, mode):
        set_mode(mode)
        tag = html_tag(client.get("/settings").data.decode())
        expected = "dark" if mode == "dark" else "light"
        assert f'data-bs-theme="{expected}"' in tag

    @pytest.mark.parametrize("mode", ["system", "light", "dark"])
    def test_inline_script_receives_the_configured_mode(self, client, mode):
        """The client-side resolver needs the raw mode, not the fallback."""
        set_mode(mode)
        chat = create_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert f'var mode = "{mode}";' in html

    def test_resolver_script_runs_before_the_stylesheet(self, client):
        """Setting the attribute after CSS loads would flash the wrong theme."""
        set_mode("system")
        chat = create_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert html.index("var mode =") < html.index("bootstrap@5.3.3/dist/css")


# ────────────────────────────────────────────────────────────────────
# Settings form round-trip
# ────────────────────────────────────────────────────────────────────

class TestThemeSetting:
    @pytest.mark.parametrize("mode", ["system", "light", "dark"])
    def test_form_persists_mode(self, client, mode):
        client.post("/settings", data={"theme_mode": mode, "tool_confirmation": "none"})
        assert load_config()["theme_mode"] == mode

    def test_selected_option_reflects_current_mode(self, client):
        set_mode("dark")
        html = client.get("/settings").data.decode()
        assert 'value="dark" selected' in html

    def test_form_exposes_all_three_options(self, client):
        html = client.get("/settings").data.decode()
        for mode in ("system", "light", "dark"):
            assert f'value="{mode}"' in html

    def test_invalid_mode_is_rejected_not_persisted(self, client):
        set_mode("dark")
        client.post("/settings", data={"theme_mode": "hot-pink", "tool_confirmation": "none"})
        assert load_config()["theme_mode"] == "dark"

    def test_omitting_the_field_does_not_wipe_the_setting(self, client):
        """A POST without theme_mode falls back to the schema default."""
        client.post("/settings", data={"tool_confirmation": "none"})
        assert load_config()["theme_mode"] in ("system", "light", "dark")


# ────────────────────────────────────────────────────────────────────
# Pygments: both themes shipped, scoped
# ────────────────────────────────────────────────────────────────────

class TestPygmentsDualTheme:
    def test_emits_both_scoped_rule_sets(self):
        css = _pygments_css()
        assert '[data-bs-theme="light"] .highlight' in css
        assert '[data-bs-theme="dark"] .highlight' in css

    def test_every_rule_is_theme_scoped(self):
        """An unscoped .highlight rule would leak one theme into the other."""
        css = _pygments_css()
        unscoped = [
            line for line in css.splitlines()
            if ".highlight" in line and "data-bs-theme" not in line
        ]
        assert not unscoped, f"unscoped highlight rules: {unscoped[:3]}"

    def test_the_two_themes_differ(self):
        css = _pygments_css()
        light, dark = css.split('[data-bs-theme="dark"]', 1)
        assert light.strip() and dark.strip()
        assert light != dark

    def test_served_in_the_chat_page(self, client):
        chat = create_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert '[data-bs-theme="dark"] .highlight' in html

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_both_rule_sets_ship_regardless_of_mode(self, client, mode):
        """Both must always be present so the OS can flip theme without a reload."""
        set_mode(mode)
        chat = create_chat()
        html = client.get(f"/chat/{chat['id']}").data.decode()
        assert '[data-bs-theme="light"] .highlight' in html
        assert '[data-bs-theme="dark"] .highlight' in html


# ────────────────────────────────────────────────────────────────────
# No hardcoded light-only styling
# ────────────────────────────────────────────────────────────────────

class TestNoHardcodedLightStyling:
    @staticmethod
    def _template(name: str) -> str:
        return (TEMPLATES / name).read_text()

    def test_code_surfaces_use_theme_tokens(self):
        base = self._template("base.html")
        for selector in (".tool-args, .tool-output", ".markdown-body pre"):
            block = base[base.index(selector):]
            block = block[:block.index("}")]
            assert "var(--pengy-code-bg)" in block, f"{selector} lost its theme token"

    def test_both_token_sets_are_defined(self):
        base = self._template("base.html")
        for token in ("--pengy-code-bg", "--pengy-code-fg", "--pengy-inline-code-fg"):
            # once for light, once for the dark override
            assert base.count(token) >= 3, f"{token} not defined for both themes"
        assert '[data-bs-theme="dark"]' in base

    def test_no_bootstrap_light_only_classes(self):
        """bg-light / navbar-light / text-dark don't respond to data-bs-theme."""
        for name in ("base.html", "chat.html", "settings.html"):
            body = self._template(name)
            for cls in ("navbar-light", "bg-light", "text-dark"):
                assert cls not in body, f"{name} still uses light-only class {cls!r}"

    def test_no_stray_hex_colours_outside_the_token_block(self):
        """Colours belong in the :root token block, not scattered through the CSS."""
        base = self._template("base.html")
        tokens_end = base.index("/* ── Layout")
        body = base[tokens_end:]
        strays = re.findall(r"#[0-9a-fA-F]{6}\b", body)
        # The tool-card accent is a deliberate theme-agnostic highlight colour.
        strays = [h for h in strays if h.lower() != "#f9e2af"]
        assert not strays, f"hardcoded colours outside token block: {strays}"
