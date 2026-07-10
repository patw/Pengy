"""Pengy Web — entry point."""

import argparse


def _get_version() -> str:
    """Return the Pengy version string (actual build version)."""
    try:
        from importlib.metadata import version as _v
        return _v("pengy")
    except Exception:
        from pengy import __version__
        return __version__


def main():
    parser = argparse.ArgumentParser(
        description="Pengy Web — chat with LLMs from your browser",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Bind port (default: 5000)")
    parser.add_argument("--debug", action="store_true",
                        help="Flask debug mode (do not use behind nginx)")
    parser.add_argument(
        "-v", "--version",
        action="store_true",
        help="Show version information and exit.",
    )
    args = parser.parse_args()

    if args.version:
        print(f"Pengy v{_get_version()}")
        return

    from pengy.web.app import app
    print(f"🐧 Pengy Web listening on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
