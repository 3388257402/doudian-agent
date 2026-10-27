@echo off
REM ============================================================================
REM  Double-click me to stop whatever is listening on 8000 / 5173.
REM
REM  Why kill-by-port instead of kill-by-PID:
REM    the launcher starts a process tree (cmd.exe -> npm -> node, python -> worker).
REM    Killing only the parent leaves the child holding the port, and the next start
REM    fails with "port already in use", which looks like a broken project.
REM  Safety: only processes actually LISTENING on 8000/5173 are touched --
REM    nothing else on this machine is killed.
REM ============================================================================
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo Stopping services on ports 8000 and 5173 ...
echo.

set FOUND=0
for %%P in (8000 5173) do (
  for /f "tokens=5" %%A in ('netstat -ano ^| findstr /R /C:":%%P .*LISTENING"') do (
    echo   port %%P -^> PID %%A
    taskkill /PID %%A /T /F >nul 2>&1
    set FOUND=1
  )
)

if "%FOUND%"=="0" (
  echo   nothing was listening - services already stopped.
) else (
  echo.
  echo Done. Verifying ...
  REM 用 ping 代替 timeout：timeout 在输入被重定向时（脚本/管道里调用）会直接失败，
  REM 而 ping -n 3 在两种情况下都能当"等 2 秒"用。
  ping -n 3 127.0.0.1 >nul
  netstat -ano | findstr /R /C:":8000 .*LISTENING" /C:":5173 .*LISTENING" >nul 2>&1
  if errorlevel 1 (
    echo   [OK] ports 8000 / 5173 are free now.
  ) else (
    echo   [WARN] a port is still listening. Run this script again, or kill it manually:
    echo          netstat -ano ^| findstr ":8000 :5173"
  )
)

echo.
echo Press any key to close.
pause >nul
