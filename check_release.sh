#!/bin/bash
# Pre-flight checks — run before `git push --tags`
# For PyPI releases. Catches common issues: version drift, broken imports, test failures.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

WARNINGS=0
warn() { echo -e "\033[33m  WARNING: $*\033[0m"; WARNINGS=$((WARNINGS + 1)); }
fail() { echo -e "\033[31m  FAIL: $*\033[0m"; exit 1; }
ok()   { echo -e "\033[32m  ✓ $*\033[0m"; }

echo "========================================="
echo " Pengy (Python) Pre-Flight Release Check"
echo "========================================="

# ── 1. Version, and that all three sources agree ────────────────────
echo "--- Version ---"
PKG_VER=$(grep -oP 'version\s*=\s*"\K[\d.]+' pyproject.toml | head -1)
echo "  pyproject.toml:        $PKG_VER"
if [ -z "$PKG_VER" ]; then
    fail "Could not parse version from pyproject.toml"
fi
ok "Version found: $PKG_VER"

# The version lives in three places here, and they have drifted before (a 1.3.x
# release built a wheel whose package said something else). A mismatch is a
# release blocker, not a warning: pyproject decides what PyPI serves, while
# pengy/__init__.py decides what `pengy.__version__` reports.
INIT_VER=$(grep -oP '^__version__ = "\K[^"]+' pengy/__init__.py | head -1)
LOCK_VER=$(python3 - <<'PY'
import re
m = re.search(r'\[\[package\]\]\nname = "pengy"\nversion = "([^"]+)"', open("uv.lock").read())
print(m.group(1) if m else "")
PY
)
echo "  pengy/__init__.py:     ${INIT_VER:-<missing>}"
echo "  uv.lock (pengy):       ${LOCK_VER:-<missing>}"
if [ "$INIT_VER" = "$PKG_VER" ] && [ "$LOCK_VER" = "$PKG_VER" ]; then
    ok "pyproject, __init__.py and uv.lock all say $PKG_VER"
else
    fail "Version drift — pyproject=$PKG_VER __init__=${INIT_VER:-?} uv.lock=${LOCK_VER:-?} (bump all three)"
fi

# ── 2. Clean build with python -m build (or uv build) ──────────────
echo "--- Package build ---"
rm -rf dist/
# Build from a scratch cwd, NOT the repo root. A leftover top-level build/ (from
# an older `python -m build` or a `uv build`) shadows the `build` package itself,
# so `python3 -m build` dies with "No module named build.__main__" and the check
# reported the wrong thing ("Build failed") while the package was fine.
BUILD_CWD=$(mktemp -d)
if ( cd "$BUILD_CWD" && python3 -c 'import build' ) >/dev/null 2>&1; then
    BUILDER="python3 -m build"
elif command -v uv >/dev/null 2>&1; then
    BUILDER="uv build"   # no `build` module installed — uv ships its own
else
    BUILDER=""
fi
if [ -z "$BUILDER" ]; then
    fail "Neither the 'build' module nor uv is available — pip install build"
fi
echo "  builder: $BUILDER (cwd: $BUILD_CWD)"
if ( cd "$BUILD_CWD" && $BUILDER "$ROOT" ) > /tmp/pengy_build.log 2>&1; then
    WHEEL=$(ls -1 dist/*.whl 2>/dev/null | head -1)
    TARBALL=$(ls -1 dist/*.tar.gz 2>/dev/null | head -1)
    if [ -n "$WHEEL" ]; then
        ok "Wheel built: $(basename "$WHEEL")"
    else
        fail "Build reported success but dist/*.whl is missing — check /tmp/pengy_build.log"
    fi
    if [ -n "$TARBALL" ]; then
        ok "Sdist built: $(basename "$TARBALL")"
    fi
    # The wheel's own metadata is what PyPI shows; never trust the filename alone.
    WVER=$(python3 - "$WHEEL" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
meta = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")][0]
for line in z.read(meta).decode().splitlines():
    if line.startswith("Version: "):
        print(line.split(": ", 1)[1].strip())
        break
PY
)
    if [ "$WVER" = "$PKG_VER" ]; then
        ok "wheel metadata Version: $WVER"
    else
        fail "wheel metadata says ${WVER:-<none>} but pyproject says $PKG_VER"
    fi
else
    fail "Build failed ($BUILDER) — check /tmp/pengy_build.log"
fi
rm -rf "$BUILD_CWD"

# ── 3. Twine check ──────────────────────────────────────────────────
echo "--- Twine check ---"
if python3 -c 'import twine' >/dev/null 2>&1; then
    if python3 -m twine check dist/* > /tmp/pengy_twine.log 2>&1; then
        ok "twine check passed"
    else
        warn "twine check failed — check /tmp/pengy_twine.log"
        head -20 /tmp/pengy_twine.log
    fi
else
    warn "twine not installed — skipping metadata check (uvx twine check 'dist/*' works)"
fi

# ── 3b. Which interpreter has the deps? (.venv from uv, or venv) ──
if [ -d .venv ]; then
    PY=".venv/bin/python"
elif [ -d venv ]; then
    PY="venv/bin/python"
else
    PY=""
fi

# ── 4. Tests ────────────────────────────────────────────────────────
# Qt-importing test files self-skip without PySide6 (as they do in CI), so this
# runs on a headless box. Falls back to uv when the venv has no pytest, which is
# the normal state of a uv-managed checkout.
#
# XDG_DATA_HOME is unset on purpose: conftest.py redirects it to a session temp
# dir and refuses to run against the real one (so a launcher test can never
# overwrite your desktop entry). Distrobox exports the host's value into the dev
# guest, which made that guard fire on a perfectly good checkout.
echo "--- Tests ---"
TEST_CMD=""
if [ -n "$PY" ] && "$PY" -c 'import pytest' >/dev/null 2>&1; then
    TEST_CMD="env -u XDG_DATA_HOME $PY -m pytest tests/ -x -q"
elif command -v uv >/dev/null 2>&1; then
    TEST_CMD="env -u XDG_DATA_HOME uv run --with pytest python -m pytest tests/ -x -q"
fi
if [ -n "$TEST_CMD" ]; then
    if $TEST_CMD > /tmp/pengy_tests.log 2>&1; then
        ok "Tests pass ($TEST_CMD)"
    else
        warn "Tests failed — check /tmp/pengy_tests.log"
        tail -10 /tmp/pengy_tests.log
    fi
else
    warn "No pytest and no uv — skipping tests"
fi

# ── 5. CLI imports ──────────────────────────────────────────────────
echo "--- Import checks ---"
if [ -n "$PY" ]; then
    "$PY" -c "from pengy.main import main" 2>/dev/null && ok "pengy.main imports" || warn "pengy.main import failed"
    "$PY" -c "from pengy.cli.main import main" 2>/dev/null && ok "pengy.cli.main imports" || warn "pengy.cli.main import failed"
    "$PY" -c "from pengy.web.main import main" 2>/dev/null && ok "pengy.web.main imports" || warn "pengy.web.main import failed"
    "$PY" -c "from pengy.core.config import load_config" 2>/dev/null && ok "pengy.core imports" || warn "pengy.core import failed"
fi

# ── 5b. Entry-point smoke test (--version + --help) ────────────────
echo "--- Entry-point smoke test ---"
if [ -n "$PY" ]; then
    PASS=0; FAILED=0
    smoke() {
        local label="$1" module="$2"
        local ver help
        ver=$("$PY" -m "$module" --version 2>/dev/null)
        help=$("$PY" -m "$module" --help    2>/dev/null)
        if echo "$ver" | grep -q "^Pengy v$PKG_VER" && \
           echo "$help" | grep -qiE "usage|options"; then
            ok "$label --version ($PKG_VER) + --help"
            PASS=$((PASS+1))
        else
            warn "$label --version/--help failed (got: ${ver:-<nothing>})"
            FAILED=$((FAILED+1))
        fi
    }
    smoke "pengy.cli.main" pengy.cli.main
    smoke "pengy.web.main" pengy.web.main
    smoke "pengy.main"     pengy.main
    if [ "$FAILED" -gt 0 ]; then
        echo -e "  \033[33m$FAILED entry-point(s) failed smoke test\033[0m"
    fi
fi

# ── 6. Skills directory ─────────────────────────────────────────────
echo "--- Skills ---"
if [ -f skills/skill_index.md ]; then
    SKILL_COUNT=$(find skills -name '*_skill.md' | wc -l)
    ok "Skills directory present ($SKILL_COUNT skill files, index exists)"
else
    warn "skills/skill_index.md not found"
fi

# ── 7. CI workflow permissions ──────────────────────────────────────
echo "--- Release workflow ---"
if grep -q 'id-token: write' .github/workflows/publish.yml 2>/dev/null; then
    ok "publish.yml has 'id-token: write' for PyPI trusted publishing"
else
    warn "publish.yml missing 'id-token: write' — PyPI upload may fail"
fi
if grep -q 'contents: write' .github/workflows/publish.yml 2>/dev/null; then
    ok "publish.yml has 'contents: write' for GitHub Release"
else
    warn "publish.yml missing 'contents: write' — GitHub Release may fail"
fi

# ── Summary ─────────────────────────────────────────────────────────
echo ""
echo "========================================="
if [ $WARNINGS -eq 0 ]; then
    echo -e "\033[32m All checks passed! Ready to tag.\033[0m"
else
    echo -e "\033[33m $WARNINGS warning(s) found — review above before tagging.\033[0m"
fi
echo "========================================="
