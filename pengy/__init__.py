"""Pengy package."""

try:
    from importlib.metadata import version as _metadata_version
    __version__ = _metadata_version("pengy")
except Exception:
    # Fallback when running from source / not installed
    __version__ = "1.3.13"
