# Building from Source (Python)

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Install from source

```bash
git clone https://github.com/patw/Pengy.git
cd Pengy

# CLI + Web UI (no Qt — this is all you need on a server)
uv sync
# or: pip install -e .

# Add the Qt desktop GUI
uv sync --extra gui
# or: pip install -e ".[gui]"

# Everything (`all` is an alias for `gui`)
uv sync --extra all
```

## Run after building

```bash
# CLI
pengy-cli

# Web
pengy-web

# GUI (requires the gui extra)
pengy
```

## Running tests

```bash
python -m pytest tests/ -v
```

## Building a distributable package

```bash
pip install build
python -m build
# → dist/pengy-<version>.tar.gz, dist/pengy-<version>-py3-none-any.whl
```

## Platform notes

- **Linux:** the default install has no Qt at all, so it works on a headless box out of the box. The desktop GUI needs the `gui` extra (PySide6-Essentials) and a display.
- **macOS:** If your default `/usr/bin/python3` is older than 3.10, use `uv` — it installs a compatible Python automatically. PySide6-Essentials ships a `universal2` wheel.
- **Windows:** PySide6-Essentials wheels are available for `win_amd64` and `win_arm64`. The CLI and web UI work natively.
- **Containers / musl:** there are no PySide6 wheels for musl-based images (Alpine), which is one reason Qt is not in the default install — `pip install pengy` stays installable there.
