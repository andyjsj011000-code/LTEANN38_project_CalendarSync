# CalendarSync

Microsoft Teams 채널 공지사항(텍스트/이미지)을 AI가 분석하여 사용자의 Teams 캘린더에 일정을 자동으로 동기화해 주는 서비스입니다.

## Tech Stack
- **Framework**: FastAPI (Python 3.11+)
- **API**: Microsoft Graph API, OpenAI API (gpt-4o-mini)
- **Authentication**: MSAL (Microsoft Authentication Library)

## 약관 및 방침
- [개인정보 처리방침](./docs/privacy.md)
- [서비스 이용약관](./docs/terms.md)


## 학교 계정 웹서비스

실제 Microsoft 로그인 시작점: `web_service.school:create_school_app`.
구현 범위·보안 경계·설정·배포 전 검증: [docs/SCHOOL_WEB.md](docs/SCHOOL_WEB.md).
이 설정은 기존 Teams 봇과 별도 프로세스이며 자동으로 운영 서버에 배포되지 않습니다.


## 웹 테스트 서버 · 현재 시작점

- 화면 미리보기: `python -m web_service --demo` 또는 `start_web_preview.cmd` (예시 전용, localhost).
- 실제 학교 계정: `python -m web_service --school --env-file deploy/school.test.env`.
- 서버 팩토리: `web_service.server:create_test_server`.
- 설정과 테스트 순서: [docs/TEST_SERVER.md](docs/TEST_SERVER.md).

기존 `web_service.school:create_school_app`은 이전의 짧은 세션·Oracle 조회 연결부다. 지속 연동·직접 Teams 수집·변경 검토·브라우저 알림을 테스트할 때는 위 새 시작점을 사용한다. 실제 학교 계정과 공용 테스트 서버의 설정·실연동 검증은 별도로 필요하다.

## 개발 환경과 코드 검사

저장소 루트의 `pyproject.toml`에서 Ruff와 pytest 설정을 관리합니다.
실행용 `requirements*.txt`는 기존 역할을 유지하며, 개발 도구는 별도로 설치합니다.
아래 명령은 프로젝트 가상환경에서 실행합니다.

```powershell
python -m pip install -r requirements-web.txt -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

자동으로 정리하려면 `python -m ruff check --fix .` 다음
`python -m ruff format .`을 실행하고 변경 내용을 검토합니다.
Ruff 버전은 개발 의존성과 설정에서 `0.17.4`로 고정했습니다.
버전을 올릴 때는 두 설정을 함께 변경하고 검사 결과를 확인합니다.

현재 검사 범위는 `web_service`, `tests_web`, `tools`, `beta`와 루트 Python 스크립트입니다.
별도 운영 중인 Oracle 봇 `app/`은 제외하며, 해당 코드의 검증·정리는 별도로 진행합니다.
pytest fixture를 다른 테스트 모듈에서 가져올 때는 `from module import fixture as fixture`처럼
명시적으로 다시 내보내 자동 정리로 삭제되지 않도록 합니다.

GitHub Actions의 **Python quality checks**가 푸시와 PR에서 린팅·포맷을 검사합니다.
기존 **CalendarSync PostgreSQL beta checks**는 `oracle` 푸시와 `oracle` 대상 PR에서
화면 로직·웹 API·임시 PostgreSQL 18 통합 테스트를 실행합니다. 두 검사 모두 배포를 실행하지 않습니다.
검사 실패를 병합 차단 조건으로 강제하려면 저장소 브랜치 보호 설정에서 해당 검사를 필수로 지정합니다.

실제 PostgreSQL 통합 검사는 테스트용 DB와
`CALENDARSYNC_DISPOSABLE_POSTGRES_DSN`, `CALENDARSYNC_ALLOW_TEMP_SCHEMA=yes`가 필요합니다.
DB가 없으면 해당 검사는 건너뜁니다. 운영 DB 주소를 테스트에 사용하지 않습니다.
외부 연결을 차단하는 로컬 검사는 `python -I -S tools/run_restricted_tests.py`로 실행합니다.
이 실행기는 같은 `pyproject.toml`을 읽되 추가 pytest 옵션을 비우고,
하위 프로세스가 필요한 베타 패키지 실행 검사를 제외합니다.
