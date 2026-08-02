"""Configuration management for Pengy."""
import getpass
import json
import os
import platform
import shutil
import socket
import tempfile
from datetime import date
from pathlib import Path

# Global override for config directory (set via --config-dir)
_config_dir_override: str | None = None

def set_config_dir(path: str) -> None:
    """Override the config directory (e.g. from --config-dir flag)."""
    global _config_dir_override
    _config_dir_override = path

def get_config_dir() -> Path:
    """Return the current config directory.

    Resolution order (highest priority first):
      1. ``_config_dir_override`` — set via :func:`set_config_dir` (CLI --config-dir, test fixtures)
      2. ``PENGY_CONFIG_DIR`` env var — CI / global test baseline
      3. ``~/.config/pengy`` — default user config directory
    """
    if _config_dir_override:
        return Path(_config_dir_override).expanduser().resolve()
    env = os.environ.get("PENGY_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".config" / "pengy"

CONFIG_FILE = Path("settings.json")
CHATS_FILE = Path("chats.json")
TASKS_FILE = Path("tasks.json")

def _config_path(*parts: str) -> Path:
    """Resolve a path relative to the config directory."""
    return get_config_dir().joinpath(*parts)

DEFAULT_SYSTEM_MESSAGE = (
    "You are a helpful assistant named Pengy. "
    "The current date is {date} and the user is {username} on host {hostname} which is {osinfo}."
)

DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
    "system_message": DEFAULT_SYSTEM_MESSAGE,
    "tool_confirmation": "none",  # "all" | "safe" | "none"
    "reasoning_effort": "",  # "" (provider default) | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"
    "preserve_reasoning": False,
    "context_keep_turns": 0,
    "ui_scale": 100,
    "theme_mode": "system",  # "system" | "light" | "dark"
    "theme_accent": "default",  # "default" | "blue" | "teal" | "green" | "orange" | "red" | "pink" | "purple"
    "user_agent": "PengyAgent/1.0",
    "llm_timeout": 300,
    "tool_timeout": 300,
    "tool_output_max_chars": 50000,  # chars — snipped (head+tail) if exceeded
    "image_max_dimension": 4096,   # px — Anthropic limit is 8000, 4096 is safe
    "image_max_mb": 4.5,            # MB — Anthropic limit is 5
    "image_quality": 85,             # JPEG quality 0–100
}


def render_system_message(template: str) -> str:
    """Fill dynamic placeholders in a system message template."""
    return template.format(
        date=date.today().strftime("%B %d, %Y"),
        username=getpass.getuser(),
        hostname=socket.gethostname(),
        osinfo=f"{platform.system()} {platform.release()}",
    )


def _atomic_write(target: Path, data) -> None:
    """Write *data* as JSON to *target* atomically (via temp + rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe_json_load(path: Path) -> dict | list | None:
    """Load JSON from *path*, returning None if the file is missing or corrupt.

    A corrupt file is renamed to *.corrupt-timestamp* so the user can
    recover data manually, and the caller will start with a fresh default.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        import time
        backup = path.with_name(
            f"{path.name}.corrupt-{int(time.time())}"
        )
        try:
            shutil.move(str(path), str(backup))
        except OSError:
            pass
        return None


def load_config() -> dict:
    """Load configuration from JSON file, with corruption recovery.

    On first run (no file exists), writes defaults to disk so the file
    is immediately available for hand-editing.
    """
    _config_path().mkdir(parents=True, exist_ok=True)
    saved = _safe_json_load(_config_path("settings.json"))
    if saved is not None and isinstance(saved, dict):
        config = {**DEFAULTS, **saved}
        _migrate_config(config, saved)
        return config
    # First run — persist defaults so the file exists for hand-editing
    config = DEFAULTS.copy()
    save_config(config)
    return config


def _migrate_config(config: dict, saved: dict) -> None:
    """Migrate legacy boolean fields to the new tool_confirmation string.

    *saved* is the raw data from disk (before defaults merge), so we can
    detect whether the user had old-style keys.
    """
    # Already migrated — just clean up any stale legacy keys
    if "tool_confirmation" in saved:
        config.pop("yolo_mode", None)
        config.pop("auto_approve_readonly", None)
        return

    # Old-style keys present — convert them
    yolo = saved.get("yolo_mode", False)
    auto_readonly = saved.get("auto_approve_readonly", False)

    if yolo is True:
        config["tool_confirmation"] = "all"
    elif auto_readonly is True:
        config["tool_confirmation"] = "safe"
    else:
        config["tool_confirmation"] = "none"

    config.pop("yolo_mode", None)
    config.pop("auto_approve_readonly", None)


def save_config(config: dict) -> None:
    """Save configuration to JSON file atomically."""
    _atomic_write(_config_path("settings.json"), config)
