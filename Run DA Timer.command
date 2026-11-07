#!/bin/bash
# Run DA Timer from source on a Mac (double-click in Finder).
# Needs Python 3 from python.org (it includes the Tk the app uses).
cd "$(dirname "$0")"
PY=python3
if ! $PY -c "import tkinter" 2>/dev/null; then
  echo "Python with Tk isn't installed. Get Python 3.12 from https://www.python.org/downloads/macos/"
  read -p "Press Enter to close"; exit 1
fi
# exact, hash-checked versions (requirements.txt)
$PY -c "import PIL, openpyxl, numpy, sounddevice, AppKit" 2>/dev/null || \
  $PY -m pip install --quiet --require-hashes -r requirements.txt || {
    echo "Couldn't install DA Timer's parts - see above. Python 3.12 is recommended."
    read -p "Press Enter to close"; exit 1; }
exec $PY da_timer.py
