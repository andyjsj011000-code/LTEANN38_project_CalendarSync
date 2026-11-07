# Render 베타 배포 설정 초안

이 문서는 검토용입니다. 실제 Render 서비스·DB를 변경하거나 배포하지 않았습니다.
현재 실행 결과와 미완료 항목은 `BETA_PREPARATION_STATUS_KO.md`를 함께 확인합니다.

최신 검증은 **541개 통과·실제 PostgreSQL 통합 24개 통과·건너뜀 없음**입니다.
새 DB 공간 생성·기존 공간 보호·실패 시 전체 취소·재시작 시 기존 데이터 유지도 검사했습니다.
https://github.com/andyjsj011000-code/LTEANN38_project_CalendarSync/actions/runs/42001139649

## 현재 화면에서 필요한 입력

서비스 생성 폼에 무료 요금제·Singapore·`oracle`·빌드/실행 명령·자동 배포 Off를
작성했고 암호화 키·알림 키·기존 앱의 Client ID도 준비했습니다. 아래 네 값은
해당 서비스의 Environment Variables 값 칸에 직접 입력해야 합니다. 채팅에는 보내지 않습니다.

| 변수 | 가져오는 곳 |
| --- | --- |
| `DATABASE_URL` | `calendarsync-beta-test` DB의 Connect → Internal → Database URL |
| `WEB_ENTRA_TENANT_ID` | 학교 앱 등록 → 개요 → 디렉터리(테넌트) ID |
| `WEB_ENTRA_CLIENT_SECRET` | 기존 앱 담당자가 보관한 클라이언트 암호의 **값**. 암호 ID나 Microsoft 계정 비밀번호가 아닙니다. |
| `WEB_OPENAI_API_KEY` | 베타 공지 분석에 사용할 OpenAI API 키 |

이 값들은 실제로 확인하지 못했으므로 비어 있습니다. 값을 입력한 뒤에도 실제
배포·학교 로그인·권한 동의·공지 수집 검증은 다음 단계로 남아 있습니다.

GitHub 임시 PostgreSQL 18 검증은 완료됐습니다. 전체 웹 테스트 504개와 실제 DB 통합
검사 20개가 통과했고 건너뛴 검사는 없습니다. 실제 학교 계정·Graph·AI·외부 알림과
Render의 TLS·기존 DB 상태까지 검증했다는 뜻은 아닙니다.
검증: https://github.com/andyjsj011000-code/LTEANN38_project_CalendarSync/actions/runs/41995178273

## 준비한 설정

새 웹서비스 이름은 `calendarsync-beta`, 브랜치는 개인 fork의 `oracle`, 지역은
이미 만든 DB와 같은 Singapore를 기준으로 했습니다. 예시는 무료 웹서비스 하나만
정의하며 DB·도메인·유료 리소스를 새로 만들지 않습니다. 자동 배포는 끈 상태입니다.
기존 Oracle 데몬, systemd 파일, 기존 Render 서비스에는 적용하지 않습니다.

`deploy/render-beta.yaml.example`은 비활성 예시 파일입니다. 저장소에 올려도 이 파일
자체가 Blueprint 배포를 시작하지 않습니다. 실제 서비스 생성은 별도 작업입니다.

| 항목 | 값 |
| --- | --- |
| Build Command | `pip install -r requirements-postgres-experimental.txt` |
| Start Command | `python -m uvicorn web_service.hosted:create_hosted_app --factory --host 0.0.0.0 --port $PORT --workers 1 --no-access-log` |
| Health Check | `/health` |
| 수집 방식 | 기존 5분 반복 수집을 기준으로 한 direct 모드 |
| 자동 배포 | Off |

## 환경변수

`deploy/render-beta.env.example`은 항목 이름만 담았습니다. 실제 비밀값을 파일·채팅·GitHub에
넣지 않고 새 서비스의 Environment 화면에 입력해야 합니다. `WEB_DATA_KEY`와 알림 키는
재배포 때 새로 만들지 않고 유지해야 기존 암호화 데이터와 기기 연결을 사용할 수 있습니다.

- `DATABASE_URL`: 새 서비스와 DB가 같은 지역일 때 Render DB의 Internal Database URL.
- `WEB_ORIGIN`: 새 웹서비스에 실제로 부여된 `https://…onrender.com` 주소. 끝에 `/`를 넣지 않습니다.
- `WEB_ENTRA_*`: 사용하기로 한 학교 앱 등록의 테넌트·앱 ID·유효한 비밀값.
- `WEB_OPENAI_API_KEY`: 베타 공지 분석에 사용할 키.
- Microsoft 앱의 웹 리디렉션 URI에는 실제 `WEB_ORIGIN` 뒤에 `/auth/callback`을 붙입니다.
- 브라우저 알림을 사용할 때는 `WEB_VAPID_PRIVATE_KEY`·`WEB_VAPID_SUBJECT`도 별도로 설정합니다.

Render의 내부 연결은 자체 서명 인증서를 사용하므로 예시에 TLS `require`를 명시했습니다.
코드는 TLS 검증 실패 뒤 암호화 설정을 자동으로 낮춰 재접속하지 않습니다. 외부 접속에는
인증서·CA 구성을 검토하고 기본 `verify-full`을 사용하거나 접속 정책을 명시적으로 결정합니다.
이번 작업에서는 네트워크·접속 허용 범위·인증서 설정을 실제로 변경하지 않았습니다.

## DB를 변경하기 전에

1. GitHub의 임시 PostgreSQL 18 검사가 실제로 실행되고 통과했는지 확인합니다. 테스트는
   Render DB나 학교 계정 비밀값을 사용하지 않습니다.
2. 사용자가 `calendarsync-beta-test`를 새 베타 전용 DB로 확인했습니다. 전용
   `calendarsync_web` 공간에 테이블 35개·인덱스 4개를 한 트랜잭션으로 처음 생성하는
   `postgres_bootstrap` 절차를 준비했습니다. `--plan`은 접속 없이 계획을 출력하고
   `--apply`는 이미 존재하는 공간이면 변경 없이 거절합니다.
3. `web_service.hosted`의 `--check-config`는 설정 형식만 확인하고 DB에 접속하지 않습니다.
   `--check-database`는 이미 준비된 스키마를 조회·검증하며 생성·변경·마이그레이션을 하지 않습니다.
4. 이후 학교 테스트 계정으로 로그인, 공지 수집, 브라우저 알림을 실제로 확인해야 합니다.

기존 기본 서버의 Render 실행 차단은 유지합니다. 새 `web_service.hosted:create_hosted_app`은
별도 베타 진입점입니다. `WEB_BOOTSTRAP_NEW_SCHEMA=1`을 명시한 경우에만 공간이 없으면
처음 생성합니다. 이미 있으면 읽기 검증만 하고 테이블을 자동 수정·초기화하지 않습니다.
이 설정의 기본값은 0이며, 예시의 1은 이번에 확인된 새 베타 DB를 위한 선택입니다.
스키마가 맞지 않으면 실행이 실패하며 SQLite로 자동 전환하지 않습니다.

`WEB_ORIGIN`을 비워두면 Render의 `RENDER_EXTERNAL_URL`을 사용합니다. 서비스가 생성된
뒤 실제 주소에 `/auth/callback`을 붙여 학교 앱의 웹 리디렉션 URI로 추가해야 합니다.
공식 설명: https://render.com/docs/environment-variables

## 선택형 일정 편의 기능 운영

`WEB_PLANNER_ENHANCEMENTS`의 기본값은 `0`입니다. PostgreSQL 회귀 검사와 건너뛰기 없는
통합 검사가 통과한 코드를 배포할 때 `1`로 설정하면 초안 저장·휴지통·수집 상태 기능을
활성화합니다. `/api/service`의 `planner_enhancements`에서 적용 여부를 확인할 수 있습니다.

- 활성화한 앱의 시작 과정에서 전용 schema, 기본 schema 버전 `1`, 학교 tenant를 확인하고
  트랜잭션 잠금 아래 고정 sidecar 세 개(`web_task_drafts`, `web_task_trash`,
  `web_ingestion_status`)와 인덱스만 생성합니다. 재시작은 기존 행을 보존하며 기존 테이블을
  변경·초기화하지 않습니다. `WEB_BOOTSTRAP_NEW_SCHEMA`의 새 schema 생성과는 별개입니다.
- `--check-config`는 DB에 연결하지 않고, `--check-database`는 플래그가 `1`이어도 조회만
  합니다. 아직 sidecar가 없는 DB에서는 후자가 실패하며, 점검 명령이 대신 설치하지 않습니다.
- 초안 본문은 마지막 저장부터 7일 후 만료됩니다. 정리 작업은 본문을 비우고 버전을 올리며
  오래된 탭의 저장을 차단하는 버전 기록은 남깁니다. 휴지통은 삭제 시점부터 7일간 복구할 수
  있고, 만료한 항목은 조회·복구에서 제외한 뒤 백그라운드 작업이 삭제합니다.
- 복구는 원래 일정 ID를 사용하며 삭제 전 버전보다 `1` 높은 버전으로 되돌립니다. 저장된
  알림 설정을 복원하고 기존 알림 발송 기록은 보존하여 이미 발송한 알림의 중복을 막습니다.
- 수집의 마지막 성공 시각은 최종 권한 확인과 일정 저장이 함께 커밋됐을 때만 갱신됩니다.
  일부 수집이나 실패는 성공으로 표시하지 않습니다. 플래그를 다시 `0`으로 바꾸어도
  저장된 sidecar와 기존 일정 데이터는 삭제하지 않습니다.

## 참고 문서

- https://render.com/docs/blueprint-spec
- https://render.com/docs/postgresql-creating-connecting
- https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers
