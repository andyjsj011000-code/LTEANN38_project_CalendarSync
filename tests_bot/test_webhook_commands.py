import json
import sys
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.pool import StaticPool

from app.services.bot_service import BotService

REPO_ROOT = Path(__file__).resolve().parents[1]
STUDENT = "test-student"
ACCOUNT = "student@example.invalid"
CONNECTOR = "https://connector.example.invalid"
CONVERSATION = "test-conversation"
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone(timedelta(hours=9)))
SCHEDULE_TITLE = "테스트 행사"
AUTH_HEADERS = {"Authorization": "Bearer test"}


def load_bot_module(monkeypatch, name):
    """테스트 DB와 외부 서비스 대역을 연결한 뒤 실제 모듈을 로드한다."""
    path = REPO_ROOT.joinpath(*name.split(".")).with_suffix(".py")
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bot(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    base = declarative_base()
    settings = SimpleNamespace(
        DATA_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
        DUPLICATE_WEBHOOK_DEBOUNCE_SECONDS=30,
        GRADE_OVERRIDES={},
    )
    graph = AsyncMock()
    graph.get_user.return_value = {"mail": ACCOUNT}
    connector_auth = SimpleNamespace(authenticate=AsyncMock())
    db_calls = []
    responses = deque()
    sent = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "test-token"})
        assert str(request.url) == f"{CONNECTOR}/v3/conversations/{CONVERSATION}/activities"
        sent.append(json.loads(request.content)["text"])
        status = responses.popleft() if responses else 201
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status)

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    sender = BotService(
        "test-app",
        "test-secret",
        "test-tenant",
        outbound,
        allowed_hosts={"connector.example.invalid"},
    )

    def get_db():
        db_calls.append(True)
        with Session(engine) as db:
            yield db

    for name, contents in {
        "app.core.config": {"settings": settings},
        "app.core.database": {"Base": base, "get_db": get_db},
        "app.core.dependencies": {
            "bot_service": sender,
            "graph_service": graph,
            "bot_connector_auth": connector_auth,
        },
    }.items():
        module = ModuleType(name)
        vars(module).update(contents)
        monkeypatch.setitem(sys.modules, name, module)

    try:
        load_bot_module(monkeypatch, "app.core.security")
        load_bot_module(monkeypatch, "app.models.types")
        domain = load_bot_module(monkeypatch, "app.models.domain")
        calendar = load_bot_module(monkeypatch, "app.models.master_calendar")
        notification = load_bot_module(monkeypatch, "app.utils.notification")
        monkeypatch.setattr(notification, "now_kst", lambda: NOW)
        webhook = load_bot_module(monkeypatch, "app.endpoints.webhook")

        base.metadata.create_all(engine)
        with Session(engine) as db:
            student = domain.User(id=STUDENT, service_url=CONNECTOR, conversation_id=CONVERSATION)
            channel = domain.Channel(channel_id="test-channel", team_id="test-team")
            event = calendar.MasterCalendar(
                channel=channel,
                raw_message_id="test-message",
                title=SCHEDULE_TITLE,
                start_datetime=NOW,
                end_datetime=NOW + timedelta(hours=1),
            )
            db.add(calendar.UserSyncLog(user=student, master_schedule=event))
            db.commit()

        @asynccontextmanager
        async def lifespan(_):
            async with outbound:
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(webhook.router)
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                graph=graph,
                connector_auth=connector_auth,
                db_calls=db_calls,
                responses=responses,
                sent=sent,
                settings=settings,
                webhook=webhook,
            )
    finally:
        engine.dispose()


def send_command(
    bot, text, *, activity_id="test-activity", channel="msteams", conversation=CONVERSATION
):
    activity = {
        "type": "message",
        "channelId": channel,
        "from": {"aadObjectId": STUDENT},
        "conversation": {"id": conversation},
        "serviceUrl": CONNECTOR,
        "text": text,
    }
    if activity_id is not None:
        activity["id"] = activity_id
    return bot.client.post("/api/messages", json=activity, headers=AUTH_HEADERS)


@pytest.mark.parametrize("statuses", [(201,), (401, 202)])
def test_schedule_posts_todays_events(bot, statuses):
    bot.responses.extend(statuses)

    response = send_command(bot, "/schedule")

    assert response.status_code == 200
    assert response.json() is None
    assert len(bot.sent) == len(statuses)
    assert all(SCHEDULE_TITLE in text for text in bot.sent)


@pytest.mark.parametrize("failure", [500, httpx.ConnectError("test connection failed")])
def test_schedule_reports_send_failure(bot, failure):
    bot.responses.append(failure)

    response = send_command(bot, "/schedule")

    assert response.json() == {"status": "error", "message": "schedule_delivery_failed"}
    assert len(bot.sent) == 2
    assert SCHEDULE_TITLE in bot.sent[0]
    assert "오늘 일정을 보내지 못했습니다." in bot.sent[1]


def test_schedule_reports_an_unexpected_notice_exception(bot, monkeypatch):
    notice = AsyncMock(side_effect=RuntimeError("unexpected test failure"))
    monkeypatch.setattr(bot.webhook, "send_today_notice_to_user", notice)

    response = send_command(bot, "/schedule")

    assert response.json() == {"status": "error", "message": "schedule_delivery_failed"}
    assert len(bot.sent) == 1
    assert "오늘 일정을 보내지 못했습니다." in bot.sent[0]


@pytest.mark.parametrize("profile", ["no_grade", "empty", "error"])
def test_status_keeps_service_and_account_lines_without_grade(bot, profile):
    if profile == "empty":
        bot.graph.get_user.return_value = None
    elif profile == "error":
        bot.graph.get_user.side_effect = RuntimeError("test Graph failure")

    response = send_command(bot, "/status")

    assert response.json() == {"status": "ok"}
    (message,) = bot.sent
    assert "CalendarSync 서비스 상태: 정상" in message
    account = ACCOUNT if profile == "no_grade" else "미확인"
    assert f"등록 계정**: `{account}`" in message
    assert "학년 정보**: `일반`" in message


def test_grade_can_be_inferred_without_override_configuration(bot):
    bot.settings.GRADE_OVERRIDES = None
    assert bot.webhook.calculate_grade_from_email("25student@example.invalid", 2026) == 2


@pytest.mark.parametrize("activity_ids", [("help-1", "schedule-1"), (None, None)])
def test_separate_commands_from_same_user_are_processed(bot, activity_ids):
    help_reply = send_command(bot, "/help", activity_id=activity_ids[0])
    schedule_reply = send_command(bot, "/schedule", activity_id=activity_ids[1])

    assert help_reply.json() == {"status": "ok"}
    assert schedule_reply.json() is None
    assert len(bot.sent) == 2
    assert "사용 안내" in bot.sent[0]
    assert SCHEDULE_TITLE in bot.sent[1]


def test_repeat_delivery_is_ignored_in_the_same_channel_only(bot):
    assert send_command(bot, "/help").json() == {"status": "ok"}
    repeated = send_command(bot, "/help")
    assert repeated.json() == {"status": "ok", "message": "duplicate_event_ignored"}
    assert send_command(bot, "/help", channel="other-test-channel").json() == {"status": "ok"}
    assert len(bot.sent) == 2
    assert bot.graph.get_user.await_count == 2


def test_activity_id_is_scoped_to_conversation(bot):
    assert send_command(bot, "/schedule").json() is None
    assert send_command(bot, "/schedule", conversation="other-conversation").json() is None
    assert len(bot.sent) == 2


def test_burst_limit_is_separate_from_retry_deduplication(bot, monkeypatch):
    now = [100.0]
    cache = bot.webhook.TTLCache(maxsize=1024, ttl=30, timer=lambda: now[0])
    monkeypatch.setattr(bot.webhook, "RECENT_MESSAGE_REQUESTS", cache)
    for index in range(5):
        activity_id = f"activity-{index}"
        assert send_command(bot, "/help", activity_id=activity_id).json() == {"status": "ok"}
        retry = send_command(bot, "/help", activity_id=activity_id)
        assert retry.json() == {"status": "ok", "message": "duplicate_event_ignored"}

    limited = send_command(bot, "/help", activity_id="another-event")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"
    assert len(bot.sent) == 5

    now[0] += 30
    assert send_command(bot, "/help", activity_id="another-event").json() == {"status": "ok"}
    assert len(bot.sent) == 6


def test_command_does_not_suppress_lifecycle_events(bot):
    assert send_command(bot, "/help").json() == {"status": "ok"}
    event = {
        "type": "conversationUpdate",
        "from": {"aadObjectId": STUDENT},
        "conversation": {"id": CONVERSATION},
        "serviceUrl": CONNECTOR,
    }
    assert bot.client.post("/api/messages", json=event, headers=AUTH_HEADERS).status_code == 200
    event["type"] = "installationUpdate"
    repeated_welcome = bot.client.post("/api/messages", json=event, headers=AUTH_HEADERS)
    assert repeated_welcome.json() == {"status": "ok", "message": "duplicate_event_ignored"}
    assert len(bot.sent) == 2
    assert "서비스가 정상 연결되었습니다" in bot.sent[1]


def test_missing_sender_does_not_call_graph(bot):
    response = bot.client.post("/api/messages", json={"type": "message"}, headers=AUTH_HEADERS)

    assert response.json() == {"status": "ok", "message": "no_user_id_in_activity"}
    bot.graph.get_user.assert_not_awaited()


def test_rejected_authentication_precedes_all_command_work(bot):
    bot.connector_auth.authenticate.side_effect = HTTPException(status_code=403)

    response = send_command(bot, "/schedule")

    assert response.status_code == 403
    assert not bot.db_calls
    assert not bot.sent
    bot.graph.get_user.assert_not_awaited()
    assert not bot.webhook.RECENT_MESSAGE_ACTIVITIES
    assert not bot.webhook.RECENT_MESSAGE_REQUESTS
