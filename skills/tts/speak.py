#!/usr/bin/env python3
"""TTS via spd-say. Reads text from args or stdin."""
import subprocess, sys

def main():
    text = " ".join(sys.argv[1:])
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        print("Usage: python speak.py <text> (or pipe text via stdin)", file=sys.stderr)
        sys.exit(1)
    subprocess.run(["spd-say", text])

if __name__ == "__main__":
    main()
