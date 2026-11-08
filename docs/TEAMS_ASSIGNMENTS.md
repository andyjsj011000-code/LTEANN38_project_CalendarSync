# Teams 과제 연동

과제 탭은 채널 공지에서 추출한 일정과 별도로, 로그인한 학생에게 배정된 Teams 과제를 읽는다.
예정, 기한 경과, 완료, 전체 분류를 제공한다. 최근 60일의 마감과 앞으로의 과제, 기한 없는 과제를 가져오며 날짜가 있는 과제는 캘린더와 오늘 마감 과제에도 표시한다.
완료 여부는 학생 본인의 Teams 제출 상태를 사용한다. 웹에서 제출하거나 제출 상태를 변경하지 않는다.

## 활성화

1. 같은 Microsoft Entra 앱에 Microsoft Graph **위임된 권한** `EduAssignments.ReadBasic`을 추가한다.
2. 학교 테넌트 관리자가 이 권한에 동의한다. 성적 조회 및 쓰기 권한은 요청하지 않는다.
3. Render 베타 서비스에 `WEB_ASSIGNMENTS_ENABLED=1`을 설정하고 이 변경을 배포한다.
4. 로그인 후 과제 탭의 **Teams 과제 연결하기**를 누른다.
   연결은 `/auth/login?assignments=1`을 사용하고 기기 연동 유지 선택을 전달한다.
   기존 로그인은 이전 권한만 요청하므로 관리자 승인 전에도 공지와 개인 할 일을 사용할 수 있다.

새로운 권한을 승인하기 전에는 기존 access/refresh token으로 과제를 조회할 수 없다.
현재 기기에서 한 번 추가 연결한 뒤에는 암호화한 자격 정보에 선택 권한을 보존하며 토큰 갱신 때도 요청한다.

## 데이터와 오류 처리

- `GET /education/me/assignments?$expand=submissions`의 학생별 결과만 사용한다. 다른 사용자의 ID를 요청 매개변수로 받지 않는다.
- 개인 과제는 공용 채널 이벤트 및 AI 분석 캐시에 넣지 않는다.
- 별도 `web_assignment_snapshots` 테이블에 사용자별 암호화 스냅샷을 저장한다. 기존 테이블을 변경하지 않는다.
- 권한 부족, 재로그인 필요, 수집 실패를 빈 목록의 정상 수집과 구분한다.
- 모든 페이지를 읽은 뒤에만 스냅샷을 바꾼다. 잘못된 응답, 제한 초과, 중간 페이지 실패 때 기존 스냅샷을 지우지 않는다.
- 스냅샷은 최대 60초 동안 재사용하며, 재사용할 때도 현재 기기 연결과 학교 구성원 여부를 확인한다. 수동 새로고침은 이 캐시를 건너뛴다.
- 로그아웃 및 기기 해제 중 완료된 요청은 브라우저에 다시 나타나지 않는다.
- 안내 본문이 목록 API에서 제공되지 않으면 Teams 원문 링크를 제공한다. 과목 이름은 가입 팀 목록에서 확인하며 조회 실패 때는 Teams 수업으로 표시한다.

## 검증

`tests_web/test_assignments.py`와 `tests_web/test_assignments_ui.cjs`에서
한국 시각 마감, 본인 제출 상태, 페이지 이동, 권한 부족, 사용자별 격리,
암호화 저장, 동시 로그아웃, 추가 권한 선택 및 독립 화면 로딩을 검증한다.
실제 학교 테넌트의 과제 조회는 관리자 권한 승인 후 확인해야 한다.

## Microsoft 공식 참고

- https://learn.microsoft.com/en-us/graph/api/educationuser-list-assignments?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/resources/educationsubmission?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/permissions-reference#eduassignmentsreadbasic

