# CalendarSync 웹 기반 구현 0.1

기준: fork 저장소 `andyjsj011000-code/LTEANN38_project_CalendarSync`의 `oracle` 브랜치, 기존 코드 커밋 `b5ae89894ec227b0ad4b19e8d5f969480c2b8254`.

이번 변경은 `web_service/`, `tests_web/`, `requirements-web.txt`, 이 설명서를 추가한다. 기존 `app/`의 수집·OCR·AI 분석·Teams 캘린더 기록·알림 작업과 기존 설정 파일은 수정하지 않는다. 다른 브랜치나 원본 저장소도 변경하지 않는다.

## 이번에 구현한 범위

- 기존 봇 프로세스와 분리된 FastAPI 웹 API. 웹 모듈을 불러와도 Oracle 접속이나 기존 스케줄러를 시작하지 않는다.
- 학교 Microsoft 로그인용 authorization-code 흐름 연결부. MSAL에 state·nonce·PKCE 검사를 맡기고, 서버에서 학교 tenant·앱 audience·issuer·사용자 object ID·만료를 추가 검사한다. 이메일 주소는 표시용이며 권한 기준으로 사용하지 않는다.
- 로그인 흐름은 서버에 암호화해 보관하고 브라우저에는 임의 참조값만 둔다. 세션 쿠키도 임의 값이며 서버 DB에는 해시로 저장한다. 로그아웃·만료·사용 중지 시 세션을 거부한다.
- 학생별 개인 할 일 생성·조회·수정·삭제, 완료·숨김·복원. 변경 버전이 오래된 다른 기기의 쓰기는 409로 거부한다.
- 캘린더 조회와 오늘 목록 분리. 기간 일정은 전체 기간을 반환하며, 개인 할 일의 오늘 노출은 매일/선택 날짜/마지막 날을 따른다. 선택 날짜에는 마지막 날을 항상 포함한다. 지난 미완료는 오늘로 이동시키지 않는다.
- 공통 일정 접근과 채널 표시 선호 분리. 접근 가능 확인이 만료되거나 삭제되면 해당 채널 일정은 조회할 수 없다. 개인 숨김·완료는 다른 학생에게 전파되지 않는다.
- Teams 캘린더 기록 이력에 의존하지 않는 조회. 새 사용자의 외부 기록은 기본 OFF다. 아직 연결되지 않은 채팅·외부 기록 기능은 켤 수 없으며 API가 409로 설명한다.
- 개인 텍스트·프로필·추후 수집할 원문 등의 암호화 저장. 공통 일정/원문 버전/개인 상태/외부 이벤트 연결을 별도 테이블로 준비했다.

## 현재 상태와 제한

이 변경은 공유 문서 P1의 **개발용 기반**이다. 실제 학생이 사용하는 서비스나 P1 전체 완료를 뜻하지 않는다.

| 항목 | 현재 상태 |
| --- | --- |
| 개인별 저장·조회 API | 로컬 SQLite 통합 검증 완료 |
| 학교 로그인 | 연결 코드와 테스트용 공급자 검증 완료. 실제 학교 앱 설정 및 MSAL 실연동 미검증 |
| Oracle 운영 DB | 미연결. SQLite는 개발 어댑터이며 Oracle 마이그레이션을 대체하지 않음 |
| 실제 Teams 채널·공지 | 미연결. 테스트 데이터만 검증, 런타임은 예시 계정·채널을 자동 생성하지 않음 |
| A형 화면 | 기존 HTML 시제품 별도 유지. 이 API에 연결하는 작업은 다음 단계 |
| 변경 후보·답글·신고·개인 수정 | 후속 구현. 원문 버전·개인 수정 저장 공간만 준비 |
| 푸시·Teams 채팅·캘린더 기록 | 미연결. 실제 전송·외부 일정 변경을 실행하지 않음 |
| 기존 이용자 자동 이관 | 미구현. 아래 이관 원칙을 먼저 적용해야 함 |

새로 로그인한 학생에게 실제 채널을 보여 주려면, 다음 단계에서 해당 학생이 현재 접근 가능한 채널만 검증해 `web_channel_access`를 갱신해야 한다. 기존 팀 소속만으로 모든 비공개·공유 채널 접근을 부여하면 안 된다. 과거 사용자-채널 매핑을 그대로 권한으로 인정하는 가져오기도 구현하지 않았다.

## 실행

Python 3.12 이상을 권장한다. 저장소 루트에서 별도 가상환경에 `requirements-web.txt`를 설치한다. 기존 서버의 `requirements.txt`를 덮어쓰지 않는다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-web.txt
```

처음 한 번 Fernet 키를 생성해 안전한 서버 설정에 저장한다. **기존 DB로 다시 실행할 때도 같은 키가 필요하다.** 새 키를 매번 생성하거나 DB와 함께 보관한 키를 잃으면 암호화된 내용을 읽을 수 없다. 키와 실제 자격 증명을 Git에 올리지 않는다.

```powershell
# 최초 생성 예시. 생성한 값을 보관하고 이후에는 그 값을 설정한다.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:WEB_DATA_KEY = "보관한 키"
$env:WEB_ORIGIN = "http://localhost:8080"
python -m uvicorn web_service.api:create_app --factory --host 127.0.0.1 --port 8080 --no-access-log
```

`/health`와 `/docs`에서 프로세스·API 명세를 확인한다. 아직 화면은 연결되지 않아 `/`는 상태 JSON을 반환한다. 로그인 설정이 없으면 `/auth/login`은 503, 보호된 API는 401을 반환한다. 개발용 사용자 ID 헤더나 우회 로그인 엔드포인트는 없다.

환경 변수 목록은 `web_service/.env.example`에 있다. 예제 파일을 자동으로 읽지 않으므로 프로세스 환경에 명시적으로 설정한다. 운영 origin은 경로·끝 슬래시가 없는 HTTPS 주소로 설정한다. HTTP는 localhost 개발 환경에만 허용한다.

### 실제 Microsoft 로그인 연결 시

학교의 단일 tenant 웹 앱에서 다음을 설정·확인한다.

1. `WEB_ENTRA_TENANT_ID`, `WEB_ENTRA_CLIENT_ID`, `WEB_ENTRA_CLIENT_SECRET` 세 값을 서버 환경에 설정한다. 값은 저장소에 기록하지 않는다.
2. 앱에 **웹** Redirect URI `${WEB_ORIGIN}/auth/callback`을 등록한다. 운영 HTTPS 주소와 일치해야 한다.
3. 실제 학교 계정 로그인, 다른 tenant 거절, 로그아웃·만료를 실제 앱에서 검증한다. 로그인 성공은 채널 읽기·Teams 채팅·캘린더 쓰기 권한 확보를 의미하지 않는다.
4. 프록시에서도 인증 콜백의 query/code·쿠키를 로그에 기록하지 않도록 구성한다. 서버·키·DB의 백업과 복원은 별도 운영 단계에서 검증한다.

이번 실행 환경에서는 MSAL을 설치할 수 없어 어댑터 계약을 테스트 대역으로 검사했다. 실제 MSAL/Entra 로그인 성공을 확인했다고 해석하면 안 된다.

## 프런트엔드 연결 계약

인증 후 `GET /api/me`의 `csrf_token`을 모든 쓰기 요청의 `X-CSRF-Token` 헤더에 보낸다. 브라우저는 같은 origin의 HttpOnly 세션 쿠키를 사용한다. 다른 origin의 쓰기 요청은 거부한다.

| 요청 | 의미 |
| --- | --- |
| `GET /api/me` | 현재 사용자·CSRF 토큰 |
| `GET /api/channels` | 현재 접근 권한이 유효한 채널과 표시 여부 |
| `PUT /api/channels/{id}/preference` | 본인 채널 표시 여부 |
| `GET /api/settings`, `PUT /api/settings` | 본인 설정과 실제 연결된 기능 확인 |
| `POST /api/tasks` | 본인 개인 할 일 생성 |
| `GET`, `PUT`, `DELETE /api/tasks/{id}` | 본인 할 일 조회·수정·삭제 |
| `PATCH /api/tasks/{id}/state` | 완료·숨김·복원 |
| `PATCH /api/events/{id}/state` | 접근 가능한 공통 일정의 본인 완료·숨김 |
| `GET /api/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD` | 전체 기간을 유지한 캘린더 항목 |
| `GET /api/today?day=YYYY-MM-DD` | Teams/개인 두 구역의 해당 날짜 목록, day 생략 시 한국 날짜 |
| `GET /api/hidden?start=...&end=...` | 복원할 수 있는 본인 숨김 항목 |
| `POST /auth/logout` | 현재 세션 폐기 |

`PUT`은 해당 표현 전체를 교체한다. 프런트엔드는 기존 값을 읽어 수정한 전체 설정/할 일을 보낸다. 개인 할 일 수정·상태 변경에는 조회한 `version`을 넣고, 삭제에는 `?version=...`을 사용한다. 409면 최신 값을 다시 읽어 사용자에게 충돌을 알린다. 다른 학생의 항목 또는 접근 불가능한 공통 일정은 404다.

예: 10월 20-24일의 “개발 완료하기”를 선택 날짜 `[10월 20일]`로 만들면 서버는 마지막 날인 24일을 추가한다. 캘린더에는 20-24일 전체 범위를 반환하고, 오늘 목록에는 20·24일만 포함한다. `completed` 값이 바뀌어도 정렬 순서를 바꾸지 않는다. 완료·취소의 알림 제외는 향후 전송 계층에서 이 상태를 이용해 구현해야 한다.

## 기존 서비스와 이관할 때 지킬 원칙

1. 현재 봇의 캘린더 기록 담당을 유지한 상태에서 웹 조회를 먼저 연결한다. 웹 서비스에는 기존 `UserSyncLog`를 근거로 한 자동 쓰기를 시작시키지 않는다.
2. 기존 사용자·공통 일정·외부 이벤트 ID의 읽기 전용 스냅샷과 복원 수단을 준비한다. 운영 DB에 새 스키마를 적용하기 전에 Oracle 어댑터와 마이그레이션을 별도로 작성·검증한다.
3. 기존 사용자 ID(`oid`)와 새 웹 키(`tenant:oid`)를 연결하되, 실제 학교 로그인 및 현재 채널 권한을 다시 확인한다. 기존 봇 미설치 학생도 웹에 로그인할 수 있어야 한다.
4. 기존 외부 이벤트 ID를 인수하면 기록 담당을 `legacy`로 표시해 보존한다. 같은 학생의 같은 일정을 두 서비스가 동시에 쓰지 않도록 단일 담당 전환을 먼저 구현한다. 이 전환은 현재 코드에서 자동 실행되지 않는다.
5. 기록을 끌 때 기존 일정 유지, ON 중 숨김은 제거·복원은 재등록, 취소·개인 수정 충돌 등 확정 규칙을 적용한 후에 쓰기를 켠다. 새 서비스가 기존 이벤트를 새 이벤트로 중복 생성하면 안 된다.

## 테스트와 다음 작업

```text
python -m pytest tests_web -q
```

2026-10-22 로컬 검증: 38개 테스트 통과. 실제 FastAPI ASGI 요청과 SQLite를 사용해 학생 간 조회·변경 차단, CSRF/세션/콜백 재사용, 기간·오늘 노출, 완료 위치, 숨김·필터, 채널 권한 만료, 동시 수정 충돌, 저장 암호화를 검사했다. 이 Windows 실행 환경에서는 이벤트 루프의 로컬 소켓 생성이 막혀, 네트워크 I/O를 금지한 메모리용 이벤트 루프에서 같은 테스트를 실행했다. 실제 HTTP 서버·MSAL·Graph·Oracle·휴대폰 알림을 테스트한 결과는 아니다.

다음 순서: Oracle 웹 저장 어댑터·마이그레이션 → 실제 학교 로그인 검증 → 학생별 실제 채널 접근 확인·공지 읽기 → A형 화면 연결 → 변경 후보와 개인 수정 → 알림·선택적 외부 기록.

공식 참고: [Microsoft MSAL 인증 코드 흐름](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens), [Microsoft 사용자 식별 및 권한 검증](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation), [FastAPI 통합 테스트](https://fastapi.tiangolo.com/tutorial/testing/).
