#!/usr/bin/env bash
cd "$(dirname "$0")"
python -m pengy.web.main "$@"
