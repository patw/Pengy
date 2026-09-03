"""Durable, content-addressed attachment storage (attachment schema v1).

Chat JSON stores small references; immutable source bytes and generated image
representations live below ``<config>/attachments``.  This module deliberately
knows only the image handler in v1 while retaining a generic attachment shape.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from pengy.core.config import get_config_dir
from pengy.core.image_utils import preprocess

MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 40_000_000
_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")


def attachment_root() -> Path:
    return get_config_dir() / "attachments"


def _digest_for_id(attachment_id: str) -> str:
    match = _ID_RE.fullmatch(attachment_id or "")
    if not match:
        raise ValueError("invalid attachment id")
    return match.group(1)


def object_path(attachment_id: str) -> Path:
    digest = _digest_for_id(attachment_id)
    return attachment_root() / "objects" / "sha256" / digest[:2] / digest


def derivative_path(attachment_id: str, name: str) -> Path:
    if name not in {"image-display-v1.jpg", "thumbnail-256-v1.jpg"}:
        raise ValueError("invalid derivative name")
    digest = _digest_for_id(attachment_id)
    return attachment_root() / "derivatives" / "sha256" / digest[:2] / digest / name


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _safe_name(name: str) -> str:
    clean = " ".join((name or "Image").replace("\x00", "").split())
    return clean[:240] or "Image"


def _mime_for_format(fmt: str | None) -> str:
    return {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp", "BMP": "image/bmp", "TIFF": "image/tiff"}.get((fmt or "").upper(), "application/octet-stream")


def import_image_bytes(data: bytes, name: str, *, max_dimension: int = 4096,
                       max_mb: float = 4.5, quality: int = 85) -> dict[str, Any]:
    """Validate/import source bytes and install bounded display derivatives."""
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ValueError("image exceeds the 25 MB attachment source limit")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PIXELS:
                raise ValueError("image exceeds decoded pixel limit")
            width, height, media_type = image.width, image.height, _mime_for_format(image.format)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("attachment is not a valid image") from exc

    digest = hashlib.sha256(data).hexdigest()
    attachment_id = f"sha256:{digest}"
    source_path = object_path(attachment_id)
    if not source_path.exists():
        _atomic_write(source_path, data)
    ensure_image_derivatives(attachment_id, max_dimension=max_dimension, max_mb=max_mb, quality=quality)
    return {
        "v": 1, "id": attachment_id, "kind": "image", "name": _safe_name(name),
        "media_type": media_type, "byte_size": len(data),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "image": {"width": width, "height": height},
    }


def import_image(path: Path, name: str | None = None, **limits: Any) -> dict[str, Any]:
    with open(path, "rb") as stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)
    return import_image_bytes(data, name or path.name, **limits)


def ensure_image_derivatives(attachment_id: str, *, max_dimension: int = 4096,
                             max_mb: float = 4.5, quality: int = 85) -> None:
    source = object_path(attachment_id)
    if not source.is_file():
        raise FileNotFoundError("attachment object is unavailable")
    display = derivative_path(attachment_id, "image-display-v1.jpg")
    thumbnail = derivative_path(attachment_id, "thumbnail-256-v1.jpg")
    if display.exists() and thumbnail.exists():
        return
    # The display recipe is always sanitized RGB JPEG. Do it here rather than
    # relying on preprocess(), whose source-format preservation is intended for
    # provider transport and may retain alpha-bearing PNG/WebP images.
    with Image.open(source) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        normalized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        limit = int(max_mb * 1024 * 1024)
        display_buffer = io.BytesIO()
        for jpeg_quality in (quality, 75, 60, 45, 30):
            display_buffer = io.BytesIO()
            normalized.save(display_buffer, format="JPEG", quality=jpeg_quality, optimize=True)
            if len(display_buffer.getvalue()) <= limit:
                break
        thumb = normalized.copy()
        thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
        thumb_buffer = io.BytesIO()
        thumb.save(thumb_buffer, format="JPEG", quality=82, optimize=True)
    if not display.exists(): _atomic_write(display, display_buffer.getvalue())
    if not thumbnail.exists(): _atomic_write(thumbnail, thumb_buffer.getvalue())


def load_object(attachment_id: str, *, verify_hash: bool = False) -> bytes:
    data = object_path(attachment_id).read_bytes()
    if verify_hash and hashlib.sha256(data).hexdigest() != _digest_for_id(attachment_id):
        raise ValueError("attachment object hash does not match its id")
    return data


def attachment_exists(ref: dict[str, Any]) -> bool:
    try: return object_path(str(ref.get("id", ""))).is_file()
    except ValueError: return False


def scan_references(chats: list[dict[str, Any]]) -> set[str]:
    """Collect valid attachment IDs referenced by chat dictionaries."""
    refs: set[str] = set()
    for chat in chats:
        for message in chat.get("messages", []) if isinstance(chat, dict) else []:
            for ref in message.get("attachments", []) if isinstance(message, dict) else []:
                if isinstance(ref, dict):
                    try:
                        _digest_for_id(str(ref.get("id", "")))
                        refs.add(str(ref["id"]))
                    except (ValueError, KeyError):
                        continue
    return refs


def storage_report(chats: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return usage and reclaimable-object counts without deleting anything."""
    root = attachment_root()
    objects = []
    object_dir = root / "objects" / "sha256"
    if object_dir.is_dir():
        objects = [p for p in object_dir.glob("??/*") if p.is_file() and _ID_RE.fullmatch("sha256:" + p.name)]
    referenced = scan_references(chats or [])
    object_bytes = sum(p.stat().st_size for p in objects)
    reclaimable = [p for p in objects if "sha256:" + p.name not in referenced]
    derivatives = [p for p in (root / "derivatives" / "sha256").glob("??/*/*") if p.is_file()] if (root / "derivatives" / "sha256").is_dir() else []
    return {"objects": len(objects), "object_bytes": object_bytes, "derivatives": len(derivatives), "referenced": len(referenced), "reclaimable_objects": len(reclaimable), "reclaimable_bytes": sum(p.stat().st_size for p in reclaimable), "delete_performed": False}


def image_data_url(ref: dict[str, Any], **limits: Any) -> str | None:
    """Resolve a valid image ref to an ephemeral OpenAI-compatible data URI."""
    try:
        if ref.get("kind") != "image": return None
        ensure_image_derivatives(str(ref.get("id")), **limits)
        data = derivative_path(str(ref["id"]), "image-display-v1.jpg").read_bytes()
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except (OSError, ValueError, KeyError):
        return None


def attachment_label(ref: dict[str, Any]) -> str:
    if ref.get("kind") == "image":
        meta = ref.get("image") if isinstance(ref.get("image"), dict) else {}
        dimensions = f" · {meta.get('width')}×{meta.get('height')}" if meta.get("width") and meta.get("height") else ""
        return f"[image: {ref.get('name', 'Image')}{dimensions}]"
    return f"[attachment: {ref.get('name', ref.get('kind', 'unknown'))}]"


def message_to_api(message: dict[str, Any], *, include_attachments: bool = True,
                   max_dimension: int = 4096, max_mb: float = 4.5, quality: int = 85) -> dict[str, Any]:
    """Copy a persisted message and derive provider-only image_url parts."""
    result = dict(message)
    refs = result.pop("attachments", None) or []
    if message.get("role") != "user" or not include_attachments:
        return result
    if not isinstance(refs, list) or not refs:
        return result
    parts: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict): continue
        uri = image_data_url(ref, max_dimension=max_dimension, max_mb=max_mb, quality=quality)
        if uri:
            parts.append({"type": "image_url", "image_url": {"url": uri}})
    text = message.get("content")
    if isinstance(text, str) and text:
        parts.append({"type": "text", "text": text})
    if parts:
        result["content"] = parts
    result.pop("attachments", None)
    return result


def resolve_history(messages: list[dict[str, Any]], *, attachment_keep_turns: int = 4,
                    max_dimension: int = 4096, max_mb: float = 4.5, quality: int = 85) -> list[dict[str, Any]]:
    users = [i for i, msg in enumerate(messages) if msg.get("role") == "user"]
    keep = set(users[-attachment_keep_turns:]) if attachment_keep_turns > 0 else set(users)
    return [message_to_api(msg, include_attachments=i in keep,
                           max_dimension=max_dimension, max_mb=max_mb, quality=quality)
            for i, msg in enumerate(messages)]
