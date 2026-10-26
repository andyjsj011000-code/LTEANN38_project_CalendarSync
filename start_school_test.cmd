@echo off
setlocal
cd /d "%~dp0"
if not exist "deploy\school.test.env" (
  echo Create deploy\school.test.env from deploy\school.test.env.example first.
  echo Do not put real secrets into the example file or Git.
  pause
  exit /b 1
)
where py >nul 2>&1
if errorlevel 1 (
  python -m web_service --school --env-file "deploy\school.test.env"
) else (
  py -3 -m web_service --school --env-file "deploy\school.test.env"
)
if errorlevel 1 (
  echo.
  echo Test server could not start. Check configuration and requirements-school.txt.
  pause
)
