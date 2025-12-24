#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2025 Stephen Le

# Cherry Studio Sync - GUI Launcher (Mac/Linux)
# This script attempts to find Python and launch the GUI

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Check for uv (modern Python package manager)
if command -v uv &> /dev/null; then
    echo "Found uv, launching with uv run..."
    uv run python cherry_studio_sync.py --gui "$@"
    exit 0
fi

# 2. Check for python3 (standard on Mac/Linux)
if command -v python3 &> /dev/null; then
    echo "Found python3, launching..."
    python3 cherry_studio_sync.py --gui "$@"
    exit 0
fi

# 3. Check for python (might be Python 3 on some systems)
if command -v python &> /dev/null; then
    # Verify it's Python 3
    if python --version 2>&1 | grep -q "Python 3"; then
        echo "Found python, launching..."
        python cherry_studio_sync.py --gui "$@"
        exit 0
    fi
fi

# 4. Check for conda
if command -v conda &> /dev/null; then
    echo "Found conda, launching..."
    conda run python cherry_studio_sync.py --gui "$@"
    exit 0
fi

# Python not found
echo ""
echo "Python not found. Please install Python 3.10+ and ensure it's in your PATH."
echo ""
echo "Options:"
echo "  - Install via your package manager (apt, brew, etc.)"
echo "  - Install via uv: https://docs.astral.sh/uv/"
echo "  - Install via conda: https://conda.io"
echo ""
echo "On Linux, you may also need to install tkinter:"
echo "  Ubuntu/Debian: sudo apt-get install python3-tk"
echo "  Fedora: sudo dnf install python3-tkinter"
exit 1
