"""Durable device progress, isolated SQLite transactions and no network."""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_ingestion_status import STATUS_SCHEMA_SQL
from web_service.sqlite_ingestion_status import SQLiteIngestionStatus
from web_service.storage import Database, Missing
from web_service.test_storage import EXTRA_SCHEMA


class StatusDatabase(SQLiteIngestionStatus, Database):
    planner_enhancements_enabled = True


@pytest.fixture
def status_store(tmp_path):
    now = [1000]
    repo = StatusDatabase(
        tmp_path / "status.sqlite3", Fernet.generate_key().decode(), clock=lambda: now[0]
    )
    repo.initialize()
    devices = {}
    with repo.connect() as db:
        db.executescript(EXTRA_SCHEMA)
        db.execute(STATUS_SCHEMA_SQL)
    for name in ("alice", "bob"):
        user = repo.save_user("school", name, {"name": name})
        device = {"id_hash": "device-" + name, "user_id": user, "tenant_id": "school", "version": 1}
        devices[name] = device
        with repo.connect() as db:
            db.execute(
                """INSERT INTO web_connections
                (id_hash,user_id,credential_blob,token_expires,expires_at,background)
                VALUES(?,?,?,?,?,1)""",
                (device["id_hash"], user, repo.seal({}), 4000, 10000),
            )
    return SimpleNamespace(repo=repo, devices=devices, now=now)


def finish(s, device, attempt, partial=False):
    with s.repo.connect() as db:
        return s.repo.finish_ingestion_snapshot_in(
            db,
            device,
            {
                "attempt": attempt,
                "report": {
                    "channels_total": 2,
                    "channels_checked": 2,
                    "channels_scanned": 1,
                    "partial": partial,
                },
            },
        )


def test_queue_progress_completion_and_partial_keep_distinct_durable_times(status_store):
    s, repo = status_store, status_store.repo
    device = s.devices["alice"]
    assert repo.ingestion_status(device)["state"] == "idle"
    with repo._status_connect() as db:
        repo._status_device(db, device)
        repo._queue_ingestion_in(db, device["id_hash"], s.now[0])
    assert repo.ingestion_status(device)["state"] == "queued"
    token = repo.claim_ingestion(device)
    assert token and repo.claim_ingestion(device) is None
    assert repo.update_ingestion(
        device,
        token,
        {"channels_total": 2, "channels_checked": 1, "private_source": "never-persist-this"},
    )
    first = repo.ingestion_status(device)
    assert first["state"] == "running" and first["last_success_at"] is None
    assert first["counts"] == {"channels_total": 2, "channels_checked": 1}
    s.now[0] += 1
    assert finish(s, device, token)
    assert repo.ingestion_status(device)["last_success_at"] == s.now[0]
    with repo.connect() as db:
        scheduled = db.execute(
            "SELECT ingest_at FROM web_connections WHERE id_hash=?", (device["id_hash"],)
        ).fetchone()[0]
    assert scheduled == s.now[0] + 300
    token = repo.claim_ingestion(device)
    s.now[0] += 1
    assert finish(s, device, token, partial=True)
    partial = repo.ingestion_status(device)
    assert partial["state"] == "partial" and partial["last_success_at"] == s.now[0] - 1
    with repo.connect() as db:
        assert (
            db.execute(
                "SELECT ingest_at FROM web_connections WHERE id_hash=?", (device["id_hash"],)
            ).fetchone()[0]
            == scheduled
        )
    assert repo.ingestion_status(s.devices["bob"])["state"] == "idle"


def test_failed_final_transaction_does_not_claim_success_or_advance_schedule(status_store):
    s, repo = status_store, status_store.repo
    device = s.devices["alice"]
    token = repo.claim_ingestion(device)
    with pytest.raises(RuntimeError, match="rollback"):
        with repo.connect() as db:
            assert repo.finish_ingestion_snapshot_in(
                db, device, {"attempt": token, "report": {"channels_total": 1, "partial": False}}
            )
            raise RuntimeError("rollback")
    assert repo.ingestion_status(device)["state"] == "running"
    assert repo.ingestion_status(device)["last_success_at"] is None
    assert repo.end_ingestion(device, token, error_code="https://private.example/secret")
    failed = repo.ingestion_status(device)
    assert failed["state"] == "error" and failed["error_code"] == "source_unavailable"
    assert failed["last_success_at"] is None
    with repo.connect() as db:
        assert (
            db.execute(
                "SELECT ingest_at FROM web_connections WHERE id_hash=?", (device["id_hash"],)
            ).fetchone()[0]
            == 0
        )


def test_expired_attempt_and_late_writer_cannot_overwrite_replacement(status_store):
    s, repo = status_store, status_store.repo
    device = s.devices["alice"]
    old = repo.claim_ingestion(device)
    s.now[0] += 301
    assert repo.ingestion_status(device)["error_code"] == "interrupted"
    new = repo.claim_ingestion(device)
    assert new and new != old
    assert repo.update_ingestion(device, old, {"channels_total": 100}) is False
    assert repo.end_ingestion(device, old, error_code="source_unavailable") is False
    assert finish(s, device, old) is False
    assert repo.ingestion_status(device)["state"] == "running"
    assert repo.ingestion_status(device)["counts"] is None
    assert finish(s, device, new)


@pytest.mark.parametrize("invalid", ["owner", "tenant", "version", "revoked", "expired"])
def test_device_proof_fences_status_reads_and_progress_writes(status_store, invalid):
    s, repo = status_store, status_store.repo
    device = dict(s.devices["alice"])
    token = repo.claim_ingestion(device)
    if invalid == "owner":
        device["user_id"] = s.devices["bob"]["user_id"]
    elif invalid == "tenant":
        device["tenant_id"] = "another-school"
    elif invalid == "version":
        device["version"] += 1
    else:
        with repo.connect() as db:
            field, value = ("active", 0) if invalid == "revoked" else ("expires_at", s.now[0])
            db.execute(
                "UPDATE web_connections SET " + field + "=? WHERE id_hash=?",
                (value, device["id_hash"]),
            )
    with pytest.raises(Missing):
        repo.ingestion_status(device)
    with pytest.raises(Missing):
        repo.update_ingestion(device, token, {"channels_checked": 5})
    with pytest.raises(Missing):
        finish(s, device, token)


def test_partial_interruption_keeps_already_observed_counts(status_store):
    s, repo = status_store, status_store.repo
    device = s.devices["alice"]
    token = repo.claim_ingestion(device)
    repo.update_ingestion(
        device, token, {"channels_total": 3, "channels_checked": 1, "messages_analyzed": 4}
    )
    with repo.connect() as db:
        repo.finish_ingestion_snapshot_in(
            db,
            device,
            {
                "attempt": token,
                "report": {
                    "channels_total": 3,
                    "channels_checked": 0,
                    "messages_analyzed": 0,
                    "partial": True,
                },
            },
        )
    result = repo.ingestion_status(device)
    assert result["state"] == "partial" and result["last_success_at"] is None
    assert result["counts"]["channels_checked"] == 1
    assert result["counts"]["messages_analyzed"] == 4
