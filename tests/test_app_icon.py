"""Exercise real native icon conversion, which --version never reaches."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("PySide6", reason="Requires the optional GUI extra")


def test_application_icon_native_conversion():
    # A subprocess turns a native SIGTRAP into a normal test failure rather
    # than aborting the entire suite. Do not force offscreen on macOS: Cocoa
    # is the code path this regression needs to exercise.
    env = os.environ.copy()
    if sys.platform == "darwin":
        env.pop("QT_QPA_PLATFORM", None)
    elif sys.platform != "win32" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"
    code = """
from pathlib import Path
from PySide6.QtWidgets import QApplication
from pengy.ui.app_icon import application_icon
import pengy
app = QApplication([])
icon = application_icon(Path(pengy.__file__).parent / 'assets' / 'icon.png')
assert not icon.isNull()
assert not icon.pixmap(256, 256).isNull()
app.setWindowIcon(icon)
app.processEvents()
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
