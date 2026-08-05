@echo off
REM Launch PolyClusters on Windows.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
)
".venv\Scripts\pythonw.exe" -m polyclusters
goto :eof
:err
echo.
echo Setup failed. Make sure Python 3.11+ is installed and on PATH.
pause
