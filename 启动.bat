@echo off
REM ============================================================================
REM  Double-click me to start the demo.
REM  ASCII-only on purpose: cmd.exe reads .bat in the OEM/ANSI codepage, so a
REM  UTF-8 Chinese line here would show as mojibake on some machines.
REM  All Chinese output comes from quickstart.ps1 (which is saved as UTF-8 with
REM  BOM, so PowerShell reads it correctly).
REM ============================================================================
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo  doudian-agent  -  starting local demo
echo  (first run installs Python deps, about 1-2 minutes)
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\quickstart.ps1"
set EXITCODE=%ERRORLEVEL%

echo.
if "%EXITCODE%"=="0" (
  echo [OK] Started. If the browser did not open, visit: http://localhost:5173
) else (
  echo [FAILED] exit code %EXITCODE%
  echo See the messages above; the script prints what to do next.
)
echo.
echo Press any key to close this window (the services keep running).
pause >nul
