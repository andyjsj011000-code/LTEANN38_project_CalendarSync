@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 start_beta.py %*
goto finished
:use_python
python start_beta.py %*
:finished
if errorlevel 1 echo Please install Python 3.11 or newer, or check the error above.
pause
