@REM This Source Code Form is subject to the terms of the Mozilla Public
@REM License, v. 2.0. If a copy of the MPL was not distributed with this
@REM file, You can obtain one at https://mozilla.org/MPL/2.0/.
@REM
@REM Copyright (c) 2025 Stephen Le

@echo off
setlocal

REM Cherry Studio Sync - GUI Launcher (Windows)
REM This script attempts to find Python and launch the GUI

cd /d "%~dp0"

REM 1. Check for uv (modern Python package manager)
where uv >nul 2>&1 && (
    echo Found uv, launching with uv run...
    uv run python cherry_studio_sync.py --gui %*
    goto :end
)

REM 2. Check for system Python
where python >nul 2>&1 && (
    echo Found Python, launching...
    python cherry_studio_sync.py --gui %*
    goto :end
)

REM 3. Check for py launcher (Windows Python launcher)
where py >nul 2>&1 && (
    echo Found py launcher, launching with Python 3...
    py -3 cherry_studio_sync.py --gui %*
    goto :end
)

REM 4. Check for conda
where conda >nul 2>&1 && (
    echo Found conda, launching...
    call conda run python cherry_studio_sync.py --gui %*
    goto :end
)

REM Python not found
echo.
echo Python not found. Please install Python 3.10+ and ensure it's in your PATH.
echo.
echo Options:
echo   - Download from https://python.org
echo   - Install via uv: https://docs.astral.sh/uv/
echo   - Install via conda: https://conda.io
echo.
pause

:end
endlocal
