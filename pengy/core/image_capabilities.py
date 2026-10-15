"""Persistent, endpoint/model-scoped image-input rejections.

Each route owns an atomic file so concurrent tabs/processes cannot overwrite
another route's entry. Originals are never modified. Delete the route's file
(or the image_rejections directory) to retry after provider capabilities change.
The schema and SHA-256 key match the Rust and C++ editions.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from pengy.core.config import get_config_dir, _atomic_write

_lock = threading.RLock()
_observed: set[tuple[str, str, str]] = set()


def _route(base_url: str, model: str):
    endpoint = base_url.strip().rstrip("/")
    identity = [endpoint, model]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = get_config_dir() / "image_rejections" / (hashlib.sha256(encoded).hexdigest() + ".json")
    return path, endpoint, (str(path.parent), endpoint, model)


def images_rejected(base_url: str, model: str) -> bool:
    path, endpoint, key = _route(base_url, model)
    with _lock:
        # Disk is consulted every time; missing/invalid data never infers a
        # capability. Memory only backs observations whose save failed.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return (isinstance(data, dict) and data.get("v") == 1
                    and data.get("endpoint") == endpoint and data.get("model") == model)
        except (OSError, ValueError):
            return key in _observed


def remember_image_rejection(base_url: str, model: str) -> None:
    path, endpoint, key = _route(base_url, model)
    with _lock:
        try:
            _atomic_write(path, {"v": 1, "endpoint": endpoint, "model": model,
                                 "rejected_at": int(time.time())})
            _observed.discard(key)
        except OSError:
            # A read-only config must not prevent the current process from
            # learning the route's limitation, nor fail a recoverable turn.
            _observed.add(key)
