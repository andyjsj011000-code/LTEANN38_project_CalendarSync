# CalendarSync 코드와 흐름을 한 페이지에서 읽기 {#oracle-guide}

웹 요청은 권한 경계를 통과하고, 업무 흐름이 처리를 조율하며, 저장소가 데이터를 검증하고 기록합니다.

@answer scope="oracle PR #9 병합본 · 핵심 웹 경로 · 선택 기능은 설명에 구분"
@meta locale=ko style=clean repo_url=https://github.com/andyjsj011000-code/LTEANN38_project_CalendarSync.git repo_revision=5108a33b61c436a31a56322299d773715c72acd1
@source modules title="최종 코드의 역할 구분" path=docs/LTEANN_PORT_REVIEW.md lines=39:54
@source request title="요청 인증과 CSRF 검사" path=web_service/request_dependencies.py lines=24:44
@source sync-code title="권한 확인과 수집 예약" path=web_service/server.py lines=188:234
@source push-code title="발송 직전 재검증" path=web_service/push.py lines=207:275

## 01 · 코드는 어떤 역할로 나뉘나요? {#structure-panel span=12}

역할별 모듈 지도입니다. 실제 디렉터리의 포함 관계나 실행 순서를 뜻하지 않습니다.[](source:modules)

@diagram tree id=structure source=structure.json title="최종 코드 구조"

## 02 · 로그인과 일정 요청은 어떻게 처리하나요? {#requests-panel span=12}

개인 일정은 Teams 권한 확인과 독립적으로 처리합니다. 편집 요청에는 CSRF와 버전 검사가 적용됩니다.[](source:request)

@diagram workflow id=requests source=requests.json title="로그인·일정 요청"

## 03 · 권한 확인과 실제 수집은 언제 실행하나요? {#sync-panel span=12}

공유 모드에서는 권한을 확인한 뒤 수집 요청을 기록하고 `queued`를 먼저 응답합니다. 실제 수집은 응답 후 작업이나 정기 worker가 이어갑니다.[](source:sync-code)

@diagram workflow id=sync source=sync.json title="동기화"

## 04 · 알림은 무엇을 다시 확인하나요? {#notifications-panel span=12}

후보를 계산한 뒤에도 장치, 일정 상태, 현재 구독을 발송 직전에 다시 확인합니다.[](source:push-code)

@diagram workflow id=notifications source=notifications.json title="알림 발송"
