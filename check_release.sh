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

# ── 1. Version ──────────────────────────────────────────────────────
echo "--- Version ---"
PKG_VER=$(grep -oP 'version\s*=\s*"\K[\d.]+' pyproject.toml | head -1)
echo "  pyproject.toml: $PKG_VER"
if [ -n "$PKG_VER" ]; then
    ok "Version found: $PKG_VER"
else
    fail "Could not parse version from pyproject.toml"
fi

# ── 2. Clean build with python -m build ─────────────────────────────
echo "--- Package build ---"
if command -v python3 &>/dev/null; then
    rm -rf dist/
    if python3 -m build > /tmp/pengy_build.log 2>&1; then
        WHEEL=$(ls -1 dist/*.whl 2>/dev/null | head -1)
        TARBALL=$(ls -1 dist/*.tar.gz 2>/dev/null | head -1)
        if [ -n "$WHEEL" ]; then
            ok "Wheel built: $(basename "$WHEEL")"
        fi
        if [ -n "$TARBALL" ]; then
            ok "Sdist built: $(basename "$TARBALL")"
        fi
    else
        fail "Build failed — check /tmp/pengy_build.log"
    fi
else
    fail "python3 not found"
fi

# ── 3. Twine check ──────────────────────────────────────────────────
echo "--- Twine check ---"
if python3 -m twine check dist/* > /tmp/pengy_twine.log 2>&1; then
    ok "twine check passed"
else
    warn "twine check failed — check /tmp/pengy_twine.log"
    head -20 /tmp/pengy_twine.log
fi

# ── 4. Tests ────────────────────────────────────────────────────────
echo "--- Tests ---"
if [ -d venv ]; then
    if venv/bin/python -m pytest tests/ -x -q > /tmp/pengy_tests.log 2>&1; then
        ok "Tests pass"
    else
        warn "Tests failed — check /tmp/pengy_tests.log"
        tail -10 /tmp/pengy_tests.log
    fi
else
    warn "No venv/ found — skipping test run (run: python3 -m venv venv && venv/bin/pip install -e '.[cli,web]' pytest)"
fi

# ── 5. CLI imports ──────────────────────────────────────────────────
echo "--- Import checks ---"
if [ -d venv ]; then
    venv/bin/python -c "from pengy.main import main; print('  main OK')" 2>/dev/null && ok "pengy.main imports" || warn "pengy.main import failed"
    venv/bin/python -c "from pengy.cli.main import main; print('  CLI OK')" 2>/dev/null && ok "pengy.cli.main imports" || warn "pengy.cli.main import failed"
    venv/bin/python -c "from pengy.web.main import main; print('  Web OK')" 2>/dev/null && ok "pengy.web.main imports" || warn "pengy.web.main import failed"
    venv/bin/python -c "from pengy.core.config import load_config; print('  Core OK')" 2>/dev/null && ok "pengy.core imports" || warn "pengy.core import failed"
else
    warn "No venv — skipping import checks"
fi

# ── 5b. Entry-point smoke test (--version + --help) ────────────────
echo "--- Entry-point smoke test ---"
if [ -d .venv ]; then
    PY=".venv/bin/python"
elif [ -d venv ]; then
    PY="venv/bin/python"
else
    PY=""; warn "No venv/.venv — skipping entry-point smoke test"
fi
if [ -n "$PY" ]; then
    PASS=0; FAIL=0
    smoke() {
        local label="$1" module="$2"
        local ver help
        ver=$("$PY" -m "$module" --version 2>/dev/null)
        help=$("$PY" -m "$module" --help    2>/dev/null)
        if echo "$ver" | grep -q "^Pengy v" && \
           echo "$help" | grep -qiE "usage|options"; then
            ok "$label --version + --help"
            PASS=$((PASS+1))
        else
            warn "$label --version/--help failed"
            FAIL=$((FAIL+1))
        fi
    }
    smoke "pengy.cli.main" pengy.cli.main
    smoke "pengy.web.main" pengy.web.main
    smoke "pengy.main"     pengy.main
    if [ "$FAIL" -gt 0 ]; then
        echo -e "  \033[33m$FAIL entry-point(s) failed smoke test\033[0m"
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
