# Release Guide

How to build and publish Pengy to PyPI.

## Prerequisites

```bash
pip install build twine
```

## Bump the version

Update the version in `pyproject.toml`:

```toml
version = "1.0.1"  # <-- bump this
```

Also update `pengy/__init__.py` if you maintain a `__version__` there.

## Build

```bash
# Clean previous builds
rm -rf dist/

# Build source distribution and wheel
python -m build
```

## Check

Inspect the built package:

```bash
# Check the contents
tar tzf dist/pengy-*.tar.gz

# Run twine check for common issues
twine check dist/*
```

## Upload

### Test PyPI first (recommended)

```bash
twine upload --repository testpypi dist/*

# Install from test PyPI to verify
pip install --index-url https://test.pypi.org/simple/ pengy
# and, to check the GUI extra resolves:
pip install --index-url https://test.pypi.org/simple/ "pengy[gui]"
```

### Production PyPI

```bash
twine upload dist/*
```

## Verify

```bash
# Fresh venv, DEFAULT install — this is the experience most users get, so it is
# the one that must work:
python -m venv /tmp/pengy-test
/tmp/pengy-test/bin/pip install pengy
/tmp/pengy-test/bin/pengy-cli --version            # → Pengy vX.Y.Z
/tmp/pengy-test/bin/pengy-web --version            # → Pengy vX.Y.Z
/tmp/pengy-test/bin/pengy-cli "Hello, what model are you?"

# Then confirm the GUI extra still resolves (downloads PySide6-Essentials):
/tmp/pengy-test/bin/pip install "pengy[gui]"
/tmp/pengy-test/bin/pengy --version                # → Pengy vX.Y.Z
```

## Git tag (after successful release)

```bash
git tag v1.0.1
git push origin v1.0.1
```
