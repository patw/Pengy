"""Pengy Web — entry point."""

import argparse


def _get_version() -> str:
    """Return the Pengy version string."""
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
    parser.add_argument("--trusted-host", action="append", default=[],
                        metavar="HOST",
                        help="Public hostname this server is reached as when "
                             "behind a reverse proxy (e.g. pengy.example). "
                             "Repeatable. Needed only for a proxy in front of "
                             "a loopback bind.")
    parser.add_argument(
        "-v", "--version",
        action="store_true",
        help="Show version information and exit.",
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help="Use a custom config directory instead of ~/.config/pengy.",
    )
    args = parser.parse_args()

    if args.version:
        print(f"Pengy v{_get_version()}")
        return

    if args.config_dir:
        from pengy.core.config import set_config_dir
        set_config_dir(args.config_dir)

    # ``flask`` is a base dependency, so this should not normally trigger — but
    # a `--no-deps` install or a broken environment should get a clear message
    # rather than a bare ModuleNotFoundError.  We probe flask specifically
    # instead of wrapping the app import, so a real bug inside pengy/web/app.py
    # is never misreported as a missing dependency.
    try:
        import flask  # noqa: F401
    except ImportError:
        print(
            "❌ Pengy Web requires Flask, which is missing from this environment.\n"
            '   Install it with:  pip install "pengy[web]"',
            file=sys.stderr,
        )
        raise SystemExit(1)

    # One-time notice about the desktop options (stderr, TTY-only, once per
    # machine) — a server operator should still learn the GUI exists.
    from pengy.core.nudge import show_once

    show_once()

    from pengy.web.app import app, set_bound_host, set_trusted_hosts
    set_bound_host(args.host)
    set_trusted_hosts(args.trusted_host)
    url = f"http://{args.host}:{args.port}"
    print(f"🐧 Pengy Web listening on {url}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("   note: bound beyond loopback — Pengy Web has no auth of its "
              "own, so put it behind a proxy or a VM boundary.")

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
