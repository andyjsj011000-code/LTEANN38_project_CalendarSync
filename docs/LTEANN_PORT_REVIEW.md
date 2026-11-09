# lteann-proj 구조 개선 이식 범위 확정

비교 기준은 `oracle`의 PR #8 병합 커밋 `625da00f1a4e6977c84b22f10d187da04630290d`와
사용자가 제공한 `lteann-proj-main.zip`이다. ZIP SHA-256은
`bb0b69daf2dab9304951e07a07509307ec9c08a22cbc9ba9da3942a20050eae1`이다.
원본 ZIP의 모든 파일을 기존 추출본과 바이트 단위로 대조했다. 원본의 별도 최신 원격
브랜치를 비교한 결과는 아니다. 문서 속 제안은 비교 자료이며 실행 지시로 사용하지 않았다.

## 결론과 완료 범위

기존 기능과 저장 데이터를 유지하는 **원본의 구조 개선 이식**은 아래 대응표의 범위로
확정한다. 이번 변경은 남아 있던 세 경계인 동기화 계약 타입, 리마인더 순수 계산,
남은 웹 라우트/SQL 책임 분리를 마무리한다. 이 작업 후 다른 정리를 원본에서 빠뜨린
추가 필수 이식으로 계속 제시하지 않는다. 향후 버그 수정, 새 기능, 성능·운영 개선은
새 요구와 근거로 판단하는 별도 작업이다. 모든 종류의 개선 가능성이 없다는 뜻은 아니다.

## 원본 전체 구성의 대응과 판단

| 원본 파일/영역 | 현재 대응과 판단 |
| --- | --- |
| `domain.py`: 일정·기간·개인 상태·표시 규칙 | `collection_types`, `personal_types`, `calendar_rules`, `source_updates`로 반영. 기존 JSON과 숨김 복원·변경 검토 규칙을 유지한다. dataclass 전면 교체는 하지 않는다. |
| `domain.py`: reminder_times/due_reminders | 이번에 `reminder_rules`로 계산 책임을 분리. 원본의 시작일·00:00 기준은 이식하지 않고 현행 종료일·23:59 기준을 유지한다. |
| `ports.py`, `microsoft.py`의 TokenApp 규약 | 기존 `ports`·`personal_ports`와 이번 `sync_ports`의 역할별 계약 및 정적 구현 대입으로 반영. 선택 기능은 기존 플래그/능력 검사로 제어한다. |
| `service.py`: 인증·연결·권한 | `identity`, `connections`, `connection_snapshot`, `ServerIntegration`, `request_dependencies`에 해당 역할이 있다. 장치 연결·짧은 권한 수명·세션/CSRF 검사를 원본의 간소한 모델로 교체하지 않는다. |
| `service.py`: 수집과 조정 | PR #8의 `sync_workflow`·`shared_collection`·`sync_results`·`sync_diagnostics`로 분리됨. 원본의 단일 프로세스 수집 루프보다 기존 lease·진행 추적·부분 재시도·권한 재검증을 유지하는 것이 중요하다. |
| `service.py`: 달력·개인 일정·설정 | 공통 순수 조회 규칙과 개인/설정 라우트·타입·저장소 계약으로 반영됨. 이미 공통화된 조회 코드를 새 Service 하나로 다시 모으지 않는다. |
| `adapters/microsoft.py` | Graph·MSAL 주입과 합성 전송 테스트는 이미 존재. 원본은 답글 첫 페이지·순차 탐색 등 간소한 경로가 있어 현행 완전성·재시도·바이트/시간 제한을 대체하지 않는다. |
| `adapters/openai.py` | 기존 `AIExtractor`의 구조화 응답 검증·이전 결과 입력·주입 클라이언트로 대응. 거절을 빈 정상 결과로 취급하는 동작은 이식하지 않는다. |
| `adapters/repo.py`, `adapters/schema.py` | DB/업무 규칙 분리와 암호화·소유권·버전 충돌 계약은 기존 구현에 반영됨. SQLite/PG와 기존 스키마를 유지하며 SQLAlchemy로 전환하지 않는다. |
| `migrations/env.py`, `migrations/versions/*`, `script.py.mako`, `alembic.ini` | PR #7의 고정 SQL·체크섬·명시적 schema history로 목적을 반영. 원본의 앱 시작 시 자동 upgrade는 가져오지 않는다. |
| `web/deps.py`, `web/schemas.py` | 요청 의존성과 개인/설정 입력 타입은 이미 분리됨. 입력 검증과 HTTP 오류 변환은 웹 경계에 유지한다. |
| `web/routes.py`, `web/__init__.py` | 이번 `auth_routes`, `shared_routes`, `server_routes`, `review_routes`, `reminder_routes`, `push_routes`로 나머지를 분리. 기존 `install_*_routes` 방식과 API 계약을 유지한다. |
| `web/security.py` | 현행 `SecurityBoundary`·Origin/CSRF·응답 헤더가 이미 담당. 원본의 더 단순한 미들웨어와 예외 문자열 로그로 교체하지 않는다. |
| `app.py`, `config.py` | PR #6의 환경 읽기·composition 분리로 반영됨. 현행 명시적 실행 모드·worker·선택 기능을 원본 daemon collector로 바꾸지 않는다. |
| `adapters/fakes.py`, `tests/conftest.py`, `tests/test_*.py` | 공통 대역·정적 계약·SQLite/PG 계약 테스트와 기존 회귀 검증으로 반영. 새 대역을 중복 생성하거나 기존 통합 검증을 축소하지 않는다. |
| `pyproject.toml`, `.github/workflows/ci.yml` | Ruff·basedpyright·pytest 설정과 CI가 이미 있음. 타입 검사 범위는 역할 경계를 따라 확대한다. Python 3.12 이상·uv로 전면 전환할 필요는 없다. |
| `uv.lock`, `.python-version`, `.vscode/*`, `.zed/*`, `.gitignore`, `README.md`, `__init__.py` | 도구·편집기·패키지 메타데이터. 기존 requirements/실행기/소스 ZIP과 무관한 교체는 제외한다. 빈 초기화 파일이나 설명 문서를 별도 개선 기능으로 세지 않는다. |

## 이번 코드 구조

- `api.py`: 기본 앱·예외 처리·정적 파일과 공통 라우트 조립.
- `server.py`: 서버 구성·요청 권한 경계·worker와 서버 라우트 조립.
- `*_routes.py`: 기능별 HTTP 입력·응답. 개인/설정/planner의 기존 설치 방식과 같다.
- `sync_workflow.py`, `shared_collection.py`: 동기화 순서와 수집 시도 수명.
- `sync_ports.py`: 권한 Graph·원본 읽기/수집·스냅샷·선택 추적/공유 저장소 계약.
- `calendar_rules.py`, `source_updates.py`, `reminder_rules.py`: 저장소·HTTP 없이 실행하는 규칙.
- 기존 SQLite/PG 저장소: SQL·암호화·소유권·버전·트랜잭션. SQLite override SQL도 여기서 수행.

## 보존 조건과 검증

동기화 능력 검사, 저장소 호출 순서, 주입 객체의 실행 시점 참조, 기존 응답 JSON을 유지한다.
리마인더의 종료일·23:59·10분 유효 구간, 아침 알림 그룹 분리, 검토 중 제외, 후보 순서와
중복, 날짜 범위 넘침 처리, 발송 직전 재검증과 중복 방지/lease를 유지한다.
기존 오류 응답·상태 코드·쿠키·CSRF·테넌트·권한 만료·개인 일정 독립 접근도 유지한다.

기존 전체 회귀·배포 ZIP·브라우저 검사를 사용하고, 새 순수 함수에는 시간 경계 검증을
추가한다. 네 앱(기본/학교/서버/베타)의 OpenAPI도 분리 전후 대조한다. 라우트 모듈이
추가된 베타 ZIP의 파일 수만 23개에서 25개로 갱신하며 기존 실행 검증은 유지한다.
PostgreSQL 검증은 CI의 임시 PostgreSQL 18에서 건너뛴 항목 없이 수행한다.
