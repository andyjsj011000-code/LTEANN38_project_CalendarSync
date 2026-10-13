import asyncio
import copy
import html
import json
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import msal
import requests
import logging
from Channel_Export import get_graph_access_token
from temp_http_client import safe_http_request
import httpx
from schedule_dedup import METADATA_PROPERTY, describe, exact_match, find_duplicate
load_dotenv()

TENANT_ID = os.environ.get("TENANT_ID")
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

TIME_ZONE = "Korea Standard Time"
KST = timezone(timedelta(hours=9))
logger = logging.getLogger("ScheduleBot")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')


# 8일 이상 장기 일정 백엔드 분할 함수
def process_long_term_schedules(notice_data: dict) -> list[dict]:
    """GPT 추출 결과에서 8일 이상 장기 일정을 [#시작], [#종료] 2개 객체로 분할하여 반환합니다."""
    # 만약 입력이 GPT의 전체 wrapper 객체일 경우(schedules 배열 포함) 처리
    raw_schedules = (
        notice_data.get("schedules")
        if isinstance(notice_data, dict) and "schedules" in notice_data
        else (
            [notice_data] if isinstance(notice_data, dict) else notice_data
        )
    )

    processed_list = []

    for item in raw_schedules:
        raw_start = item.get("start_time") or item.get("start_date")
        raw_end = item.get("end_time") or item.get("end_date")
        title = item.get("title", "")

        if not raw_start or not raw_end:
            processed_list.append(item)
            continue

        try:
            # ISO 문자열에서 날짜 파싱 (시간 및 타임존 오프셋 정제)
            clean_start = raw_start.replace("Z", "").split("+")[0].strip()
            clean_end = raw_end.replace("Z", "").split("+")[0].strip()

            start_dt = datetime.fromisoformat(clean_start)
            end_dt = datetime.fromisoformat(clean_end)

            # 날짜 차이 계산 (일 단위)
            day_diff = (end_dt.date() - start_dt.date()).days

            # 8일 이상 지속되는 장기 기간 일정인 경우 2개로 분할
            if day_diff >= 8:
                # 1) 시작일 당일 객체
                start_item = copy.deepcopy(item)
                start_item["title"] = f"{title} [#시작]"
                start_item["start_time"] = (
                    f"{start_dt.strftime('%Y-%m-%d')}T00:00:00"
                )
                start_item["end_time"] = (
                    f"{start_dt.strftime('%Y-%m-%d')}T23:59:00"
                )
                processed_list.append(start_item)

                # 2) 종료일 당일 객체
                end_item = copy.deepcopy(item)
                end_item["title"] = f"{title} [#종료]"
                end_item["start_time"] = (
                    f"{end_dt.strftime('%Y-%m-%d')}T00:00:00"
                )
                end_item["end_time"] = (
                    f"{end_dt.strftime('%Y-%m-%d')}T23:59:00"
                )
                processed_list.append(end_item)

            else:
                # 7일 이하 단기 일정 또는 당일/마감 일정은 그대로 유지
                processed_list.append(item)

        except Exception as e:
            # 날짜 파싱 실패 시 원본 그대로 보존
            processed_list.append(item)

    return processed_list


def make_event_body(notice: dict):
    # 1. 필드 매핑
    title = notice.get("title") or "팀즈 자동 등록 일정"
    summary = notice.get("summary") or notice.get("description") or ""
    source = notice.get("source") or ""
    web_url = notice.get("web_url") or ""

    # 2. Location 방어 처리
    raw_location = notice.get("location")
    if isinstance(raw_location, str):
        location_name = raw_location
    elif isinstance(raw_location, dict):
        location_name = (
            raw_location.get("raw") or raw_location.get("name") or ""
        )
    else:
        location_name = ""

    # 3. HTML 본문 구성
    content = f"<h2>{html.escape(title)}</h2>"
    if summary:
        content += f"<p>{html.escape(summary)}</p>"
    if location_name:
        content += f"<p><b>장소:</b> {html.escape(location_name)}</p>"
    if source:
        content += f"<p><b>출처:</b> {html.escape(source)}</p>"

    # 4. 날짜 및 시간 처리
    raw_start = notice.get("start_time") or notice.get("start_date")
    raw_end = notice.get("end_time") or notice.get("end_date")

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    def parse_datetime_str(dt_str: str, is_end: bool = False) -> str:
        if not dt_str or not isinstance(dt_str, str):
            return ""

        clean_str = dt_str.replace("Z", "").split("+")[0].strip()

        if len(clean_str) == 10 and clean_str.count("-") == 2:
            return (
                f"{clean_str}T23:59:00" if is_end else f"{clean_str}T00:00:00"
            )

        if "T" in clean_str:
            date_part, time_part = clean_str.split("T")
            time_part = time_part[:8]

            if is_end and time_part.startswith("23:59"):
                time_part = "23:59:00"

            return f"{date_part}T{time_part}"

        return clean_str

    start_datetime = (
        parse_datetime_str(raw_start, is_end=False) or f"{today_str}T00:00:00"
    )
    end_datetime = (
        parse_datetime_str(raw_end, is_end=True)
        or f"{start_datetime.split('T')[0]}T23:59:00"
    )

    if end_datetime < start_datetime:
        end_datetime = start_datetime

    # 5. web url 확인
    if web_url:
        '''
        logger.info(f"[Calendar Service] web_url 정상 수신: {web_url}")
        '''
        # 본문 하단에 원본 메시지 링크 HTML 생성
        content += f'<br><a href="{web_url}">일정 펼치고 여길 클릭해서 게시물로 이동</a><br>'
        
    # 6. MS Graph API Payload 생성
    event = {
        "subject": title,
        "body": {
            "contentType": "HTML",
            "content": content,
        },
        "location": {
            "displayName": location_name,
        },
        "isAllDay": False,
        "start": {
            "dateTime": start_datetime,
            "timeZone": TIME_ZONE,
        },
        "end": {
            "dateTime": end_datetime,
            "timeZone": TIME_ZONE,
        },
        #calendarsync 식별자
        "singleValueExtendedProperties": [
            {
                "id": "String {fbd4ec16-7f0f-46d2-a9db-f32258a48607} Name CalendarSyncApp",
                "value": "CalendarSync_2026"
            }
        ]
    }

    event["singleValueExtendedProperties"].append({
        "id": METADATA_PROPERTY,
        "value": json.dumps({
            "version": 1,
            "summary": summary,
            "event_kind": notice.get("event_kind", "unknown"),
            "time_precision": notice.get("time_precision", "unknown"),
            "identity_context": notice.get("identity_context"),
            "audience": notice.get("audience"),
            "target_grades": notice.get("target_grades", []),
        }, ensure_ascii=False),
    })
    return event

def is_duplicate_event(new_event: dict, existing_events: list) -> bool:
    """Only clear exact matches are decided without semantic comparison."""
    new = describe(new_event)
    return any(exact_match(new, describe(event)) for event in existing_events
               if not event.get("isCancelled"))


async def get_existing_events_for_day(user_email: str, headers: dict, target_datetime_str: str, http_client: httpx.AsyncClient) -> list:
    """
    등록하려는 날짜 당일의 기존 일정들을 API로 미리 끌어옵니다.
    """
    date_part = target_datetime_str.split("T")[0]
    day_start = datetime.fromisoformat(date_part).replace(tzinfo=KST)
    params = {
        "startDateTime": day_start.isoformat(),
        "endDateTime": (day_start + timedelta(days=1)).isoformat(),
        "$expand": f"singleValueExtendedProperties($filter=id eq '{METADATA_PROPERTY}')",
    }
    query_headers = {**headers, "Prefer": 'outlook.timezone="Korea Standard Time"'}
    url = f"https://graph.microsoft.com/v1.0/users/{user_email}/calendarView"
    events = []
    while url:
        res = await safe_http_request(
            http_client, "GET", url, headers=query_headers, params=params, timeout=5
        )
        # 조회 실패를 "일정 없음"으로 취급하면 중복 생성되므로 호출자에게 전달합니다.
        res.raise_for_status()
        data = res.json()
        events.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        params = None
    return events


class CalendarRegistrationBatch:
    """사용자 한 명의 동기화에서 채널 간 중복 검사와 등록을 직렬화합니다."""

    def __init__(self, user_email: str, http_client: httpx.AsyncClient, access_token: str = None):
        self.user_email = user_email
        self.http_client = http_client
        self.access_token = access_token
        self._lock = asyncio.Lock()
        self._created_events = []
        self._day_events = {}
        self.had_failures = False

    async def add_notice(self, notice_data: dict) -> int:
        async with self._lock:
            previous_count = len(self._created_events)
            try:
                succeeded = await add_notice_to_calendar(
                    self.user_email, notice_data, self.http_client, self.access_token,
                    known_events=self._created_events,
                    day_events=self._day_events,
                )
            except Exception:
                self.had_failures = True
                raise
            if not succeeded:
                self.had_failures = True
            return len(self._created_events) - previous_count

async def add_notice_to_calendar(
    user_email: str, notice_data: dict, http_client: httpx.AsyncClient,
    access_token: str = None, *, known_events: list | None = None,
    day_events: dict[str, list] | None = None,
) -> bool:
    """
    GPT AI에서 추출한 단일 notice 또는 schedules 포함 dict를 받아 장기 일정 분할 후 지정 유저의 MS 캘린더에 등록합니다.
    """
    if not access_token:
        access_token = await get_graph_access_token()
    if access_token is None:
        '''
        logger.error("[Calendar] access_token 없음으로 등록 취소")
        '''
        return False

    # 1. 후처리 함수를 거쳐 8일 이상 일정을 분할한 일정 리스트 획득
    target_notices = process_long_term_schedules(notice_data)

    success_all = True
    if known_events is None:
        known_events = []
    if day_events is None:
        day_events = {}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # 2. 각 일정을 캘린더에 등록
    for notice in target_notices:
        try:
            event_body = make_event_body(notice)
        except Exception as e:
            '''
            logger.error(f"[make_event_body 에러] {e}")
            '''
            success_all = False
            continue
        # 1. 해당 날짜의 기존 일정 미리 끌어오기 (중복 검사용)
        start_datetime = event_body.get("start", {}).get("dateTime", "")
        # 같은 동기화에서 성공한 일정은 Graph 조회에 아직 안 보여도 중복을 막습니다.
        if await find_duplicate(event_body, known_events):
            continue

        day_key = start_datetime.split("T")[0]
        if day_key not in day_events:
            try:
                day_events[day_key] = await get_existing_events_for_day(
                    user_email, headers, start_datetime, http_client=http_client
                )
            except Exception as e:
                logger.error(f"[calendarView 조회 실패] {e}", exc_info=True)
                success_all = False
                continue
        existing_events = day_events[day_key]

        # 2. 중복 검사 실행
        if await find_duplicate(event_body, existing_events):
            logger.info(f"[Calendar 스킵 - 중복 일정 감지] {event_body.get('subject')}")
            continue  # 중복이면 생성 요청을 보내지 않고 다음으로 건너뜁니다.
        
        url = f"https://graph.microsoft.com/v1.0/users/{user_email}/events"

        response = await safe_http_request(http_client, "POST", url, headers=headers, json=event_body)

        if response.status_code in (200, 201):
            # This overlay is checked before the per-day Graph snapshot.
            known_events.append(event_body)
            '''
            logger.info(f"[Calendar API 성공]")
            '''
        else:
            logger.error(
                f"[Calendar API 실패 - HTTP {response.status_code}]"
                f" {response.text}"
            )
            success_all = False

    return success_all



if __name__ == "__main__":
    user_email = os.environ.get("USER_MAIL")
    # 테스트 케이스: 8일 이상 지속되는 장기 공지
    long_notice = {
        "title": "[테스트] 장기 프로젝트",
        "start_time": "2026-07-11T00:00:00Z",
        "end_time": "2026-08-15T23:59:00Z",
        "summary": "장기 프로젝트가 진행됩니다.",
        "location": None,
        "is_for_all_grades": False,
        "target_grades": [1],
        "confidence": 1.0,
    }
    # 실행 시 [#시작], [#종료] 2개의 객체로 자동 split되어 MS 캘린더에 등록됨
    add_notice_to_calendar(user_email, long_notice)
