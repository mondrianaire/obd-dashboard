@echo off
REM ============================================================
REM   Drive Telemetry Dashboard - refresh
REM   Double-click this file (or run it from cmd) to:
REM      - scan your Dropbox CsvLogs folder for any new drives
REM      - rebuild telemetry_data.json
REM      - re-inject the data into telemetry_dashboard.html
REM      - update the version badge with today's date
REM ============================================================

cd /d "%~dp0"

REM Prefer "py -3" launcher if present (standard on Windows Python)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 refresh_dashboard.py
    goto :done
)

REM Fall back to plain "python"
where python >nul 2>nul
if %errorlevel%==0 (
    python refresh_dashboard.py
    goto :done
)

echo Python was not found on PATH. Install Python 3 and re-run.
pause
exit /b 1

:done
echo.
echo Press any key to close...
pause >nul
