@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
:start
if not exist "start_beta.py" goto incomplete_package
if not exist "requirements-beta.txt" goto incomplete_package
if exist "web_service\beta.py" goto find_python
if exist "..\web_service\beta.py" goto find_python
goto incomplete_package
:find_python
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 start_beta.py %*
goto finished
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python start_beta.py %*
:finished
if errorlevel 1 goto startup_failed
echo.
echo CalendarSync 실행이 종료되었습니다.
goto wait_for_action
:incomplete_package
echo.
echo [실행 중단] 실행에 필요한 파일이 같은 폴더에 없습니다.
echo ZIP 안에서 start_beta.cmd만 열면 실행되지 않습니다.
echo.
echo 1. 이 창을 닫고 원래 ZIP 파일을 마우스 오른쪽 버튼으로 누르세요.
echo 2. '모두 압축 풀기'를 선택하세요.
echo 3. 풀린 폴더에서 start_beta.cmd를 실행하세요.
echo    같은 폴더에 start_beta.py, requirements-beta.txt, web_service가 있어야 합니다.
echo.
echo 먼저 ZIP 전체를 풀어 주세요. 이 안내는 Python 설치 여부와 관계없습니다.
goto wait_for_action
:missing_python
echo.
echo [실행 중단] Python 실행 명령을 찾지 못했습니다.
echo Python 3.11 이상을 설치하고 Add Python to PATH를 선택해 주세요.
goto wait_for_action
:startup_failed
echo.
echo [실행 중단] 위에 표시된 오류 때문에 베타 서버를 시작하지 못했습니다.
echo 오류 내용을 확인해 주세요. 이 창은 자동으로 닫히지 않습니다.
:wait_for_action
echo.
choice /c RQ /n /m "R: 다시 시도 / Q: 창 닫기 : "
if errorlevel 2 exit /b 1
goto start
