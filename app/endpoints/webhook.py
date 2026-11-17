# app/endpoints/webhook.py
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from cachetools import TTLCache
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import bot_connector_auth, bot_service, graph_service
from app.models.domain import User
from app.models.master_calendar import UserSyncLog
from app.utils.notification import send_today_notice_to_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhook"])

RECENT_LIFECYCLE_USERS: TTLCache[str, None] = TTLCache(
    maxsize=100, ttl=settings.DUPLICATE_WEBHOOK_DEBOUNCE_SECONDS
)
RECENT_MESSAGE_ACTIVITIES: TTLCache[tuple[str, str, str], None] = TTLCache(
    maxsize=1024, ttl=settings.DUPLICATE_WEBHOOK_DEBOUNCE_SECONDS
)
MESSAGE_RATE_LIMIT = 5
MESSAGE_RATE_WINDOW_SECONDS = 30
RECENT_MESSAGE_REQUESTS: TTLCache[str, deque[float]] = TTLCache(
    maxsize=1024, ttl=MESSAGE_RATE_WINDOW_SECONDS
)


def record_message_request(user_id: str) -> bool:
    """사용자별 최근 30초의 메시지 요청을 세고 허용한 요청만 기록한다."""
    now = RECENT_MESSAGE_REQUESTS.timer()
    requests = RECENT_MESSAGE_REQUESTS.get(user_id)
    if requests is None:
        requests = deque()
    while requests and requests[0] <= now - MESSAGE_RATE_WINDOW_SECONDS:
        requests.popleft()
    if len(requests) >= MESSAGE_RATE_LIMIT:
        return False
    requests.append(now)
    RECENT_MESSAGE_REQUESTS[user_id] = requests
    return True


def calculate_grade_from_email(email: Optional[str], current_year: int) -> Optional[int]:
    """이메일 아이디의 앞 2자리 숫자를 입학 연도로 판단하여 학년을 계산합니다."""
    if not email:
        return None

    username = email.split("@")[0].strip()
    grade_overrides = settings.GRADE_OVERRIDES or {}
    if username in grade_overrides:
        grade = grade_overrides[username]
        return int(grade) if grade is not None else None

    match = re.match(r"^(\d{2})", username)
    if match:
        entry_year_suffix = int(match.group(1))
        entry_year = 2000 + entry_year_suffix
        grade = current_year - entry_year + 1

        if 1 <= grade <= 3:
            return grade
    return None


async def cleanup_user_data(user_id: str, db: Session):
    """유저가 앱을 삭제/차단했을 때 Graph API 일정 완전 삭제 및 DB 유저 잔여 데이터 정제"""
    try:
        # 1. DB에서 유저 조회
        user_stmt = select(User).where(User.id == user_id)
        db_user = db.execute(user_stmt).scalar_one_or_none()

        if not db_user:
            return

        target_graph_user = db_user.email if db_user.email else db_user.id

        # 2. DB SyncLog에서 유효한 outlook_event_id 추출 (None 및 빈 문자열 제거)
        log_stmt = select(UserSyncLog.outlook_event_id).where(
            UserSyncLog.user_id == user_id, UserSyncLog.outlook_event_id.isnot(None)
        )
        raw_event_ids = db.execute(log_stmt).scalars().all()
        valid_event_ids = [e_id for e_id in raw_event_ids if e_id and e_id.strip()]

        # Step A: DB 로그 기반 타겟 삭제 시도
        if valid_event_ids:
            try:
                await graph_service.delete_events_by_ids(
                    user_id=target_graph_user, event_ids=valid_event_ids
                )
            except Exception as graph_err:
                logger.warning(f"ID 기반 Graph API 삭제 중 일부 실패 (Fallback 진행): {graph_err}")

        # Step B: Fallback (보완책) - DB에 없거나 누락된 유령 일정까지 Extended Property로 조회하여 이중 삭제
        try:
            await graph_service.delete_user_synced_events(user_id=target_graph_user)
        except Exception as fallback_err:
            logger.warning(f"Extended Property 기반 Fallback 삭제 실패: {fallback_err}")

        # 3. DB 유저 삭제 (Cascade 설정으로 관련 SyncLog, ChannelMapping 자동 삭제)
        db.delete(db_user)
        db.commit()

        logger.info("[DB 유저 삭제 완료] 데이터 완전 제거")

    except Exception as e:
        db.rollback()
        logger.error(f"유저 삭제/정리 중 에러 발생: {e}", exc_info=True)


async def authenticated_activity(request: Request) -> dict:
    authorization = request.headers.getlist("authorization")
    if len(authorization) != 1:
        raise HTTPException(403, "Missing Bot Connector authorization")
    try:
        activity = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid Teams activity") from None
    if not isinstance(activity, dict):
        raise HTTPException(400, "Invalid Teams activity")
    await bot_connector_auth.authenticate(authorization[0], activity)
    return activity


@router.post("/api/messages")
async def teams_event_webhook(
    background_tasks: BackgroundTasks,
    data: dict = Depends(authenticated_activity),
    db: Session = Depends(get_db),
):
    """검증된 Teams 이벤트를 등록된 사용자와 연결해 처리한다."""
    action = data.get("action")
    activity_type = data.get("type")
    now_utc = datetime.now(timezone.utc)

    from_user = data.get("from") or {}
    user_id = from_user.get("aadObjectId") or from_user.get("id")
    if not isinstance(user_id, str) or not user_id.strip():
        return {"status": "ok", "message": "no_user_id_in_activity"}
    user_conversation = data.get("conversation") or {}
    user_conversation_id = user_conversation.get("id")
    service_url = data.get("serviceUrl")

    if activity_type == "message":
        # 활동 ID가 있을 때만 동일한 Teams 이벤트의 재전송으로 판단한다.
        channel_id = data.get("channelId") or "msteams"
        activity_id = data.get("id")
        identity = (channel_id, user_conversation_id, activity_id)
        activity_key = (
            identity
            if all(isinstance(value, str) and value.strip() for value in identity)
            else None
        )
        if activity_key is not None and activity_key in RECENT_MESSAGE_ACTIVITIES:
            return {"status": "ok", "message": "duplicate_event_ignored"}
        if not record_message_request(user_id):
            return JSONResponse(
                status_code=429,
                content={"status": "error", "message": "message_rate_limited"},
                headers={"Retry-After": str(MESSAGE_RATE_WINDOW_SECONDS)},
            )
        if activity_key is not None:
            RECENT_MESSAGE_ACTIVITIES[activity_key] = None
    else:
        # 설치 시 함께 오는 두 이벤트의 안내 중복은 기존처럼 묶어서 처리한다.
        if user_id in RECENT_LIFECYCLE_USERS:
            return {"status": "ok", "message": "duplicate_event_ignored"}
        RECENT_LIFECYCLE_USERS[user_id] = None

    # Graph 조회가 실패해도 계정과 학년을 미확인 상태로 처리한다.
    user_email = None
    try:
        # graph_service의 유저 정보 조회 메서드 호출 (메서드명은 프로젝트 환경에 맞춰 확인 필요)
        user_info = await graph_service.get_user(user_id)
        if user_info:
            user_email = user_info.get("mail") or user_info.get("userPrincipalName")
    except Exception as ge:
        logger.warning(f"[Graph API] 이메일 조회 실패: {ge}")

    user_grade = calculate_grade_from_email(user_email, now_utc.year)

    try:
        if activity_type == "installationUpdate" and action == "remove":
            # 백그라운드 태스크로 유저 데이터 정리 실행
            goodbye_text = (
                "**CalendarSync 서비스 연동을 해지합니다**\n\n"
                "캘린더에 동기화되있는 일정을 자동으로 삭제하며 "
                "더 이상 알림을 수신하지 않게 됩니다. 안녕히가세요~!"
            )
            background_tasks.add_task(
                bot_service.send_teams_reply,
                service_url,
                user_conversation_id,
                goodbye_text,
            )
            background_tasks.add_task(cleanup_user_data, user_id, db)
            return {"status": "ok", "message": "user_cleanup_initiated"}

        if activity_type in ("installationUpdate", "conversationUpdate"):
            # 2. ORM 객체를 세션에 다 올리지 않고 필요한 필드만 select() 실행하여 메모리 경량화
            stmt_user = select(User.id, User.email, User.grade).where(User.id == user_id)
            existing_user_row = db.execute(stmt_user).first()

            if not existing_user_row:
                # 신규 유저 생성
                new_user = User(
                    id=user_id,
                    email=user_email,
                    grade=user_grade,
                    conversation_id=user_conversation_id,
                    service_url=service_url,
                )
                db.add(new_user)
                logger.info("[신규 유저 등록]")
            else:
                # 기존 유저 정보 갱신 (지정 객체만 단건 조작)
                db_user = db.get(User, user_id)
                if db_user:
                    if user_email:
                        db_user.email = user_email
                    if user_grade:
                        db_user.grade = user_grade
                    if user_conversation_id:
                        db_user.conversation_id = user_conversation_id
                    if service_url:
                        db_user.service_url = service_url
                    logger.info("🔄 [유저 정보 갱신]")

            db.commit()

        # 3. 웰컴 메시지 발송
        if activity_type in ("installationUpdate", "conversationUpdate"):
            # 봇 추가/설치 시: 웰컴 메시지 발송
            welcome_text = (
                "**CalendarSync 서비스가 정상 연결되었습니다!**\n\n"
                "백그라운드에서 공지사항 및 포스터를 분석하여 "
                "캘린더로 자동 동기화해 드립니다. 별도의 명령어 없이 작동합니다."
            )
            background_tasks.add_task(
                bot_service.send_teams_reply,
                service_url,
                user_conversation_id,
                welcome_text,
            )

        elif activity_type == "message":
            stmt_user = select(User.service_url, User.conversation_id).where(User.id == user_id)
            existing_user_row = db.execute(stmt_user).first()

            if existing_user_row:
                # 1. 메시지 텍스트 추출 및 HTML/AtMention 태그 정제
                raw_text = data.get("text", "") or ""
                # 팀즈 @봇이름 태그(<at>...</at>) 및 공백 제거
                clean_text = re.sub(r"<at>.*?</at>", "", raw_text).strip()

                # 2. 커맨드 분기 처리
                if clean_text in ("/help", "help"):
                    reply_text = (
                        "**CalendarSync 사용 안내**\n\n"
                        "• **자동 동기화**: 채널에 올라오는 공지사항을 AI가 분석하여 캘린더에 자동 등록합니다.\n\n"
                        "• **지원 명령어**:\n"
                        "  - '/help': 도움말 출력\n"
                        "  - '/status': 서비스 연결 상태 및 서버 상태 확인\n"
                        "  - '/schedule': 오늘의 일정 불러오기\n\n"
                        f"명령어는 {MESSAGE_RATE_WINDOW_SECONDS}초에 {MESSAGE_RATE_LIMIT}회까지 사용할 수 있습니다."
                    )

                elif clean_text in ("/status", "status"):
                    grade_text = f"{user_grade}학년" if user_grade else "일반"
                    reply_text = (
                        f"🟢 **CalendarSync 서비스 상태: 정상**\n\n"
                        f"• **등록 계정**: `{user_email or '미확인'}`\n"
                        f"• **학년 정보**: `{grade_text}`"
                    )

                elif clean_text in ("/schedule", "schedule"):
                    try:
                        result = await send_today_notice_to_user(db, user_id)
                    except Exception:
                        db.rollback()
                        logger.exception("오늘 일정 알림 처리 실패")
                    else:
                        if result["success"]:
                            return

                    if existing_user_row.service_url and existing_user_row.conversation_id:
                        background_tasks.add_task(
                            bot_service.send_teams_reply,
                            existing_user_row.service_url,
                            existing_user_row.conversation_id,
                            "오늘 일정을 보내지 못했습니다. 잠시 후 다시 시도해 주세요.",
                        )
                    return {"status": "error", "message": "schedule_delivery_failed"}
                else:
                    # 지정된 커맨드가 아닌 일반 메시지를 보냈을 때의 기본 안내
                    reply_text = (
                        "**CalendarSync 명령어 안내**\n\n"
                        "이 봇은 백그라운드 자동 동기화 전용 서비스입니다.\n"
                        "사용 가능한 명령어를 보시려면 **`/help`**을 입력해주세요."
                    )

                # 3. 비동기 백그라운드 답장 실행
                background_tasks.add_task(
                    bot_service.send_teams_reply,
                    service_url,
                    user_conversation_id,
                    reply_text,
                )

            return {"status": "ok"}
    except Exception as e:
        db.rollback()
        logger.error(f"Teams Webhook 처리 중 에러 발생: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
