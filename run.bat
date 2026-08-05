@echo off
REM Launch PolyClusters on Windows.
REM First run also builds the virtual environment and makes the shortcuts,
REM so a fresh clone is one double-click away from a desktop icon.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    echo Installing dependencies, this takes a minute...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
)

REM Shortcuts embed absolute paths, so they are made here rather than shipped.
if not exist "PolyClusters.lnk" (
    echo Creating shortcuts...
    ".venv\Scripts\python.exe" scripts\make_shortcut.py
)

start "" ".venv\Scripts\pythonw.exe" -m polyclusters
goto :eof

:err
echo.
echo Setup failed. Make sure Python 3.11+ is installed and on PATH.
pause
