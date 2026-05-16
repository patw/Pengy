#!/bin/bash
# Run Pengy
export PYTHONPATH="$(dirname "$0"):$PYTHONPATH"
python3 "$(dirname "$0")/pengy/main.py" "$@"
