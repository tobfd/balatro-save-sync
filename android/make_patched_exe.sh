#!/bin/sh
# Linux/macOS helper: builds the patched Balatro.exe for balatro-mobile-maker.
# Usage: ./make_patched_exe.sh --out ~/balatro-mobile-maker [--device Phone]
exec python3 "$(dirname "$0")/make_patched_exe.py" "$@"
