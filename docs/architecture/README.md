# 코드 구조와 주요 동작 흐름

[통합 페이지](oracle-guide.html)는 코드 구조도와 세 가지 업무 흐름을 담은 독립 HTML입니다.
저장소를 내려받은 뒤 이 파일을 Chrome 또는 Edge에서 열면 됩니다. GitHub의 파일 화면은
HTML 소스를 표시하므로, 브라우저에서 보려면 저장한 파일을 직접 엽니다. 읽는 데 서버나
Archify 설치, 외부 네트워크 연결은 필요하지 않습니다.

기준 코드는 `oracle`의 PR #9 병합 커밋
[`5108a33`](https://github.com/andyjsj011000-code/LTEANN38_project_CalendarSync/commit/5108a33b61c436a31a56322299d773715c72acd1)입니다.
실시간으로 저장소를 조회하는 문서가 아니라 해당 코드의 동작을 정리한 스냅샷입니다.
구조도는 역할별 분류이며 실제 디렉터리 포함 관계나 실행 순서를 뜻하지 않습니다.

## 페이지 읽기

- 넓은 화면은 2열, 좁은 화면은 1열입니다. 글씨를 줄이지 않고 줄바꿈합니다.
- 굵은 실선은 기본 진행, 점선은 분기·합류입니다. 조건과 도착 단계 번호를 함께 읽습니다.
- A/B 연결 표식을 클릭하거나 Enter로 실행하면 대응 단계로 이동하고 해당 분기만 강조합니다.
  도착지의 같은 표식으로 돌아올 수 있습니다. 상단 **강조 해제** 또는 Esc로 해제합니다.
- 각 그림의 글씨 크기 조절과 **자세히 보기**를 사용할 수 있습니다.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| [oracle-guide.html](oracle-guide.html) | 완성된 통합 페이지. 단독으로 복사해서 열 수 있습니다. |
| [source/oracle-guide.archify.md](source/oracle-guide.archify.md) | 패널 설명, 배치, 코드 근거를 담은 Archify 문서 원본 |
| [source/structure.json](source/structure.json) | 역할별 코드 구조도 원본 |
| [source/requests.json](source/requests.json) | 로그인·일정 요청과 권한 경계 원본 |
| [source/sync.json](source/sync.json) | 권한 확인·수집 예약·실제 수집 흐름 원본 |
| [source/notifications.json](source/notifications.json) | 알림 후보 계산·발송 직전 재검증 흐름 원본 |
| `renderer/` | 한국어 글꼴, 반응형 2열 배치, 연결 표식과 탐색 동작을 재현하는 렌더링 코드 |
| [build.mjs](build.mjs) | 원본에서 HTML을 다시 만드는 진입점 |
| [checks/browser-check.mjs](checks/browser-check.mjs) | 완성 HTML을 실제 브라우저에서 검증하는 진입점 |

각 JSON의 `sources`는 기준 커밋의 코드 파일과 줄 번호를 기록합니다.
그림의 의미를 바꿀 때에는 해당 코드 근거와 Markdown 설명도 함께 확인합니다.

## 수정과 재생성

완성 HTML을 직접 고치기보다 `source/`와 필요한 `renderer/` 파일을 수정하고 재생성합니다.
보기만 할 때와 달리 재생성에는 **Node.js 24 이상**과 **Archify 3.2** 설치 경로가 필요합니다.
Archify 자체는 이 저장소에 복사하지 않습니다. 저장소 루트에서 실행합니다.

```powershell
$env:ARCHIFY_HOME = 'Archify가 설치된 디렉터리의 절대 경로'
node docs/architecture/build.mjs
```

환경변수 대신 설치 디렉터리를 첫 인자로 전달할 수도 있습니다.

```powershell
node docs/architecture/build.mjs 'Archify가 설치된 디렉터리의 절대 경로'
```

재생성기는 Archify의 원본·코드 근거 검증과 HTML 검사를 실행합니다. 브라우저 동작 검사는
아래 명령으로 별도 실행합니다. 결과 JSON과 화면 캡처는 `.checks/`에 저장하며 Git에 넣지 않습니다.
`renderer/font-loader.mjs`는 이 생성 프로세스에만 적용되며 설치된 Archify 파일을 수정하지 않습니다.

## 브라우저 검증

Node.js 24 이상과 Chrome 또는 Edge가 필요합니다. Windows의 기본 설치 경로와 일부
일반적인 Chrome 경로를 찾으며, 다른 경로는 `ARCHIFY_BROWSER`로 지정합니다.
브라우저 검사는 헤드리스 브라우저를 사용하고 로컬 임시 프로필만 만들고 정리합니다.

```powershell
node docs/architecture/checks/browser-check.mjs

# 기본 경로에서 브라우저를 찾지 못할 때
$env:ARCHIFY_BROWSER = '브라우저 실행 파일의 절대 경로'
node docs/architecture/checks/browser-check.mjs
```

검사 항목은 글씨 크기·잘림·가로 넘침, 2열/1열 전환, 원본의 모든 단계·관계 보존,
2열에서 화살표 교차·선분 겹침, A/B 양방향 이동과 단일 분기 강조, 클릭·Enter·터치 입력,
확대·화면 변경 후 선택 유지, 강조 해제, 모션 감소 설정과 브라우저 실행 오류입니다.

문서 전용 변경에서는 저장소의 **Python quality checks**가 실행됩니다.
**CalendarSync PostgreSQL beta checks**는 기존 경로 필터에 따라 실행 여부가 결정되며,
이 문서 추가 때문에 애플리케이션 코드나 CI 실행 조건을 바꾸지 않습니다.
