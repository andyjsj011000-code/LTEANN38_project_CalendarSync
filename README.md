# CalendarSync

Microsoft Teams 채널 공지사항(텍스트/이미지)을 AI가 분석하여 사용자의 Teams 캘린더에 일정을 자동으로 동기화해 주는 서비스입니다.

## Tech Stack
- **Framework**: FastAPI (Python 3.11+)
- **API**: Microsoft Graph API, OpenAI API (gpt-4o-mini)
- **Authentication**: MSAL (Microsoft Authentication Library)

## 약관 및 방침
- [개인정보 처리방침](./docs/privacy.md)
- [서비스 이용약관](./docs/terms.md)


## 학교 계정 웹서비스

실제 Microsoft 로그인 시작점: `web_service.school:create_school_app`.
구현 범위·보안 경계·설정·배포 전 검증: [docs/SCHOOL_WEB.md](docs/SCHOOL_WEB.md).
이 설정은 기존 Teams 봇과 별도 프로세스이며 자동으로 운영 서버에 배포되지 않습니다.
