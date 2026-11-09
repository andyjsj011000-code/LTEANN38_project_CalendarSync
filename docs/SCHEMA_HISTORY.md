# DB 변경 이력과 명시적 업그레이드

`web_schema_version=1`을 유지하면서 별도 `web_schema_history`로 기본 스키마와 선택 기능의
출처를 기록합니다. 앱 시작 함수, 기존 bootstrap과 선택 기능 설치기는 변경하지 않았습니다.
이 관리 도구는 별도로 실행하며 웹 소스 ZIP에 포함합니다. 간편 베타 ZIP은 그대로 유지합니다.

## 프로필과 고정 정의

| 프로필 | 기존 DB 종류 | 명시적 업그레이드 |
| --- | --- | --- |
| `sqlite-foundation` | 기본 웹 SQLite | 없음 |
| `sqlite-school` | 학교 로그인 웹 SQLite | 없음 |
| `sqlite-server` | 학교 테스트 서버 SQLite | `planner` |
| `sqlite-beta` | 합성 데이터 베타 SQLite | 없음 |
| `postgres-server` | 전용 스키마의 PostgreSQL 서버 | `shared-ingestion`, `planner` |

`web_service/schema_revisions/`는 현재 정의의 고정 SQL 사본입니다. `schema_catalog.py`가
ID·부모 관계·SHA-256을 고정하며 수정된 기존 파일을 거부합니다. 줄바꿈은 LF로 정규화합니다.
기본 정의는 비교용 메모리 DB에서만 사용하고 기존 대상 DB에 재실행하지 않습니다.
제공하는 업그레이드는 기존 기능의 추가 테이블·인덱스를 그대로 생성하는 작업입니다.

새 DB 변경은 새 SQL 파일·revision·부모 관계와 해당 업그레이드 처리·검증을 함께 추가합니다.
이미 공개한 SQL·체크섬은 수정하지 않습니다. 기존 설치기와 고정 정의가 같은지도 테스트합니다.

## 확인과 기준선 등록

저장소 루트에서 실행합니다. 기본 동작과 `--plan`은 DB 연결 없이 계획만 출력합니다.

```powershell
python -m web_service.schema_history_cli --profile sqlite-server --plan
python -m web_service.schema_history_cli --profile sqlite-server --check --database '기존-서버.sqlite3'
python -m web_service.schema_history_cli --profile sqlite-server --baseline --database '기존-서버.sqlite3' --expected-sha256 '검토한-기준선-계획-해시'
```

쓰기 작업에는 검토한 계획의 `plan_sha256`을 전달합니다. 해시는 프로필·작업·번들 SQL과
이력 형식을 고정합니다. 접속 대상은 별도 파일/스키마와 접속 설정으로 지정합니다.
기준선은 구조 확인 후 이력 테이블과 미기록 revision만 추가하며 기존 데이터·버전 행을
수정하지 않습니다. `kind=observed`, `recorded_at`은 **현재 확인하여 기록한 시각**입니다.
과거의 DDL 실행 증거나 실행 시각이 아니며, 재실행해도 기존 행·종류·시각은 유지합니다.

`--check`는 SQLite를 `mode=ro`·`query_only`, PostgreSQL을 읽기 전용 트랜잭션으로 확인합니다.
없던 SQLite 파일을 만들지 않습니다. 학생 내용·키·DSN·접속 암호를 출력하지 않습니다.
종료 코드는 완전한 추적 상태 `0`, 거부/실패 `1`, 구조는 확인했지만 미기록 이력이 있는
`--check`는 `2`입니다.

검사 범위는 열·타입·NULL 허용·PK/UNIQUE·FK와 명명 인덱스입니다. CHECK/DEFAULT 표현식의
모든 동치성이나 데이터 내용의 무결성을 증명하는 DB 감사는 아닙니다. 알 수 없는 버전·revision,
체크섬 차이·빠진 부모·다른 관리 대상 구조는 자동으로 고치지 않고 거부합니다.

## 선택 기능 업그레이드

먼저 기준선이 모두 추적된 상태여야 합니다. 업그레이드 계획의 해시는 기준선 계획과 다릅니다.

```powershell
python -m web_service.schema_history_cli --profile sqlite-server --plan --feature planner
python -m web_service.schema_history_cli --profile sqlite-server --upgrade planner --database '기존-서버.sqlite3' --expected-sha256 '검토한-planner-계획-해시'
```

DDL·구조 검증·`kind=applied` 이력이 하나의 트랜잭션으로 완료되며 실패하면 함께 롤백됩니다.
SQLite는 `BEGIN IMMEDIATE`, PostgreSQL은 transaction advisory lock과 짧은 관리 테이블 잠금으로
동시 실행을 조정합니다. 기존 두 선택 기능 설치기의 advisory lock도 사용합니다.
이미 완료된 작업은 다시 실행하지 않습니다.

기존 앱 설정으로 선택 기능을 먼저 설치했다면 `--check`에 미기록 revision이 나타납니다.
검토 후 `--baseline`으로 `observed`로 등록합니다. 이미 있던 기능을 새 실행으로 기록하지 않습니다.
테이블 설치와 앱의 기능 활성화는 별개이며 기존 `WEB_SHARED_CHANNEL_INGESTION`·
`WEB_PLANNER_ENHANCEMENTS` 선택 조건은 유지합니다.

## PostgreSQL

기존 비공개 `DATABASE_URL`, `WEB_DATABASE_SSLMODE`, `WEB_DATABASE_SSLROOTCERT`를 사용합니다.
DB 작업에는 전용 `--schema`와 기대하는 학교 `--tenant`를 명시합니다.

```powershell
python -m web_service.schema_history_cli --profile postgres-server --plan
python -m web_service.schema_history_cli --profile postgres-server --check --schema calendarsync_web --tenant '학교-테넌트-ID'
python -m web_service.schema_history_cli --profile postgres-server --baseline --schema calendarsync_web --tenant '학교-테넌트-ID' --expected-sha256 '검토한-기준선-계획-해시'
python -m web_service.schema_history_cli --profile postgres-server --plan --feature shared-ingestion
```

`public`·시스템 스키마, 선택한 namespace 불일치와 다른 학교의 사용자·채널·공유 수집 행을
거부합니다. 스키마나 빈 DB는 새로 만들지 않으며 신규 PostgreSQL 스키마 생성은 기존
`postgres_bootstrap` 절차를 사용합니다.

## 검증

```powershell
python -m pytest tests_web/test_schema_history.py -q
python -m pytest tests_web/test_schema_history_postgres_integration.py -q
```

실제 PostgreSQL 테스트는 기존 `CALENDARSYNC_DISPOSABLE_POSTGRES_DSN`과
`CALENDARSYNC_ALLOW_TEMP_SCHEMA=yes` 조건을 사용하고 fixture가 만든 임의 테스트 스키마만
정리합니다. CI는 새 PostgreSQL 모듈도 별도 재실행하고 JUnit으로 건너뛰기 없음을 확인합니다.
