"""Durable collection status shares the real PostgreSQL snapshot transaction."""

import pytest
from test_ingestion_status import CatalogSource, seed_catalog, session_device
from test_postgres_server_integration import postgres_server as postgres_server
from test_shared_ingestion_server import DeferredTasks

from web_service.planner_schema import enable_planner_schema
from web_service.postgres_shared_schema import enable_shared_channel_schema
from web_service.school_graph import GraphFailure
from web_service.storage import Missing


@pytest.fixture
def tracked(postgres_server):
    s = postgres_server
    enable_planner_schema(s.db.connect, s.settings.tenant_id)
    enable_shared_channel_schema(s.db.connect, s.settings.tenant_id)
    s.db.planner_enhancements_enabled = True
    s.db.shared_ingestion_enabled = True
    s.integration.source = CatalogSource()
    client = s.client()
    session, device = session_device(s, client)
    seed_catalog(s)
    return s, client, session, device


def test_permission_refresh_queue_and_collection_have_distinct_success_times(tracked):
    s, client, session, device = tracked
    s.integration.run_sync(device)
    assert s.db.ingestion_status(device)["state"] == "idle"
    assert client.get("/api/today").status_code == 200
    assert s.db.ingestion_status(device)["last_success_at"] is None
    tasks = DeferredTasks()
    assert s.integration.sync(session, tasks)["ingestion"]["state"] == "queued"
    queued = client.get("/api/teams/ingestion-status")
    assert queued.status_code == 200 and queued.json()["state"] == "queued"
    assert queued.json()["last_success_at"] is None
    s.now[0] += 1
    function, args, kwargs = tasks.items[0]
    function(*args, **kwargs)
    completed = client.get("/api/teams/ingestion-status").json()
    assert completed["state"] == "completed" and completed["last_success_at"] == s.now[0]
    assert completed["counts"]["channels_total"] == 1
    assert completed["counts"]["channels_scanned"] == 1
    assert "private_source" not in completed["counts"]
    assert "private-access-token" not in str(completed)
    success = completed["last_success_at"]
    s.now[0] += 2
    s.integration.run_sync(device, force=True)
    assert s.db.ingestion_status(device)["last_success_at"] == success


def test_partial_and_failed_final_verification_preserve_last_success(tracked, monkeypatch):
    s, _, _, device = tracked
    s.integration.run_sync(device, ingest=True)
    success = s.db.ingestion_status(device)["last_success_at"]
    s.now[0] += 1
    s.integration.source.report["partial"] = True
    s.integration.run_sync(device, ingest=True)
    assert s.db.ingestion_status(device)["state"] == "partial"
    assert s.db.ingestion_status(device)["last_success_at"] == success
    s.now[0] += 1
    original_collect = s.integration.source.collect

    def collect(*args, **kwargs):
        result = original_collect(*args, **kwargs)

        def denied(*args):
            raise GraphFailure("verification_failed")

        monkeypatch.setattr(s.graph, "channels", denied)
        return result

    monkeypatch.setattr(s.integration.source, "collect", collect)
    s.integration.run_sync(device, ingest=True)
    failed = s.db.ingestion_status(device)
    assert failed["state"] == "error" and failed["error_code"] == "verification_failed"
    assert failed["last_success_at"] == success


def test_failed_final_commit_never_marks_collection_success(tracked, monkeypatch):
    s, _, _, device = tracked
    original_commit = s.db.commit_snapshot

    def fail_final(*args, **kwargs):
        if kwargs.get("ingestion") is not None:
            raise Missing()
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(s.db, "commit_snapshot", fail_final)
    s.integration.run_sync(device, ingest=True)
    result = s.db.ingestion_status(device)
    assert result["state"] == "error" and result["last_success_at"] is None
    assert s.db.device(device["id_hash"])["ingest_at"] == 0


def test_terminal_status_and_schedule_roll_back_with_expired_snapshot(tracked, monkeypatch):
    s, _, session, device = tracked
    channels = s.graph.channels("", device["object_id"], device["tenant_id"])
    attempt = s.db.claim_ingestion(device)
    lease = s.db.begin_sync(session["user_id"], 0, preserve_access=True)
    original = s.db.finish_ingestion_snapshot_in

    def expire_after_status(*args):
        result = original(*args)
        s.now[0] += s.settings.access_seconds + 1
        return result

    monkeypatch.setattr(s.db, "finish_ingestion_snapshot_in", expire_after_status)
    with pytest.raises(Missing):
        s.db.commit_snapshot(
            device,
            channels,
            s.db.catalog_events(["class"]),
            s.settings.access_seconds,
            lease,
            ingestion={"attempt": attempt, "report": {"partial": False, "channels_total": 1}},
        )
    status = s.db.ingestion_status(device)
    assert status["state"] == "running" and status["last_success_at"] is None
    assert s.db.device(device["id_hash"])["ingest_at"] == 0
    with s.db.connect() as db:
        assert db.execute("SELECT id FROM web_events").fetchall() == []


def test_stale_attempt_cannot_complete_a_newer_attempt_or_advance_schedule(tracked):
    s, _, session, device = tracked
    old = s.db.claim_ingestion(device)
    s.now[0] += 301
    current = s.db.claim_ingestion(device)
    assert current != old
    channels = s.graph.channels("", device["object_id"], device["tenant_id"])
    lease = s.db.begin_sync(session["user_id"], 0, preserve_access=True)
    assert (
        s.db.commit_snapshot(
            device,
            channels,
            [],
            s.settings.access_seconds,
            lease,
            ingestion={"attempt": old, "report": {"partial": False, "channels_total": 1}},
        )
        is False
    )
    status = s.db.ingestion_status(device)
    assert status["state"] == "running" and status["last_success_at"] is None
    assert s.db.device(device["id_hash"])["ingest_at"] == 0


def test_busy_device_preserves_the_running_attempt_and_no_false_success(tracked):
    s, _, _, device = tracked
    attempt = s.db.claim_ingestion(device)
    assert s.integration.run_sync(device, ingest=True) == {"state": "busy"}
    result = s.db.ingestion_status(device)
    assert result["state"] == "running" and result["last_success_at"] is None
    assert s.db.end_ingestion(device, attempt, queued=True)
    assert s.db.ingestion_status(device)["state"] == "queued"


def test_status_route_requires_opt_in_and_authentication(postgres_server):
    s = postgres_server
    assert s.client(None).get("/api/teams/ingestion-status").status_code == 401
    assert s.client().get("/api/teams/ingestion-status").status_code == 404


def test_attempt_expiring_after_terminal_write_rolls_back_on_postgres(tracked, monkeypatch):
    s, _, _, device = tracked
    started, collect = s.now[0], s.integration.source.collect

    def slow_collect(*args):
        result = collect(*args)
        s.now[0] = started + 299
        return result

    finish = s.db.finish_ingestion_snapshot_in

    def finish_then_wait(*args):
        result = finish(*args)
        s.now[0] += 1
        return result

    monkeypatch.setattr(s.integration.source, "collect", slow_collect)
    monkeypatch.setattr(s.db, "finish_ingestion_snapshot_in", finish_then_wait)
    s.integration.run_sync(device, ingest=True)
    status = s.db.ingestion_status(device)
    assert status["state"] == "error" and status["last_success_at"] is None
    assert s.db.device(device["id_hash"])["ingest_at"] == 0


def test_new_final_channel_keeps_collection_partial_and_status_content_free(tracked, monkeypatch):
    s, client, _, device = tracked
    collect = s.integration.source.collect

    def collected(*args):
        result = collect(*args)
        result.checked_channels = frozenset({"class"})
        result.scanned_channels = frozenset({"class"})
        monkeypatch.setattr(
            s.graph,
            "channels",
            lambda *args: [
                {"id": "class", "name": "Class", "team_id": "team"},
                {"id": "other", "name": "Other", "team_id": "team"},
            ],
        )
        return result

    monkeypatch.setattr(s.integration.source, "collect", collected)
    assert s.integration.run_sync(device, ingest=True)["state"] == "partial"
    response = client.get("/api/teams/ingestion-status")
    status = response.json()
    assert status["state"] == "partial" and status["last_success_at"] is None
    assert status["counts"]["channels_total"] == 2 and status["counts"]["channels_checked"] == 1
    assert '"class"' not in response.text and '"other"' not in response.text
    assert s.db.device(device["id_hash"])["ingest_at"] == 0
