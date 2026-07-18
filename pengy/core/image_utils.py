"""Image preprocessing for LLM vision APIs.

Provider limits (as of mid-2026):
  - Anthropic: 5 MB per image, 8000 px max dimension
  - OpenAI:   20 MB per image, auto-resizes (no hard pixel limit)
  - Gemini:   20 MB total request, no hard pixel limit

Safe defaults: 4096 px max, 4.5 MB, JPEG quality 85.
"""

import io
import mimetypes
from pathlib import Path

from PIL import Image

# Conservative defaults that work across all major providers.
DEFAULT_MAX_DIMENSION = 4096  # px — well under Anthropic's 8000
DEFAULT_MAX_MB = 4.5          # MB — under Anthropic's 5 MB
DEFAULT_QUALITY = 85          # JPEG quality (0–100)

# MIME types that don't benefit from re-encoding to JPEG.
_LOSSY_MIMES = {"image/jpeg", "image/webp"}


def preprocess(
    path: Path,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_mb: float = DEFAULT_MAX_MB,
    quality: int = DEFAULT_QUALITY,
) -> tuple[bytes, str]:
    """Load and preprocess an image for sending to a vision LLM.

    Returns ``(bytes, mime_type)`` ready for base64-encoding.

    Steps:
      1. If any dimension > *max_dimension*, resize proportionally.
      2. If PNG/GIF/BMP and no alpha channel, convert to JPEG.
      3. If still over *max_mb*, lower quality / dimensions further.
    """
    img = Image.open(path)
    original_format = (img.format or "").upper()
    w, h = img.size
    max_bytes = int(max_mb * 1024 * 1024)

    # ── Step 1: dimension cap ────────────────────────────────
    if w > max_dimension or h > max_dimension:
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

    # ── Step 2: format conversion (lossless → JPEG when possible) ─
    mime = _guess_mime(path)
    should_convert = (
        mime not in _LOSSY_MIMES
        and original_format in ("PNG", "GIF", "BMP", "TIFF")
    )
    if should_convert:
        if img.mode == "RGBA":
            # Flatten transparency onto white
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # Encode as JPEG
        buf = _encode_jpeg(img, quality)
        if len(buf) <= max_bytes:
            return buf, "image/jpeg"
        # Still too big — let Step 3 handle it

    # ── Step 3: size cap (try lower quality, then smaller dimensions) ─
    buf = _encode(img, mime, quality)
    if len(buf) <= max_bytes:
        return buf, mime

    # Try reducing JPEG quality in steps
    if mime not in _LOSSY_MIMES or mime == "image/jpeg":
        for q in (75, 60, 45, 30):
            buf = _encode_jpeg(_to_rgb(img), q)
            if len(buf) <= max_bytes:
                return buf, "image/jpeg"

    # Last resort: shrink dimensions
    for scale in (0.75, 0.5, 0.33):
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)
        small = img.resize((new_w, new_h), Image.LANCZOS)
        buf = _encode_jpeg(_to_rgb(small), 60)
        if len(buf) <= max_bytes:
            return buf, "image/jpeg"

    # Absolute last resort — tiny thumbnail
    tiny = img.copy()
    tiny.thumbnail((512, 512), Image.LANCZOS)
    return _encode_jpeg(_to_rgb(tiny), 40), "image/jpeg"


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "image/jpeg"


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _encode(img: Image.Image, mime: str, quality: int) -> bytes:
    fmt = _mime_to_format(mime)
    buf = io.BytesIO()
    save_kwargs = {"format": fmt, "optimize": True}
    if fmt == "JPEG":
        save_kwargs["quality"] = quality
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _mime_to_format(mime: str) -> str:
    return {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/gif": "GIF",
        "image/webp": "WEBP",
        "image/bmp": "BMP",
    }.get(mime, "JPEG")
