# CalendarSync Classroom 연결

## Google 버튼으로 연결

CalendarSync 운영자가 Google Cloud 프로젝트에서 Classroom API를 켜고 Google Auth Platform에 웹 OAuth 클라이언트를 한 번 등록한다. 학생은 Cloud 프로젝트를 만들 필요가 없다. OAuth 동의 화면에는 [Classroom 읽기 범위](https://developers.google.com/workspace/classroom/guides/auth) 중 수업 목록, 공지, 본인 과제 읽기 세 가지만 등록한다. 학교 외부의 프로젝트를 테스트할 때는 External의 테스트 사용자에 대상 학교 Google 계정을 추가한다. 학교에서 외부 앱 접근을 제한하면 [학교 관리자 설정](https://support.google.com/a/answer/7281227)이 우선한다.

1. OAuth 클라이언트의 승인된 리디렉션 URI에 CalendarSync HTTPS 주소 뒤에 `/auth/classroom/google/callback`을 붙여 등록한다. 로컬 테스트에는 실제 사용하는 루프백 주소와 포트를 정확히 등록한다.
2. 아래의 `classroom` 스키마를 적용한 뒤 같은 CLI에서 `--feature classroom-google`로 계획 해시를 확인하고 `--upgrade classroom-google --expected-sha256 <plan_sha256>`를 실행한다.
3. 서버에 `WEB_CLASSROOM_ENABLED=1`, `WEB_GOOGLE_CLASSROOM_CLIENT_ID`, `WEB_GOOGLE_CLASSROOM_CLIENT_SECRET`을 설정한다. 클라이언트 비밀값은 서버의 비밀 환경 변수에 보관한다.
4. 학생이 CalendarSync의 **Google Classroom 연결**을 누르고 학교 Google 계정으로 로그인해 읽기 권한을 승인한다. 첫 수집이 완료되면 공지와 과제가 표시되며 서버에서 약 15분마다 갱신한다. Google 권한이 만료되면 **Google Classroom 다시 연결**이 표시된다.

Google의 External Testing 상태에서는 Classroom 권한의 갱신 토큰이 일반적으로 7일 후 만료된다. 학교에서 계속 사용할 수 있게 공개하기 전에는 [Google의 OAuth 앱 검증 요건](https://developers.google.com/identity/protocols/oauth2/web-server#appverification)을 확인한다. 기존 Apps Script 연결이 있는 계정에서 Google 연결이 활성화되면 Google 데이터를 먼저 보여준다. Google 연결을 끊으면 기존 Apps Script 데이터로 돌아간다.

## Apps Script로 연결

`create_test_server` 또는 `create_hosted_app`으로 실행하는 Microsoft 로그인 서버에서 사용할 수 있다. Apps Script가 15분마다 참여한 활성 수업을 읽고 최신 공지와 과제를 각각 최대 12건씩 CalendarSync로 보낸다. Classroom 카드에는 해당 Microsoft 사용자에게 연결된 항목만 표시된다.

### 서버 설정

기존 웹 DB에서 `schema_history_cli`의 `classroom` 기능을 적용한다. SQLite는 `sqlite-server` 프로필과 `--database`를, PostgreSQL은 `postgres-server` 프로필과 `--schema`, `--tenant`, `DATABASE_URL`을 사용한다. 스키마 기준선이 기록되지 않은 경우 [스키마 이력 절차](SCHEMA_HISTORY.md)에 따라 먼저 등록한다.

1. `python -m web_service.schema_history_cli --profile postgres-server --feature classroom`로 변경 계획과 `plan_sha256`을 확인한다. SQLite에서는 `--profile sqlite-server`를 사용한다.
2. 동일한 프로필로 `--upgrade classroom --expected-sha256 <plan_sha256>`과 DB 대상 인자를 지정해 적용한다.
3. `classroom-courses` 기능도 같은 절차로 계획을 확인하고 적용한다. 수업별 표시 설정은 계정마다 저장한다.
3. 서버에 `WEB_CLASSROOM_ENABLED=1`을 설정한다. 실제 스크립트 전송에는 Google 서버에서 접속할 수 있는 CalendarSync HTTPS 주소가 필요하다.

### 학생 연결

1. 학교 Microsoft 계정으로 CalendarSync에 로그인하고 **오늘 할 일 → Classroom 연결**을 연다.
2. 학교 Google 계정으로 만든 본인 전용 Apps Script 프로젝트에 화면에서 제공하는 `Code.gs`와 `appsscript.json`을 넣는다. 매니페스트가 보이지 않으면 **프로젝트 설정 → 편집기에 appsscript.json 매니페스트 파일 표시**를 선택한다.
3. **프로젝트 설정 → 스크립트 속성**에 화면에 표시된 `CALENDARSYNC_ORIGIN`과 `CALENDARSYNC_TOKEN`을 저장한다. 연결 키는 본인 전용 프로젝트에만 저장한다.
4. 편집기에서 `startClassroomSync`를 선택하고 **실행 → 권한 검토**를 누른다. 학교 Google 계정으로 수업 목록·공지·본인 과제 읽기, 외부 요청, 시간 기반 트리거 실행을 승인한다. `CalendarSync sync completed.`가 표시되면 CalendarSync를 새로고침한다.

**연결 해제**는 서버의 목록과 전송 키를 삭제한다. 다음 전송에서 서버가 `401`을 반환하면 스크립트가 해당 자동 실행 트리거를 제거한다. Apps Script의 **트리거** 메뉴에서도 직접 제거할 수 있다. 키를 잃어버렸다면 CalendarSync에서 연결을 해제한 뒤 새 키를 발급받는다.
