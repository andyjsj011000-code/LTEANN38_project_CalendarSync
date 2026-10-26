@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  python -m web_service --demo
) else (
  py -3 -m web_service --demo
)
if errorlevel 1 (
  echo.
  echo Preview could not start. Install Python and requirements-web.txt, then retry.
  pause
)
