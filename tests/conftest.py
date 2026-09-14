"""Shared pytest configuration and safety guards for the Pengy test-suite.

This file exists primarily to **prevent tests from ever writing to the
user's real config directory** (``~/.config/pengy``).

It does three things:

1. **Baseline redirect** — before any test collects, it sets
   ``PENGY_CONFIG_DIR`` to a per-session temp directory so that even
   tests without an explicit ``set_config_dir`` fixture write to a
   throwaway location.

2. **Hard guard** — after collection, it checks that
   :func:`pengy.core.config.get_config_dir` does *not* resolve to the
   real user config.  If it does, the entire test run is aborted.

3. **Teardown** — after all tests finish, the override is cleared and
   the temp directory is deleted.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# ── 1.  Baseline redirect ──────────────────────────────────────────────────────

# Create a single temp dir for the whole session.  Individual fixtures
# (e.g. ``tmp_cfg_dir`` in test_core.py, ``tmp_dirs`` in test_web.py) can
# still override this with their own temp dirs via ``set_config_dir``.
_session_tmp = tempfile.TemporaryDirectory(prefix="pengy-test-")
_session_tmp_path = Path(_session_tmp.name)

# Set the env var so that any code that reads the config dir *before*
# a fixture runs (e.g. module-level imports) still gets the temp path.
import os
os.environ["PENGY_CONFIG_DIR"] = str(_session_tmp_path)

# Same treatment for the XDG desktop directories.  ``pengy --install-launcher``
# writes $XDG_DATA_HOME/applications/pengy.desktop plus an icon — that is *real*
# desktop state (a live application-menu entry), not test state.  Without this,
# a test that forgets its ``xdg`` fixture would silently replace the developer's
# own Pengy launcher.  (2026-09-14: a hand-run `pengy --install-launcher`
# outside the suite overwrote the AppImage launcher on beholaptop; the guard
# below now catches both cases.)
_session_xdg = _session_tmp_path / "xdg-data"


def real_xdg_data_home() -> Path:
    """XDG_DATA_HOME a normal user would get (``~/.local/share``)."""
    return (Path.home() / ".local" / "share").resolve()


_caller_xdg = os.environ.get("XDG_DATA_HOME")
if _caller_xdg and Path(_caller_xdg).expanduser().resolve() == real_xdg_data_home():
    # Somebody explicitly pointed the suite at the live desktop directory.  Leave
    # it exactly as asked so the collection-time guard below can refuse to run
    # — silently redirecting would hide the mistake instead of failing on it.
    pass
else:
    os.environ["XDG_DATA_HOME"] = str(_session_xdg)


# ── 2.  Hard guard ─────────────────────────────────────────────────────────────

def pytest_collection_finish(session: pytest.Session) -> None:
    """Refuse to run if the config dir or XDG data dir point at real user state."""
    from pengy.core.config import get_config_dir

    resolved = get_config_dir()
    real = (Path.home() / ".config" / "pengy").resolve()

    if resolved == real:
        pytest.exit(
            f"\n\033[91m*** FATAL: Refusing to run tests against the real config "
            f"directory ({real}).\n"
            f"*** This would overwrite live settings, API keys, and chat "
            f"history.\n"
            f"*** Ensure conftest.py's PENGY_CONFIG_DIR guard is working.\033[0m\n",
            returncode=1,
        )

    # `pengy --install-launcher` writes a real application-menu entry under
    # $XDG_DATA_HOME; running the suite against the live one would replace the
    # developer's own launcher (e.g. one that starts a native AppImage build).
    from pengy.core.launcher import _data_home

    if _data_home().resolve() == real_xdg_data_home():
        pytest.exit(
            f"\n\033[91m*** FATAL: Refusing to run tests with XDG_DATA_HOME "
            f"pointing at real user state ({real_xdg_data_home()}).\n"
            f"*** Launcher tests would overwrite ~/.local/share/applications/"
            f"pengy.desktop.\n"
            f"*** Ensure conftest.py's XDG redirect is working.\033[0m\n",
            returncode=1,
        )


# ── 3.  Teardown ───────────────────────────────────────────────────────────────

def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Clean up the session temp dir and clear the env overrides."""
    os.environ.pop("PENGY_CONFIG_DIR", None)
    os.environ.pop("XDG_DATA_HOME", None)
    from pengy.core.config import set_config_dir
    set_config_dir(None)  # clear any leftover programmatic override
    _session_tmp.cleanup()