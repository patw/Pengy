#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ICON_DIR="$HOME/.local/share/icons"
DESKTOP_DIR="$HOME/.local/share/applications"

mkdir -p "$ICON_DIR" "$DESKTOP_DIR"

cp "$REPO_DIR/pengy.png" "$ICON_DIR/pengy.png"

sed \
    -e "s|PENGY_EXEC|$REPO_DIR/run_pengy.sh|g" \
    -e "s|PENGY_ICON|$ICON_DIR/pengy.png|g" \
    "$REPO_DIR/pengy.desktop" > "$DESKTOP_DIR/pengy.desktop"

echo "Installed to $DESKTOP_DIR/pengy.desktop"
