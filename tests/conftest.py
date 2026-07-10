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


# ── 2.  Hard guard ─────────────────────────────────────────────────────────────

def pytest_collection_finish(session: pytest.Session) -> None:
    """Refuse to run if the config dir still points at the real user config."""
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


# ── 3.  Teardown ───────────────────────────────────────────────────────────────

def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Clean up the session temp dir and clear the env override."""
    os.environ.pop("PENGY_CONFIG_DIR", None)
    from pengy.core.config import set_config_dir
    set_config_dir(None)  # clear any leftover programmatic override
    _session_tmp.cleanup()