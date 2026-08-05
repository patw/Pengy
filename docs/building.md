# Building from Source (Python)

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Install from source

```bash
git clone https://github.com/patw/Pengy.git
cd Pengy

# With uv (recommended)
uv sync --extra all

# Or with pip
pip install -e ".[all]"
```

## Run after building

```bash
# GUI
pengy

# CLI
pengy-cli

# Web
pengy-web
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

- **Linux:** Qt6 (PySide6) installs via pip. On headless systems, `pengy[cli]` or `pengy[web]` work without a display.
- **macOS:** If your default `/usr/bin/python3` is older than 3.10, use `uv` — it installs a compatible Python automatically.
- **Windows:** PySide6 wheels are available for Windows. The CLI and web UI work natively.
