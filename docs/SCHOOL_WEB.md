# Microsoft 학교 계정 웹 연결

현재 fork의 oracle 브랜치에서 사용하는 별도 웹 프로세스다. 기존 app/ 봇·수집 스케줄러와 Oracle 운영 테이블을 변경하거나 자동 실행하지 않는다. web_service.api:create_app은 기반 API, web_service.beta:create_beta_app은 예시 베타이며 **실제 로그인용 시작점은 web_service.school:create_school_app**이다.

## 구현된 흐름

학교 Entra 인증 코드 흐름(MSAL의 state·nonce·PKCE 검증) → 서버의 tenant·issuer·audience·oid·만료 검사 → delegated Graph /me에서 동일 oid와 Member 확인 → 서버 암호화 토큰과 세션 발급 → 학생 본인의 Graph 채널 메시지 읽기 권한 확인 → 해당 채널만 Oracle에서 조회 → 웹 캘린더와 개인 할 일 표시.

이메일 도메인이나 과거 Teams 봇 설치만으로 권한을 부여하지 않는다. 앱 전체 Graph 토큰은 웹 인증·권한 확인에 사용하지 않는다. Team 목록과 allChannels는 후보 목록이며 각 채널의 messages GET 성공이 있어야 접근을 인정한다. 개인·공유 채널은 이 검사를 통과해야 한다. 다른 tenant의 공유 채널과 joinedTeams에서 발견되지 않는 직접 공유 채널은 현재 제외될 수 있다. 제외된 채널을 앱 권한으로 강제 조회하지 않는다.

## Entra 설정

1. 학교 tenant의 단일 조직용 웹 앱을 등록하거나 적절한 기존 웹 앱을 사용한다. 기존 봇과 자격 증명을 분리한 별도 웹 앱을 권장한다.
2. Web 플랫폼 Redirect URI를 정확히 `https://실제호스트/auth/callback`으로 등록한다. SPA·implicit grant를 사용하지 않는다. client secret은 서버 환경에서만 설정한다.
3. Microsoft Graph **Delegated** 권한 `User.Read`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All`을 설정하고 학교 정책에 따라 관리자 동의를 완료한다. 로그인만 허용하는 앱과 Graph 읽기 권한은 다르다. 이번 버전은 Calendars.ReadWrite·채팅 쓰기·앱 전체 권한을 요청하지 않는다.
4. 소규모 베타는 Enterprise application의 사용자 할당을 요구하고 승인된 테스트 그룹에만 배정한다. MFA·기기 조건은 학교 Entra Conditional Access 정책에서 적용한다. 이 코드가 학교의 MFA 정책을 자동 설정하지는 않는다.
5. `deploy/school.env.example`의 WEB_ENTRA_*와 WEB_ORIGIN을 서버에 설정한다. secret 만료·회전 담당자를 지정한다. 실제 비밀 값은 채팅이나 Git으로 전달하지 않는다.

## Oracle 연결과 저장

- 웹 전용 SQLite DB는 개인 할 일·세션·암호화 캐시용이다. 기존 봇의 Oracle DB를 교체하거나 마이그레이션하지 않는다. 이 구성은 단일 웹 인스턴스의 소규모 베타용이며 여러 서버로 확장하기 전에 저장 계층을 별도 설계한다.
- 별도 Oracle 로그인에 기존 `master_calendars`의 SELECT만 부여하고, 읽기 전용 view/synonym으로 그 이름을 노출한다. 운영 봇 계정과 DDL/DML 권한을 웹에 주지 않는다.
- WEB_ORACLE_DATA_KEY는 기존 암호화된 title/description을 읽기 위한 기존 키다. WEB_DATA_KEY는 웹 전용 새 키다. 둘을 혼동하지 않는다. 키가 맞지 않으면 원문을 그대로 표시하지 않고 가져오기를 실패시킨다.
- 연결은 SET TRANSACTION READ ONLY로 시작하고 바인드 변수를 사용한다. 현재 권한을 확인한 채널만 읽으며 Oracle 테이블을 수정하지 않는다. 90일 전부터 365일 후까지, 채널당 최대 1000개 일정을 가져온다.
- 표시 주체가 학교 소속인지 Graph에서 확인한다. 권한 증명은 설정값 기준 최대 60초만 유지하며, 확인 실패·철회·만료 후에는 Teams 일정을 반환하지 않는다. 개인 할 일은 유지한다. 이미 화면에 표시된 정보까지 원격으로 회수할 수는 없으므로 새로고침/화면 재진입 시 서버에서 재확인한다.
- 신규 수집기를 만들지 않았으므로 **기존 봇 수집기에 등록되지 않은 채널은 권한이 있어도 일정이 비어 있을 수 있다**. 수집 범위 확대는 별도 작업이다. 기존 수집기의 긴 기간 분할·학년 필터·채널 간 중복·변경 후보 처리는 아직 해결하지 않는다. 따라서 이 연결을 전체 요구사항 완료로 해석하지 않는다.
- 사라진 원본 일정은 캐시에서 취소 표시로 유지한다. 사용자 선택형 변경 후보와 차이 비교는 후속 기능이다.

## 보안 경계

- HTTPS와 정확한 Host 검증. 운영 쿠키는 __Host- 접두사, Secure·HttpOnly·SameSite=Lax, path=/, Domain 없음.
- 토큰은 세션별 서버 암호화 저장. offline_access·갱신 토큰·브라우저 토큰 저장 없음. 최대 1시간 토큰, 기본 세션 1시간, 기본 무활동 15분. 브라우저의 15분 타이머는 보조이며 서버가 실제 만료를 강제한다.
- 쓰기 요청은 CSRF 토큰과 같은 origin 검사. 로그인 콜백은 일회용 서버 흐름 검사. 모든 기기 로그아웃과 세션 고정 방어.
- 학생별 개인 데이터 및 채널 권한 확인. 동시 조회 중 로그아웃한 세션은 새 권한을 발급받을 수 없다.
- CSP·프레임 삽입 차단·HSTS·no-store·no-referrer·MIME sniffing 방지. 캘린더의 동적 grid 때문에 style-src에만 unsafe-inline을 허용하며 inline script는 허용하지 않는다. 사용자 텍스트는 HTML escape 후 표시하고 원문 링크는 Teams HTTPS 주소만 허용한다.
- 64 KiB 요청 제한, 로그인/IP·전체 로그인·사용자별 요청 제한, Graph 목적지·다음 페이지 제한과 응답 크기·시간 제한. 프록시가 X-Forwarded-For를 **덮어쓰고**, uvicorn은 loopback 프록시만 신뢰해야 한다.
- 입력 오류는 제출한 내용을 반사하지 않는다. 서버/API 오류에 토큰·Oracle 오류를 노출하지 않는다. API 문서는 운영 시작점에서 비활성화한다.
- 보안 기록은 원문 없는 가명 이벤트로 7일 보관한다. 세션·토큰 삭제와 개인정보·백업 삭제는 다르다. 운영자는 사용자 삭제·보관 기간·복원 절차를 확정해야 한다.

## 배포

Python 3.12+의 **별도 가상환경**에 `python -m pip install -r requirements-school.txt`로 설치한다. 실제 서버 설치 후 `python -m pip check`와 `pip-audit -r requirements-school.txt`를 실행하고 해결되지 않은 취약점이 있으면 공개하지 않는다. 이 개발 환경은 외부 패키지 DNS 접근이 막혀 MSAL·oracledb 설치 및 온라인 취약점 검사를 완료하지 못했다.

웹 프로세스 환경에 예제의 값을 설정하고 실행한다. 예제 파일은 자동으로 읽지 않는다.

```
python -m uvicorn web_service.school:create_school_app --factory --host 127.0.0.1 --port 8080 --workers 1 --no-access-log
```

개발용 loopback에서만 `WEB_ORIGIN=http://localhost:8080`, `WEB_ALLOW_LOCAL_LOGIN=1`을 명시할 수 있다. 운영에서는 이 예외를 설정하지 않는다. HTTPS 밖으로 8080 포트를 노출하지 않는다.

Ubuntu 배포 예제는 deploy/calendarsync-web.service와 deploy/nginx-school.conf.example에 있다. 실제 도메인·인증서 경로로 교체하고 nginx -t로 검사한다. 코드 디렉터리는 읽기 전용, /var/lib/calendarsync는 서비스 계정만 접근, 환경 파일 0600, wallet은 읽기 전용으로 둔다. OAuth code·쿠키·본문을 프록시/오류 수집 서비스에서 수집하지 않는다. DB와 웹 키의 복원을 검증하고 학교 운영팀의 개인정보 안내를 확정한 뒤 소수 학교 계정으로 시험한다.

## 검증 범위

`python -m pytest tests_web -q` 및 `node tests_web/test_beta_calendar.cjs`, `node tests_web/test_beta_ui.cjs`, `node tests_web/test_school_ui.cjs`.

기존 회귀 검사와 위조 인증·타 학생 접근·권한 철회·실패 시 접근 차단·만료·전체 로그아웃·CSRF·Host·입력 제한·Graph SSRF·Oracle 읽기 전용 회귀 검사를 포함한다. 제한된 Windows 호스트에서는 소켓 없는 ASGI 테스트 실행기를 사용했다. 테스트 대역은 학교의 실제 Entra·Graph·Oracle 접속 성공, 브라우저 렌더링 또는 학교의 보안 승인을 증명하지 않는다.

## 실제 계정 공개 전 남은 검증

서버 비밀값 설정과 Entra 동의 → 전용 Oracle SELECT 계정 연결 → HTTPS 배포 → 허용 학생·미할당 학생·다른 tenant·게스트·비공개 채널·권한 철회 테스트 → 두 학생의 데이터 분리와 휴대폰 사용·로그아웃/만료 확인 → 의존성 감사·백업 복원·학교 운영 안내 확인.

실제 학교 계정이나 운영 서버에는 이 개발 작업에서 접속하지 않았다. 알림·외부 캘린더 기록은 구현하지 않았으며 기존 봇과 이중 기록을 시작하지 않는다.

공식 자료: https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens , https://learn.microsoft.com/en-us/graph/api/team-list-allchannels?view=graph-rest-1.0 , https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0 , https://learn.microsoft.com/en-us/graph/api/user-get?view=graph-rest-1.0
