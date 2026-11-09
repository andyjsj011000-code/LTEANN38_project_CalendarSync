# 개발 검사와 테스트 대역

저장소 루트에서 실행합니다. 개발 도구는 고정 버전이며 런타임 의존성과 분리합니다.

```powershell
python -m pip install -r requirements-web.txt -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m basedpyright
python -m pytest tests_web --ignore=tests_web/test_beta_package.py -q
```

Windows 제한 환경에서는 `python -I -S tools/run_restricted_tests.py`로 기존 웹 테스트를
실행할 수 있습니다. 베타 ZIP 실행기 테스트는 별도로 `python -m pytest
tests_web/test_beta_package.py -q`로 실행합니다.

## 타입 검사 범위

`pyproject.toml`의 `tool.basedpyright.include`가 검사 경계를 정의합니다. 현재 범위는
내부 데이터 타입, Protocol, 공통 업무 규칙, 공지 수집과 토큰 갱신 코드,
공통 요청 의존성, 개인 일정·설정 타입과 라우트, 요청 모델과 PostgreSQL 개인 일정·설정 구현,
공통 대역 및 실제 어댑터의 타입 계약입니다. 나머지 모듈은 이 경계에서 가져오는
반환 타입과 메서드 규약을 확인하며, 명시적 타입을 정리할 때 검사 범위를 넓힙니다.

`tests_web/typing_contracts.py`는 실제 Graph·AI·토큰·SQLite·PostgreSQL 구현과
테스트 대역을 같은 Protocol에 대입합니다. 런타임 메서드 존재 검사와 달리 인자 이름,
선택 인자와 반환 타입이 바뀌어도 CI에서 발견합니다. 타입 오류를 전체적으로 숨기거나
현재 오류 수를 허용하는 기준선은 사용하지 않습니다.

## 공통 대역과 계약 테스트

- `tests_web/support/fakes.py`: 수집용 Graph·AI·캐시·공유 수집 잠금·토큰 갱신 대역과
  합성 데이터. 관찰한 호출과 지정한 실패를 각 인스턴스에 보관합니다.
- `tests_web/support/postgres.py`: 기존 환경 가드를 사용하는 임시 PostgreSQL fixture.
  기존 테스트 파일의 fixture 가져오기도 유지합니다.
- `tests_web/test_adapter_contracts.py`: 대역과 실제 구현에 같은 동작 검증을 적용합니다.
  Graph/OpenAI는 MockTransport를 사용하고, 저장소는 메모리·SQLite·임시 PostgreSQL을
  비교합니다. 수집 순서, 캐시 교체·취소, 잠금 소유권·만료, 갱신 실패 후 재시도를 검사합니다.

```powershell
python -m pytest tests_web/test_adapter_contracts.py -q
```

PostgreSQL 계약은 기존 `CALENDARSYNC_DISPOSABLE_POSTGRES_DSN`과
`CALENDARSYNC_ALLOW_TEMP_SCHEMA=yes` 조건이 있을 때만 실행합니다. 각 테스트의
임의 스키마 안에서만 데이터를 만들고 정리합니다. CI는 PostgreSQL 18에서 계약을
재실행하고 JUnit 결과로 건너뛴 항목이 없는지 확인합니다.

특정 테스트만의 실패 주입은 공통 대역의 작은 하위 클래스로 표현합니다. 대역은
DB 트랜잭션이나 동시 잠금의 대체물이 아니므로 기존 PostgreSQL 통합 테스트도 유지합니다.

## 웹 라우트와 요청 의존성

`api.py`는 앱 구성과 인증·공유 일정 라우트를 연결합니다. 개인 일정은
`personal_routes.py`, 사용자 설정과 채널별 설정은 `settings_routes.py`에서
각 애플리케이션에 `install_*_routes()`로 직접 등록합니다. 기존 planner 모듈과
같은 방식이며 경로 조회와 FastAPI 의존성 재정의도 유지합니다. 저장소가 소유권 확인,
버전 충돌 처리와 쓰기 트랜잭션을 계속 담당합니다.

`request_dependencies.py`는 세션·학교 테넌트·Origin·CSRF 검사와 통합 서버의
요청 검사를 순서대로 수행하는 의존성을 만듭니다. 앱마다 한 번 만들고
`app.state.require_session`에도 같은 함수를 보관하므로 학교·서버·베타 및
선택 기능 라우트에서 동일한 인증 경계를 사용합니다. 요청별 데이터를 모듈 전역에
보관하지 않습니다.

개인 일정 읽기는 Teams 권한 확인에 종속되지 않으며, 기존 세션·유휴 시간 검사와
요청 제한은 유지됩니다. 초안·휴지통 라우트는 기존 planner 선택 기능이 켜졌을 때만
등록합니다. 새 웹 모듈은 두 ZIP 빌더의 명시적 목록에도 포함합니다.

## 개인 일정·설정 타입 경계

`personal_types.py`는 기존 딕셔너리·JSON 형식을 유지합니다. 요청 모델은 검증 후
`TaskInput.to_payload()`, `TaskState.to_changes()`, `Preferences.to_values()`로
명시적으로 변환합니다. 수정 버전은 일정 본문과 분리해 전달하고, 상태 변경의
`None`은 기존처럼 저장소가 건너뜁니다. 날짜 문자열, 종료일 보정과 선택 날짜 규칙,
사용자 정의 일정 종류는 기존 입력 검증을 따릅니다.

완전한 요청 값과 저장된 읽기 값을 구분합니다. 오래된 부분 저장 설정은
`PreferenceSnapshot`, 선택 필드가 없는 개인 일정은 `PersonalTask`의 선택 필드로
표현합니다. 읽을 때 기본값을 채우거나 필드를 제거하지 않습니다. 저장소의 JSON
복호화 지점에서만 타입을 지정하며, 응답 모델을 추가해 기존 응답을 재검증하지 않습니다.

`personal_ports.py`의 개인 일정·설정 저장소 Protocol은 라우트에서 사용하는 인자와
반환값을 정의합니다. 휴지통은 별도 선택 기능 계약입니다. `typing_contracts.py`는
기본·베타·학교용 SQLite와 PostgreSQL 구현이 이 계약을 만족하는지 검사합니다.
`storage.py` 등 큰 기존 저장소 전체는 아직 검사 대상이 아니며, 공개 메서드의 타입과
별도 계약으로 이 경계를 확인합니다. SQL, 스키마, 잠금과 쓰기 트랜잭션은 유지합니다.
