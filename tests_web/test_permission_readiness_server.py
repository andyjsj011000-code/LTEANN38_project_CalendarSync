"""Access refresh must not masquerade as an empty calendar or a missing item."""

from datetime import datetime, timezone

import pytest
from conftest import CLIENT
from test_ingestion_status import CatalogSource, seed_catalog, session_device
from test_postgres_server_integration import postgres_server as postgres_server
from test_server import server as server

from web_service.postgres_shared_schema import enable_shared_channel_schema
from web_service.school_graph import GraphFailure


@pytest.fixture(params=["server", "postgres_server"])
def access_server(request):
    # Native PostgreSQL remains opt-in through its disposable-database fixture.
    server = request.getfixturevalue(request.param)
    if request.param == "postgres_server":
        enable_shared_channel_schema(server.db.connect, server.settings.tenant_id)
    return server


def connected(s):
    s.db.shared_ingestion_enabled = True
    s.integration.source = CatalogSource()
    client = s.client()
    session, device = session_device(s, client)
    seed_catalog(s)
    response = client.get("/api/today")
    assert response.status_code == 200
    assert [event["title"] for event in response.json()["teams"]] == ["Cached deadline"]
    return client, session, device


@pytest.mark.parametrize("ingest", [False, True])
def test_every_shared_refresh_preserves_unexpired_access_during_graph(
    access_server, monkeypatch, ingest
):
    s = access_server
    client, session, device = connected(s)
    original_channels = s.graph.channels
    observed = []

    def channels(*args):
        observed.append(s.db.sync_status(session["user_id"])["state"])
        assert s.db.channels(session["user_id"], session["tenant_id"])
        response = client.get("/api/today")
        assert response.status_code == 200
        assert [event["title"] for event in response.json()["teams"]] == ["Cached deadline"]
        return original_channels(*args)

    monkeypatch.setattr(s.graph, "channels", channels)
    s.integration.run_sync(device, ingest=ingest, force=True)

    assert observed == ["checking"] * (2 if ingest else 1)
    assert s.db.sync_status(session["user_id"])["state"] == "connected"


def test_hide_can_use_still_valid_access_while_another_check_runs(access_server):
    s = access_server
    client, session, _ = connected(s)
    assert s.db.begin_sync(session["user_id"], 0, preserve_access=True)

    response = client.patch("/api/events/direct:cached/state", json={"hidden": True})

    assert response.status_code == 200
    assert response.json()["hidden"] is True
    assert s.graph.calls == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/today",
        "/api/calendar?start=2026-10-01&end=2026-10-31",
        "/api/channels",
        "/api/hidden?start=2026-10-01&end=2026-10-31",
        "/api/changes",
    ],
)
def test_expired_access_during_check_returns_retryable_response_instead_of_empty_data(
    access_server, path
):
    s = access_server
    client, session, _ = connected(s)
    assert s.db.begin_sync(session["user_id"], 0, preserve_access=True)
    s.now[0] += s.settings.access_seconds + 1

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["code"] == "permission_check_pending"
    assert response.headers["retry-after"] == "2"
    assert "Cached deadline" not in response.text
    assert s.graph.calls == 1


def test_pending_hide_is_not_executed_and_succeeds_once_after_verification(
    access_server, monkeypatch
):
    s = access_server
    client, session, device = connected(s)
    lease = s.db.begin_sync(session["user_id"], 0, preserve_access=True)
    assert lease
    s.now[0] += s.settings.access_seconds + 1
    original_state = s.db.event_state
    mutations = []

    def event_state(*args):
        mutations.append(args)
        return original_state(*args)

    monkeypatch.setattr(s.db, "event_state", event_state)
    response = client.patch("/api/events/direct:cached/state", json={"hidden": True})
    assert response.status_code == 503
    assert response.json()["code"] == "permission_check_pending"
    assert mutations == []

    # Complete the owning verification, then emulate the user's explicit retry.
    channels = [{"id": "class", "team_id": CLIENT, "name": "Class"}]
    s.db.commit_snapshot(
        device, channels, s.db.catalog_events(["class"]), s.settings.access_seconds, lease
    )
    response = client.patch("/api/events/direct:cached/state", json={"hidden": True})

    assert response.status_code == 200
    assert response.json()["hidden"] is True
    assert len(mutations) == 1


def test_genuinely_empty_permission_result_and_unknown_event_keep_normal_semantics(access_server):
    s = access_server
    client, session, device = connected(s)
    assert (
        client.patch("/api/events/direct:unknown/state", json={"hidden": True}).status_code == 404
    )
    s.graph.allowed = False
    s.integration.run_sync(device, force=True)

    response = client.get("/api/today")

    assert response.status_code == 200 and response.json()["teams"] == []
    assert s.db.sync_status(session["user_id"])["state"] == "connected"
    assert client.patch("/api/events/direct:cached/state", json={"hidden": True}).status_code == 404


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_teams_reminder_routes_return_pending_without_reading_or_saving_rules(
    access_server, monkeypatch, method
):
    s = access_server
    client, session, _ = connected(s)
    assert s.db.begin_sync(session["user_id"], 0, preserve_access=True)
    s.now[0] += s.settings.access_seconds + 1
    calls = []
    monkeypatch.setattr(s.db, "rules", lambda *args: calls.append("read"))
    monkeypatch.setattr(s.db, "save_rules", lambda *args: calls.append("save"))
    kwargs = {"json": {"offsets": [-30], "custom_times": []}} if method == "PUT" else {}

    response = client.request(method, "/api/reminders/teams/direct:cached", **kwargs)

    assert response.status_code == 503
    assert response.json()["code"] == "permission_check_pending"
    assert response.headers["retry-after"] == "2"
    assert calls == []


@pytest.mark.parametrize("failure", ["verification_failed", "source_unavailable"])
def test_queued_collection_failure_keeps_freshly_verified_cache_usable(
    access_server, monkeypatch, failure
):
    s = access_server
    client, session, device = connected(s)
    marked = []

    def collect(*args):
        raise GraphFailure(failure)

    monkeypatch.setattr(s.integration.source, "collect", collect)
    monkeypatch.setattr(s.db, "mark_ingested", marked.append)
    response = client.post("/api/teams/sync")

    assert response.status_code == 200 and response.json()["ingestion"] == {"state": "queued"}
    assert s.db.sync_status(session["user_id"])["state"] == "connected"
    assert marked == [] and s.db.device(device["id_hash"])["ingest_at"] == 0
    assert [event["title"] for event in client.get("/api/today").json()["teams"]] == [
        "Cached deadline"
    ]
    assert client.patch("/api/events/direct:cached/state", json={"hidden": True}).status_code == 200


def test_collection_failure_recovery_excludes_channels_revoked_during_collection(
    access_server, monkeypatch
):
    s = access_server
    client, session, device = connected(s)

    def collect(*args):
        s.graph.allowed = False
        raise GraphFailure("verification_failed")

    monkeypatch.setattr(s.integration.source, "collect", collect)
    assert client.post("/api/teams/sync").status_code == 200

    assert s.db.sync_status(session["user_id"])["state"] == "connected"
    assert s.db.channels(session["user_id"], session["tenant_id"]) == []
    assert client.get("/api/today").json()["teams"] == []
    assert client.patch("/api/events/direct:cached/state", json={"hidden": True}).status_code == 404
    assert s.db.device(device["id_hash"])["ingest_at"] == 0


def test_collection_recovery_remains_fail_closed_when_fresh_permission_verification_fails(
    access_server, monkeypatch
):
    s = access_server
    client, session, device = connected(s)

    def channels(*args):
        raise GraphFailure("verification_failed")

    def collect(*args):
        monkeypatch.setattr(s.graph, "channels", channels)
        raise GraphFailure("source_unavailable")

    monkeypatch.setattr(s.integration.source, "collect", collect)
    assert client.post("/api/teams/sync").status_code == 200

    assert s.db.sync_status(session["user_id"])["state"] == "verification_failed"
    assert s.db.channels(session["user_id"], session["tenant_id"]) == []
    personal_task(s, session["user_id"])
    response = client.get("/api/today")
    assert response.status_code == 503
    assert response.json()["teams_access_unverified"] is True
    assert response.json()["connection_state"] == "verification_failed"
    assert response.json()["retryable"] is True
    assert "Cached deadline" not in response.text
    with s.db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM web_events").fetchone()[0] == 1
    personal = client.get("/api/personal/today?day=2026-10-22")
    assert personal.status_code == 200 and len(personal.json()["personal"]) == 1
    assert s.db.device(device["id_hash"])["ingest_at"] == 0


@pytest.mark.parametrize(
    "failure", ["source_unavailable", "verification_failed", "refresh_unavailable", "refresh_busy"]
)
def test_temporary_refresh_failure_is_not_published_as_empty_calendar(
    access_server, monkeypatch, failure
):
    s = access_server
    client, session, device = connected(s)
    personal_task(s, session["user_id"])
    original = s.graph.channels

    def unavailable(*args):
        raise GraphFailure(failure)

    monkeypatch.setattr(s.graph, "channels", unavailable)
    s.now[0] += 1
    s.integration.run_sync(device, force=True)

    response = client.get("/api/calendar?start=2026-10-01&end=2026-10-31")

    assert response.status_code == 503
    assert response.json()["connection_state"] == failure
    assert response.json()["retryable"] is True
    assert response.json()["teams_access_unverified"] is True
    assert "Cached deadline" not in response.text
    assert client.get("/api/personal/today?day=2026-10-22").status_code == 200
    monkeypatch.setattr(s.graph, "channels", original)
    s.now[0] += s.settings.access_seconds
    recovered = client.get("/api/calendar?start=2026-10-01&end=2026-10-31")
    assert recovered.status_code == 200
    assert any(event["title"] == "Cached deadline" for event in recovered.json()["items"])


@pytest.mark.parametrize(
    "path",
    [
        "/api/today",
        "/api/calendar?start=2026-10-01&end=2026-10-31",
        "/api/hidden?start=2026-10-01&end=2026-10-31",
        "/api/channels",
    ],
)
def test_read_response_crossing_original_proof_expiry_is_retryable(
    access_server, monkeypatch, path
):
    s = access_server
    client, session, _ = connected(s)
    checked = s.db.sync_status(session["user_id"])["checked_at"]
    original = s.integration.read_teams

    def expires_after_body(current, operation):
        def build():
            result = operation()
            s.now[0] += s.settings.access_seconds
            return result

        return original(current, build)

    monkeypatch.setattr(s.integration, "read_teams", expires_after_body)

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["connection_state"] == "connected"
    assert response.json()["retryable"] is True
    assert response.json()["teams_access_unverified"] is True
    assert "Cached deadline" not in response.text
    assert s.db.sync_status(session["user_id"])["checked_at"] == checked


def personal_task(s, user, day="2026-10-22"):
    return s.db.add_task(
        user,
        {
            "title": "My personal task",
            "notes": "",
            "start_date": day,
            "end_date": day,
            "time": None,
            "display_mode": "daily",
            "selected_dates": [],
        },
    )


def test_personal_today_remains_available_while_initial_teams_permission_is_pending(
    access_server, monkeypatch
):
    s = access_server
    s.db.shared_ingestion_enabled = True
    s.integration.source = CatalogSource()
    client = s.client()
    session, device = session_device(s, client)
    item = personal_task(s, session["user_id"])
    assert s.db.begin_sync(session["user_id"], 0, preserve_access=True)
    status = s.db.sync_status(session["user_id"])

    def forbidden(*args, **kwargs):
        pytest.fail("Personal agenda must not query or synchronize Teams")

    monkeypatch.setattr(s.graph, "channels", forbidden)
    monkeypatch.setattr(s.db, "agenda", forbidden)
    monkeypatch.setattr(s.db, "calendar", forbidden)
    assert client.get("/api/today").status_code == 503
    response = client.get("/api/personal/today")
    assert response.status_code == 200
    assert response.json() == {"date": "2026-10-22", "personal": [item]}
    assert s.graph.calls == 0 and s.integration.source.calls == 0
    assert s.db.sync_status(session["user_id"]) == status
    assert s.db.channels(session["user_id"], session["tenant_id"]) == []
    assert s.db.device(device["id_hash"])["ingest_at"] == 0
    assert client.get("/api/service").json()["personal_agenda"] is True


def test_personal_today_uses_korean_date_and_rejects_invalid_dates(access_server, monkeypatch):
    s = access_server
    s.now[0] = int(datetime(2026, 10, 22, 16, tzinfo=timezone.utc).timestamp())
    client = s.client()
    session, _ = session_device(s, client)
    item = personal_task(s, session["user_id"], "2026-10-23")
    personal_task(s, session["user_id"], "2026-10-22")
    assert client.get("/api/personal/today").json() == {"date": "2026-10-23", "personal": [item]}
    assert len(client.get("/api/personal/today?day=2026-10-22").json()["personal"]) == 1
    assert client.get("/api/personal/today?day=invalid").status_code == 422
    assert s.graph.calls == 0


@pytest.mark.parametrize("access", ["anonymous", "expired", "revoked"])
def test_personal_today_still_requires_a_valid_school_session(access_server, access):
    s = access_server
    client = s.client(None) if access == "anonymous" else s.client()
    if access != "anonymous":
        session, device = session_device(s, client)
        personal_task(s, session["user_id"])
        if access == "revoked":
            s.db.revoke_device(device["id_hash"])
        else:
            s.now[0] += s.settings.session_seconds + 1
    assert client.get("/api/personal/today").status_code == 401
    assert s.graph.calls == 0
