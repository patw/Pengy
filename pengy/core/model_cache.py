"""Persistent cache of the fetched model list.

Stores the last successful ``/models`` result in ``models_cache.json``
inside the config directory, keyed by the endpoint's base URL.  The
cache is allowed to go stale — its purpose is to keep the model
dropdown populated between fetches; a stale list is fine, an empty one
is not.  Both the desktop and web frontends share this file.
"""
from __future__ import annotations

import time

from pengy.core.config import _atomic_write, _config_path, _safe_json_load

CACHE_FILE = "models_cache.json"
MAX_MODELS = 500  # defensive cap for endpoints advertising huge model lists


def _normalize(url: str) -> str:
    """Normalize a base URL for comparison: trim, strip trailing /, lowercase."""
    return (url or "").strip().rstrip("/").lower()


def load_model_cache() -> dict | None:
    """Load the cache, returning ``{"url", "fetched_at", "models"}`` or None.

    Missing or corrupt files (handled by :func:`_safe_json_load`) yield None.
    """
    data = _safe_json_load(_config_path(CACHE_FILE))
    if not isinstance(data, dict):
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    models = sorted(m for m in models if isinstance(m, str) and m)[:MAX_MODELS]
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        fetched_at = None
    return {
        "url": data.get("url", "") if isinstance(data.get("url"), str) else "",
        "fetched_at": fetched_at,
        "models": models,
    }


def save_model_cache(base_url: str, models: list[str]) -> None:
    """Persist *models* as the cached list for *base_url* (atomic write)."""
    _atomic_write(_config_path(CACHE_FILE), {
        "url": (base_url or "").strip().rstrip("/"),
        "fetched_at": int(time.time()),
        "models": sorted(m for m in models if m)[:MAX_MODELS],
    })


def cached_models_for(base_url: str) -> list[str]:
    """Return the cached model list for *base_url*, or [] on no match.

    A cached list for a *different* endpoint is never returned — offering
    the wrong endpoint's models would be worse than an empty dropdown.
    """
    cache = load_model_cache()
    if cache and _normalize(cache["url"]) == _normalize(base_url):
        return cache["models"]
    return []
