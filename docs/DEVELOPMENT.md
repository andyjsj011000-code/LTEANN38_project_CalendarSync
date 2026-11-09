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
DB 변경 이력 도구, 설정 로더와 서버 객체 조립, 내부 데이터 타입, Protocol, 공통 업무 규칙, 공지 수집과 토큰 갱신 코드,
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

## 설정 읽기와 서버 객체 조립

`config.py`는 환경값을 설정 객체로 읽습니다. `Settings.from_env(environ)`와
`Settings.from_server_env(environ)`에 문자열 매핑을 주입할 수 있으며, 생략하면
기존처럼 프로세스 환경을 사용합니다. 빈 매핑은 빈 환경으로 취급합니다.

| 설정 경로 | 세션 기본값 | 유휴 기본값 |
| --- | --- | --- |
| 직접 만든 `Settings` | 12시간 | 15분 |
| 기본 웹 `Settings.from_env()` | 1시간 | 15분 |
| 학교 테스트 서버 `Settings.from_server_env()` | 8시간 | 30분 |

기존 `WEB_SESSION_SECONDS`와 `WEB_IDLE_SECONDS`가 지정되면 같은 검증을 거쳐
기본값을 대체합니다. 이미 주입한 `Settings`는 다시 환경값으로 덮어쓰지 않습니다.

`DirectSourceConfig`, `OracleConfig`, `PushConfig`는 기존 환경변수 이름과 기본값을
보존합니다. 기존 `*_from_env()` 함수도 유지하며, `*_from_config()`는 설정 객체로
어댑터를 만듭니다. `oracle_source.OracleConfig` 가져오기 경로도 유지합니다.
이미 주입한 수집기·발송기가 있으면 사용하지 않는 해당 설정을 강제로 읽거나 검증하지 않습니다.

`composition.compose_server()`는 환경을 한 번 복사하고 저장소 선택 가드, 서버 설정,
DB·Identity·Graph·수집기·발송기를 순서대로 구성합니다. 기존 주입 대역은 그대로
유지합니다. 이 단계에서는 DB를 초기화하거나 외부 서비스를 호출하지 않습니다.
`server.create_test_server(..., environ=...)`는 이 결과로 기존 통합 서비스와 라우트,
선택 기능과 worker를 연결합니다. DB 초기화와 worker 시작 시점은 기존과 같습니다.

PostgreSQL은 여전히 명시적으로 주입한 저장소만 허용하고 테넌트·미리보기·알림 런타임
조건을 검사합니다. 별도 `hosted.py`의 설정·스키마 초기화 선택 조건과 베타·미리보기
진입점은 유지합니다. 객체 조립 단계의 기존 동적 주입은 남아 있으며, 각 서비스 경계의
Protocol과 별도 타입 계약은 계속 검사합니다.


## DB 변경 이력

프로필별 고정 SQL, 읽기 전용 확인, 기존 DB의 관찰 기준선과 선택 기능 업그레이드는
[DB 변경 이력 안내](SCHEMA_HISTORY.md)를 따릅니다. 새 도구는 앱 시작과 분리되어 있으며,
기존 스키마 버전·bootstrap·선택 기능 설치 경로는 유지합니다.


## 구조 개선 이식의 완료 범위

원본 `lteann-proj`의 전체 구성에 대한 대응과 제외 근거는
[LTEANN 구조 개선 이식 범위](LTEANN_PORT_REVIEW.md)에 정리한다.
`sync_ports`는 스냅샷·선택 추적·공유 저장소와 공급자를 역할별로 검사한다.
인증/공유 일정/서버 연결/검토/리마인더/푸시 라우트는 각각의 `*_routes`에서 등록한다.
`reminder_rules`는 기존 시간 계산만 담당하며 저장소 조회와 발송 재검증은 수행하지 않는다.
