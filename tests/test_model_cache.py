"""Tests for the persistent model-list cache.

Covers the core cache module (save/load, URL-keying, corruption, cap),
the web ``/models`` route (cache serve, refresh, stale fallback), and
the GUI settings dialog (pre-population, cache update on fetch).

Run with:  python -m pytest tests/test_model_cache.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from pengy.core import model_cache
from pengy.core.model_cache import (
    cached_models_for,
    load_model_cache,
    save_model_cache,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def cfg_dir():
    """Redirect the pengy config dir to a throwaway directory."""
    from pengy.core.config import set_config_dir, get_config_dir

    with tempfile.TemporaryDirectory(prefix="pengy-modelcache-") as d:
        set_config_dir(d)
        assert get_config_dir() != Path.home() / ".config" / "pengy"
        yield Path(d)
    set_config_dir(None)


def _cache_file(cfg_dir: Path) -> Path:
    return cfg_dir / "models_cache.json"


# ────────────────────────────────────────────────────────────────────
# Core module
# ────────────────────────────────────────────────────────────────────

class TestSaveLoad:
    def test_roundtrip(self, cfg_dir):
        save_model_cache("https://api.openai.com/v1", ["gpt-4o", "gpt-4o-mini"])
        cache = load_model_cache()
        assert cache is not None
        assert cache["url"] == "https://api.openai.com/v1"
        assert cache["models"] == ["gpt-4o", "gpt-4o-mini"]
        assert isinstance(cache["fetched_at"], int)
        assert cache["fetched_at"] > int(time.time()) - 60

    def test_writes_atomic_file(self, cfg_dir):
        save_model_cache("http://localhost:8080/v1", ["a"])
        raw = json.loads(_cache_file(cfg_dir).read_text())
        assert raw["models"] == ["a"]
        assert raw["fetched_at"]

    def test_missing_file_returns_none(self, cfg_dir):
        assert load_model_cache() is None

    def test_corrupt_file_returns_none_and_is_quarantined(self, cfg_dir):
        _cache_file(cfg_dir).write_text("{not json")
        assert load_model_cache() is None
        # _safe_json_load renames corrupt files rather than deleting them
        assert not _cache_file(cfg_dir).exists()
        assert list(cfg_dir.glob("models_cache.json.corrupt-*"))

    def test_garbage_shape_returns_none(self, cfg_dir):
        _cache_file(cfg_dir).write_text(json.dumps({"models": "nope"}))
        assert load_model_cache() is None
        _cache_file(cfg_dir).write_text(json.dumps([1, 2, 3]))
        assert load_model_cache() is None

    def test_models_sorted_and_nonstrings_dropped(self, cfg_dir):
        save_model_cache("http://x", ["z", "a", "", None, "b"])
        assert load_model_cache()["models"] == ["a", "b", "z"]

    def test_overlong_list_is_capped(self, cfg_dir):
        models = [f"m{i:04d}" for i in range(1000)]
        save_model_cache("http://x", models)
        assert len(load_model_cache()["models"]) == model_cache.MAX_MODELS


class TestUrlKeying:
    def test_matching_url_returns_list(self, cfg_dir):
        save_model_cache("https://api.openai.com/v1", ["gpt-4o"])
        assert cached_models_for("https://api.openai.com/v1") == ["gpt-4o"]

    def test_trailing_slash_and_case_are_ignored(self, cfg_dir):
        save_model_cache("https://api.openai.com/v1/", ["gpt-4o"])
        assert cached_models_for("HTTPS://api.openai.com/v1") == ["gpt-4o"]
        assert cached_models_for("https://api.openai.com/v1//") == ["gpt-4o"]

    def test_different_url_returns_empty(self, cfg_dir):
        save_model_cache("https://api.openai.com/v1", ["gpt-4o"])
        assert cached_models_for("http://localhost:8080/v1") == []

    def test_no_cache_returns_empty(self, cfg_dir):
        assert cached_models_for("https://api.openai.com/v1") == []


# ────────────────────────────────────────────────────────────────────
# Web /models route
# ────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client(cfg_dir):
    from pengy.web.app import app

    app.config["SERVER_NAME"] = "localhost"
    app.config["APPLICATION_ROOT"] = "/"
    app.config["PREFERRED_URL_SCHEME"] = "http"
    return app.test_client()


def _set_endpoint(cfg_dir, base_url):
    from pengy.core.config import load_config, save_config

    config = load_config()
    config["base_url"] = base_url
    config["api_key"] = "test-key"
    save_config(config)


class TestModelsRoute:
    def test_serves_cache_without_network(self, client, cfg_dir):
        """A matching cache is returned with cached=true and no HTTP call."""
        _set_endpoint(cfg_dir, "https://api.openai.com/v1")
        save_model_cache("https://api.openai.com/v1", ["gpt-4o", "gpt-4o-mini"])

        def no_network(*a, **kw):
            raise AssertionError("urlopen must not be called when cache matches")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(urllib.request, "urlopen", no_network)
            resp = client.get("/models")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["models"] == ["gpt-4o", "gpt-4o-mini"]
        assert data["cached"] is True
        assert data["fetched_at"]

    def test_serves_live_fetch_and_updates_cache(self, client, cfg_dir, monkeypatch):
        """Without a cache, the endpoint is hit and the result persisted."""
        _set_endpoint(cfg_dir, "https://api.openai.com/v1")
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return _FakeResponse({"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]})

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        resp = client.get("/models")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["models"] == ["gpt-5", "gpt-5-mini"]
        assert data["cached"] is False
        assert calls == ["https://api.openai.com/v1/models"]
        # Persisted — a subsequent request serves it without network
        assert load_model_cache()["models"] == ["gpt-5", "gpt-5-mini"]
        resp2 = client.get("/models")
        assert resp2.get_json()["cached"] is True
        assert len(calls) == 1

    def test_refresh_bypasses_cache(self, client, cfg_dir, monkeypatch):
        _set_endpoint(cfg_dir, "https://api.openai.com/v1")
        save_model_cache("https://api.openai.com/v1", ["old-model"])

        def fake_urlopen(req, timeout=None):
            return _FakeResponse({"data": [{"id": "new-model"}]})

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        resp = client.get("/models?refresh=1")
        data = resp.get_json()
        assert data["models"] == ["new-model"]
        assert data["cached"] is False
        assert load_model_cache()["models"] == ["new-model"]

    def test_failed_refresh_falls_back_to_stale_cache(self, client, cfg_dir, monkeypatch):
        """A failed live fetch returns the stale list plus an error note."""
        _set_endpoint(cfg_dir, "https://api.openai.com/v1")
        save_model_cache("https://api.openai.com/v1", ["stale-model"])

        def fail(*a, **kw):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        resp = client.get("/models?refresh=1")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["models"] == ["stale-model"]
        assert data["stale"] is True
        assert data["error"]

    def test_failed_fetch_no_cache_is_502(self, client, cfg_dir, monkeypatch):
        _set_endpoint(cfg_dir, "https://api.openai.com/v1")

        def fail(*a, **kw):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        resp = client.get("/models")
        assert resp.status_code == 502
        assert "error" in resp.get_json()

    def test_mismatched_cache_triggers_live_fetch(self, client, cfg_dir, monkeypatch):
        """Cache from a different endpoint is ignored, not served."""
        _set_endpoint(cfg_dir, "https://new.host/v1")
        save_model_cache("https://old.host/v1", ["wrong-endpoint-model"])

        def fake_urlopen(req, timeout=None):
            assert req.full_url == "https://new.host/v1/models"
            return _FakeResponse({"data": [{"id": "right-model"}]})

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        data = client.get("/models").get_json()
        assert data["models"] == ["right-model"]


# ────────────────────────────────────────────────────────────────────
# GUI settings dialog
# ────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────
# GUI settings dialog
# ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the GUI tests; skips if PySide6 is absent."""
    pytest.importorskip("PySide6", reason="PySide6 not installed")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


class TestSettingsDialogCache:
    def _wait_fetch_done(self, qapp, dialog, timeout=5.0):
        """Spin the event loop until the background fetch finishes."""
        deadline = time.time() + timeout
        while not dialog.fetch_models_btn.isEnabled() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()
        assert dialog.fetch_models_btn.isEnabled(), "fetch did not finish"

    def test_dialog_prepopulates_from_cache(self, qapp, cfg_dir):
        from pengy.core.config import load_config
        from pengy.ui.settings_dialog import SettingsDialog

        _set_endpoint(cfg_dir, "https://api.openai.com/v1")
        save_model_cache("https://api.openai.com/v1", ["gpt-4o", "gpt-4o-mini"])

        config = load_config()  # model defaults to gpt-4o
        dialog = SettingsDialog(config)
        try:
            items = [dialog.model_combo.itemText(i) for i in range(dialog.model_combo.count())]
            assert items == ["gpt-4o", "gpt-4o-mini"]
            assert dialog.model_combo.currentText() == "gpt-4o"
            assert "last fetched" in dialog.model_cache_note.text()
        finally:
            dialog.deleteLater()

    def test_dialog_without_cache_starts_single_entry(self, qapp, cfg_dir):
        from pengy.core.config import load_config
        from pengy.ui.settings_dialog import SettingsDialog

        _set_endpoint(cfg_dir, "https://api.openai.com/v1")
        dialog = SettingsDialog(load_config())
        try:
            assert [dialog.model_combo.itemText(i) for i in range(dialog.model_combo.count())] == ["gpt-4o"]
            assert dialog.model_cache_note.text() == ""
        finally:
            dialog.deleteLater()

    def test_fetch_persists_cache(self, qapp, cfg_dir, monkeypatch):
        from pengy.core.config import load_config
        from pengy.ui.settings_dialog import SettingsDialog

        _set_endpoint(cfg_dir, "https://api.openai.com/v1")

        def fake_urlopen(req, timeout=None):
            return _FakeResponse({"data": [{"id": "llama-3"}, {"id": "llama-3-70b"}]})

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        dialog = SettingsDialog(load_config())
        try:
            assert load_model_cache() is None
            dialog._fetch_models()
            self._wait_fetch_done(qapp, dialog)

            items = [dialog.model_combo.itemText(i) for i in range(dialog.model_combo.count())]
            assert items == ["llama-3", "llama-3-70b"]
            cache = load_model_cache()
            assert cache is not None
            assert cache["models"] == ["llama-3", "llama-3-70b"]
            assert cache["url"] == "https://api.openai.com/v1"
            assert "last fetched" in dialog.model_cache_note.text()

            # A fresh dialog opened on the same config is pre-populated
            dialog2 = SettingsDialog(load_config())
            try:
                items2 = [dialog2.model_combo.itemText(i) for i in range(dialog2.model_combo.count())]
                assert "llama-3-70b" in items2
            finally:
                dialog2.deleteLater()
        finally:
            dialog.deleteLater()
        qapp.processEvents()
