"""Configuration management for Pengy."""
import getpass
import json
import os
import platform
import socket
from datetime import date
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "pengy"
CONFIG_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SYSTEM_MESSAGE = (
    "You are a helpful assistant. "
    "The current date is {date} and the user is {username} on host {hostname} which is {osinfo}."
)

DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
    "system_message": DEFAULT_SYSTEM_MESSAGE,
    "yolo_mode": False,
    "ui_scale": 100,
    "user_agent": "PengyAgent/1.0",
}


def render_system_message(template: str) -> str:
    """Fill dynamic placeholders in a system message template."""
    return template.format(
        date=date.today().strftime("%B %d, %Y"),
        username=getpass.getuser(),
        hostname=socket.gethostname(),
        osinfo=f"{platform.system()} {platform.release()}",
    )


def load_config() -> dict:
    """Load configuration from JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        return {**DEFAULTS, **saved}
    return DEFAULTS.copy()


def save_config(config: dict) -> None:
    """Save configuration to JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
