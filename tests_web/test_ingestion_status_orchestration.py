"""Direct server orchestration on SQLite, without TestClient or external I/O."""

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from test_ingestion_status import CatalogSource, seed_catalog

from web_service.config import Settings
from web_service.school_graph import GraphFailure
from web_service.server import ServerIntegration
from web_service.storage import digest
from web_service.test_storage import TestDatabase


@pytest.fixture
def tracked_server(tmp_path):
    now = [1000]
    key = Fernet.generate_key().decode()
    settings = Settings(database_path=tmp_path / "tracked.sqlite3", data_key=key)
    db = TestDatabase(
        settings.database_path, key, clock=lambda: now[0], planner_enhancements_enabled=True
    )
    db.initialize()
    db.shared_ingestion_enabled = True
    user = db.save_user("tenant", "alice", {"name": "Alice"})
    session = db.issue_session(user, 3600)
    raw, _ = db.add_connection(session, user, "synthetic-token", 3600, "synthetic-cache", True)
    device = db.device(digest(raw))
    graph = SimpleNamespace(
        channels=lambda *args: [{"id": "class", "name": "Class", "team_id": "team"}]
    )
    source = CatalogSource()
    integration = ServerIntegration(
        settings, db, SimpleNamespace(), graph, source, SimpleNamespace(configured=False)
    )
    seed_catalog(SimpleNamespace(db=db))
    return SimpleNamespace(
        now=now, db=db, device=device, integration=integration, graph=graph, source=source
    )


def test_permission_only_refresh_does_not_record_collection_success(tracked_server):
    s = tracked_server
    s.integration.run_sync(s.device)
    assert s.db.ingestion_status(s.device)["state"] == "idle"
    s.integration.run_sync(s.device, ingest=True)
    done = s.db.ingestion_status(s.device)
    assert done["state"] == "completed" and done["last_success_at"] == s.now[0]
    assert s.db.device(s.device["id_hash"])["ingest_at"] == s.now[0] + 300
    s.now[0] += 1
    s.integration.run_sync(s.device, force=True)
    assert s.db.ingestion_status(s.device)["last_success_at"] == done["last_success_at"]


def test_partial_keeps_success_timestamp_and_observed_progress(tracked_server, monkeypatch):
    s = tracked_server
    s.integration.run_sync(s.device, ingest=True)
    success = s.db.ingestion_status(s.device)["last_success_at"]
    s.now[0] += 1
    s.source.supports_progress = True

    def interrupted(*args, on_progress):
        on_progress({"channels_total": 2, "channels_checked": 1, "messages_analyzed": 3})
        assert s.db.ingestion_status(s.device)["state"] == "running"
        raise GraphFailure("source_unavailable")

    monkeypatch.setattr(s.source, "collect", interrupted)
    s.integration.run_sync(s.device, ingest=True)
    result = s.db.ingestion_status(s.device)
    assert result["state"] == "partial" and result["last_success_at"] == success
    assert result["counts"]["channels_checked"] == 1
    assert result["counts"]["messages_analyzed"] == 3


@pytest.mark.parametrize("shared", [False, True])
def test_final_graph_failure_records_error_without_success(tracked_server, monkeypatch, shared):
    s = tracked_server
    s.db.shared_ingestion_enabled = shared
    collect = s.source.collect

    def source_then_revocation(*args):
        result = collect(*args)

        def denied(*args):
            raise GraphFailure("verification_failed")

        monkeypatch.setattr(s.graph, "channels", denied)
        return result

    monkeypatch.setattr(s.source, "collect", source_then_revocation)
    s.integration.run_sync(s.device, ingest=True)
    result = s.db.ingestion_status(s.device)
    if shared:
        assert result["state"] == "completed"
        assert result["last_success_at"] == s.now[0]
        # Permission failure is recorded by its independent operation, not by
        # a successful source pass, and cannot rewrite that pass's progress.
        s.integration.run_sync(s.device, force=True)
        assert s.db.sync_status(s.device["user_id"])["state"] == "verification_failed"
        assert s.db.ingestion_status(s.device) == result
        return
    assert result["state"] == "error" and result["error_code"] == "verification_failed"
    assert result["last_success_at"] is None
    assert s.db.device(s.device["id_hash"])["ingest_at"] == 0


def test_collection_completion_does_not_renew_expired_permissions(tracked_server, monkeypatch):
    s = tracked_server
    finish = s.db.finish_ingestion_snapshot_in

    def expire(*args):
        result = finish(*args)
        s.now[0] += s.integration.settings.access_seconds + 1
        return result

    monkeypatch.setattr(s.db, "finish_ingestion_snapshot_in", expire)
    s.integration.run_sync(s.device, ingest=True)
    result = s.db.ingestion_status(s.device)
    assert result["state"] == "completed" and result["last_success_at"] is not None
    assert s.db.channels(s.device["user_id"], s.device["tenant_id"]) == []
    assert s.db.sync_status(s.device["user_id"])["checked_at"] == 1000


def test_duplicate_collection_does_not_replace_active_attempt(tracked_server):
    s = tracked_server
    token = s.db.claim_ingestion(s.device)
    assert s.integration.run_sync(s.device, ingest=True) == {"state": "busy"}
    assert s.db.ingestion_status(s.device)["state"] == "running"
    assert s.db.ingestion_status(s.device)["last_success_at"] is None
    assert s.db.end_ingestion(s.device, token, queued=True)


def test_attempt_expiring_after_terminal_write_rolls_back_completion(tracked_server, monkeypatch):
    s = tracked_server
    started = s.now[0]
    collect = s.source.collect

    def slow_collect(*args):
        result = collect(*args)
        s.now[0] = started + 299
        return result

    finish = s.db.finish_ingestion_snapshot_in

    def finish_then_wait(*args):
        result = finish(*args)
        s.now[0] += 1
        return result

    monkeypatch.setattr(s.source, "collect", slow_collect)
    monkeypatch.setattr(s.db, "finish_ingestion_snapshot_in", finish_then_wait)
    s.integration.run_sync(s.device, ingest=True)
    status = s.db.ingestion_status(s.device)
    assert status["state"] == "error" and status["error_code"] == "interrupted"
    assert status["last_success_at"] is None
    assert s.db.device(s.device["id_hash"])["ingest_at"] == 0


@pytest.mark.parametrize("change", ["added", "removed"])
def test_collection_counts_keep_the_scanned_scope_when_permissions_change_independently(
    tracked_server, monkeypatch, change
):
    s = tracked_server
    first = [{"id": "class", "name": "Class", "team_id": "team"}]
    second = [*first, {"id": "other", "name": "Other", "team_id": "team"}]
    initial, final = (first, second) if change == "added" else (second, first)
    monkeypatch.setattr(s.graph, "channels", lambda *args: initial)
    s.source.report.update(channels_total=len(initial), channels_scanned=len(initial))
    s.source.supports_progress = True
    collect = s.source.collect

    def collected(*args, on_progress):
        result = collect(*args)
        result.checked_channels = frozenset(item["id"] for item in initial)
        result.scanned_channels = result.checked_channels
        on_progress({**result.report, "channels_checked": len(initial)})
        monkeypatch.setattr(s.graph, "channels", lambda *args: final)
        return result

    monkeypatch.setattr(s.source, "collect", collected)
    outcome = s.integration.run_sync(s.device, ingest=True)
    status = s.db.ingestion_status(s.device)
    assert status["state"] == "completed"
    assert status["counts"]["channels_total"] == len(initial)
    assert (
        status["counts"]["channels_checked"] == status["counts"]["channels_scanned"] == len(initial)
    )
    assert status["last_success_at"] is not None
    for value in (outcome, status):
        serialized = json.dumps(value)
        assert '"class"' not in serialized and '"other"' not in serialized
        assert "checked_channels" not in serialized and "scanned_channels" not in serialized
    s.integration.run_sync(s.device, force=True)
    assert s.db.ingestion_status(s.device) == status
    assert {item["id"] for item in s.db.channels(s.device["user_id"], s.device["tenant_id"])} == {
        item["id"] for item in final
    }
