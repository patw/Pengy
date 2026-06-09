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
pip install --index-url https://test.pypi.org/simple/ pengy[all]
```

### Production PyPI

```bash
twine upload dist/*
```

## Verify

```bash
# Create a fresh venv and test
python -m venv /tmp/pengy-test
/tmp/pengy-test/bin/pip install pengy[all]
/tmp/pengy-test/bin/pengy-cli "Hello, what model are you?"
```

## Git tag (after successful release)

```bash
git tag v1.0.1
git push origin v1.0.1
```
