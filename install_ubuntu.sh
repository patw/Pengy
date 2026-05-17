#!/bin/bash
set -e

sudo apt install -y \
    python3-pyside6.qtcore \
    python3-pyside6.qtgui \
    python3-pyside6.qtwidgets \
    python3-openai \
    python3-markdown \
    python3-pygments

# ddgs is not packaged in Ubuntu — install for the current user only
pip install --user ddgs
