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

Always update `pengy/__init__.py` and the local `pengy` package version in `uv.lock` to match. Audit active declarations, update the changelog and affected docs, and leave historical release entries/dependency versions unchanged. Coordinated releases use the same version in PengyR’s `Cargo.toml`, `Cargo.lock`, and `gui/Info.plist`, and PengyCPP’s `CMakeLists.txt`.

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

## GitHub CI/CD (preferred production release)

Commit the tested version bump, then push the branch and an annotated version tag:

```bash
git push origin main
git tag -a vX.Y.Z -m "Pengy vX.Y.Z — short feature summary"
git push origin vX.Y.Z
```

The tag triggers `publish.yml`, which uploads the wheel/sdist to GitHub Releases and publishes
to PyPI via trusted publishing. Do not manually upload the same version first. Use `gh run list`
and `gh run view` to check both branch CI and tag deployment, verify the uploaded assets and
PyPI version, then set a concise release title/body with `gh release edit`. For coordinated
native releases, verify Linux AppImage + .deb, macOS DMG, and Windows ZIP + MSI in both repos.
