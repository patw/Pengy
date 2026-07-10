"""Task/prompt-template management for Pengy.

Tasks are local prompt macros stored as JSON in ``~/.config/pengy/tasks.json``.
They are intentionally simple so other Pengy frontends/ports can adopt the same
format later.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from pengy.core.config import get_config_dir

_PLACEHOLDER_RE = re.compile(r"%([^%\r\n]+)%")


def _tasks_path() -> Path:
    """Return path to tasks.json in the current config directory."""
    return get_config_dir() / "tasks.json"


def _now() -> str:
    return datetime.now().isoformat()


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


def _safe_json_load(path: Path) -> list | None:
    """Load JSON array from *path*, returning None if missing/corrupt.

    Corrupt files are renamed to ``*.corrupt-timestamp`` so they can be
    recovered manually.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise json.JSONDecodeError("Expected a JSON array", "", 0)
        return data
    except (json.JSONDecodeError, OSError):
        import time

        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            shutil.move(str(path), str(backup))
        except OSError:
            pass
        return None


def _normalize_task(task: dict) -> dict:
    """Return a task dict with required fields filled in."""
    now = _now()
    return {
        "id": str(task.get("id") or uuid.uuid4()),
        "title": str(task.get("title") or "Untitled Task"),
        "template": str(task.get("template") or ""),
        "created_at": str(task.get("created_at") or now),
        "updated_at": str(task.get("updated_at") or task.get("created_at") or now),
    }


def load_tasks() -> list[dict]:
    """Load all task templates in insertion order."""
    path = _tasks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _safe_json_load(path)
    if data is None:
        return []
    return [_normalize_task(t) for t in data if isinstance(t, dict)]


def save_tasks(tasks: list[dict]) -> None:
    """Save all task templates in the provided order."""
    path = _tasks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, [_normalize_task(t) for t in tasks])


def create_task(title: str, template: str) -> dict:
    """Create and append a new task template."""
    now = _now()
    task = {
        "id": str(uuid.uuid4()),
        "title": title.strip() or "Untitled Task",
        "template": template,
        "created_at": now,
        "updated_at": now,
    }
    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)
    return task


def update_task(task_id: str, title: str, template: str) -> dict | None:
    """Update an existing task in place, preserving list order."""
    tasks = load_tasks()
    for i, task in enumerate(tasks):
        if task.get("id") == task_id:
            updated = {
                **task,
                "title": title.strip() or "Untitled Task",
                "template": template,
                "updated_at": _now(),
            }
            tasks[i] = updated
            save_tasks(tasks)
            return updated
    return None


def delete_task(task_id: str) -> None:
    """Delete a task template by ID."""
    tasks = [t for t in load_tasks() if t.get("id") != task_id]
    save_tasks(tasks)


def get_task(task_id: str) -> dict | None:
    """Get a task template by ID."""
    for task in load_tasks():
        if task.get("id") == task_id:
            return task
    return None


def extract_placeholders(template: str) -> list[str]:
    """Return unique ``%placeholder%`` names in first-seen order."""
    seen: set[str] = set()
    placeholders: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(template or ""):
        name = match.group(1).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        placeholders.append(name)
    return placeholders


def render_template(template: str, values: dict[str, str]) -> str:
    """Replace ``%placeholder%`` tokens using *values*.

    Placeholder names are stripped of surrounding whitespace before lookup, so
    ``% Youtube URL %`` and ``%Youtube URL%`` both use the same value key.
    Unknown placeholders are left untouched.
    """
    def repl(match: re.Match) -> str:
        name = match.group(1).strip()
        return str(values[name]) if name in values else match.group(0)

    return _PLACEHOLDER_RE.sub(repl, template or "")
