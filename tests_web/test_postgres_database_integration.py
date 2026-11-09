"""Opt-in facade checks; use an explicitly disposable PostgreSQL database only.

No database access occurs unless both environment guards are present. Every
case creates and drops only its own randomly named schema. These tests must
never target a student, shared, or production database. Local shim results do
not substitute for running these cases on PostgreSQL.
"""

import os
import ipaddress
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_database import PostgresDatabase
from web_service.postgres_connection import PostgresConnectionFactory
from web_service.storage import Missing, digest


@pytest.fixture
def postgres_database():
    dsn = os.environ.get("CALENDARSYNC_DISPOSABLE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("No dedicated disposable PostgreSQL database configured")
    if os.environ.get("CALENDARSYNC_ALLOW_TEMP_SCHEMA") != "yes":
        pytest.skip("Disposable database and temporary schema creation must be explicitly enabled")
    psycopg = pytest.importorskip("psycopg")
    # This identifier contains only a fixed prefix and UUID hex, never user SQL.
    schema = "calendarsync_test_" + uuid.uuid4().hex
    ddl = (Path(__file__).parents[1] / "web_service" / "postgresql_schema_draft.sql").read_text(
        encoding="utf-8"
    )
    now = [1000]
    created = False

    @contextmanager
    def connect():
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SET LOCAL search_path TO " + schema)
            connection.execute("SET LOCAL statement_timeout = '5s'")
            connection.execute("SET LOCAL lock_timeout = '4s'")
            yield connection

    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
            connection.execute("SET statement_timeout = '5s'")
            connection.execute("CREATE SCHEMA " + schema)
            created = True
        with connect() as connection:
            # Comments may contain semicolons; this draft uses plain DDL only.
            statements = "\n".join(
                line for line in ddl.splitlines() if not line.lstrip().startswith("--")
            )
            for statement in statements.split(";"):
                if statement.strip():
                    connection.execute(statement)

        repo = PostgresDatabase(
            connect, Fernet.generate_key().decode(), "tenant", clock=lambda: now[0]
        )
        repo.initialize()
        users, devices = {}, {}
        for name in ("alice", "bob"):
            user = repo.save_user("tenant", name, {"name": name})
            session = repo.issue_session(user, 3600)
            raw, _ = repo.add_connection(
                session, user, "test-token-" + name, 3600, "test-cache-" + name, background=True
            )
            users[name], devices[name] = user, repo.device(digest(raw))
        yield SimpleNamespace(
            repo=repo, connect=connect, schema=schema, users=users, devices=devices, now=now
        )
    finally:
        if created:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
                connection.execute("SET statement_timeout = '5s'")
                connection.execute("DROP SCHEMA " + schema + " CASCADE")


def source(title):
    return {
        "id": "direct:shared-event",
        "channel_id": "class",
        "title": title,
        "notes": "Test-only source",
        "time": "09:00",
        "start_date": "2026-11-05",
        "end_date": "2026-11-06",
        "kind": "assignment",
        "cancelled": False,
        "source_version": title,
        "source_url": "https://teams.microsoft.com/",
    }


CHANNELS = [{"id": "class", "name": "Class", "team_id": "team"}]


def test_native_connection_factory_scopes_transactions_and_rolls_back(postgres_database):
    p = postgres_database
    # CI's explicitly disposable loopback server has no TLS. Never lower the
    # factory's remote TLS policy or put the DSN into fixture metadata/repr.
    host = urlsplit(os.environ["CALENDARSYNC_DISPOSABLE_POSTGRES_DSN"]).hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    options = {"sslmode": "disable"} if loopback else {}
    connect = PostgresConnectionFactory(
        os.environ["CALENDARSYNC_DISPOSABLE_POSTGRES_DSN"],
        p.schema,
        statement_timeout_ms=17000,
        lock_timeout_ms=6000,
        **options,
    )

    with connect() as connection:
        settings = connection.execute("""SELECT current_schema(),
            current_setting('statement_timeout')::interval,
            current_setting('lock_timeout')::interval""").fetchone()
        assert isinstance(settings, tuple)
        assert settings == (p.schema, timedelta(seconds=17), timedelta(seconds=6))
        connection.execute("""CREATE TABLE connection_factory_probe (
            id INTEGER PRIMARY KEY,payload TEXT NOT NULL)""")
        connection.execute("INSERT INTO connection_factory_probe VALUES(%s,%s)", (1, "committed"))

    with pytest.raises(Missing):
        with connect() as connection:
            connection.execute(
                "INSERT INTO connection_factory_probe VALUES(%s,%s)", (2, "must-roll-back")
            )
            raise Missing()
    with connect() as connection:
        assert connection.execute("""SELECT id,payload FROM connection_factory_probe
            ORDER BY id""").fetchall() == [(1, "committed")]
        connection.execute(
            "INSERT INTO connection_factory_probe VALUES(%s,%s)", (3, "committed-after-rollback")
        )

    # A native driver error is redacted and also rolls its transaction back.
    with pytest.raises(RuntimeError, match="connection or transaction failed"):
        with connect() as connection:
            connection.execute(
                "INSERT INTO connection_factory_probe VALUES(%s,%s)",
                (1, "duplicate-must-not-replace"),
            )
    with p.connect() as connection:
        assert connection.execute("""SELECT id,payload FROM connection_factory_probe
            ORDER BY id""").fetchall() == [(1, "committed"), (3, "committed-after-rollback")]


def test_facade_initialize_validates_without_resetting_existing_data(postgres_database):
    p = postgres_database
    task = p.repo.add_task(
        p.users["alice"],
        {
            "title": "Private task",
            "notes": "",
            "time": None,
            "start_date": "2026-11-05",
            "end_date": "2026-11-06",
            "display_mode": "daily",
            "selected_dates": [],
        },
    )
    p.repo.initialize()
    assert p.repo.task(p.users["alice"], task["id"])["title"] == "Private task"
    with pytest.raises(Missing):
        p.repo.task(p.users["bob"], task["id"])
    with p.connect() as connection:
        blob = connection.execute(
            "SELECT payload_blob FROM web_personal_tasks WHERE id=%s", (task["id"],)
        ).fetchone()[0]
        assert "Private task" not in blob


def test_two_sync_claimants_have_only_one_live_lease(postgres_database):
    p = postgres_database
    ready = Barrier(2)

    def claim():
        ready.wait(timeout=5)
        return p.repo.begin_sync(p.users["alice"], 0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    (winner,) = [lease for lease in results if lease]
    assert len(winner) == 64
    assert sum(lease is False for lease in results) == 1
    assert p.repo.fail_sync(p.users["alice"], "verification_failed", winner)
    # Retry in the very same clock second must not reuse the old lease identity.
    current = p.repo.begin_sync(p.users["alice"], 0)
    assert current and current != winner
    with pytest.raises(Missing):
        p.repo.commit_snapshot(p.devices["alice"], CHANNELS, [source("stale")], 60, winner)
    assert p.repo.sync_status(p.users["alice"])["state"] == "checking"
    p.repo.commit_snapshot(p.devices["alice"], CHANNELS, [source("current")], 60, current)


def test_users_concurrently_commit_shared_source_with_independent_views(postgres_database):
    p = postgres_database
    leases = {name: p.repo.begin_sync(user, 0) for name, user in p.users.items()}
    ready = Barrier(2)

    def save(name):
        ready.wait(timeout=5)
        p.repo.commit_snapshot(
            p.devices[name], CHANNELS, [source("source-" + name)], 60, leases[name]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, name) for name in ("alice", "bob")]
        for future in futures:
            future.result(timeout=10)
    for name in ("alice", "bob"):
        (item,) = p.repo._shared(p.users[name], "tenant")
        assert item["id"] == "direct:shared-event"
        assert item["title"] == "source-" + name
        assert p.repo.channels(p.users[name], "tenant") == [
            {"id": "class", "name": "Class", "enabled": True}
        ]
    p.repo.override_event(
        p.users["alice"], "tenant", "direct:shared-event", {"title": "Only Alice changed this"}
    )
    p.repo.event_state(p.users["alice"], "tenant", "direct:shared-event", {"hidden": True})
    (alice,) = p.repo._shared(p.users["alice"], "tenant")
    (bob,) = p.repo._shared(p.users["bob"], "tenant")
    assert alice["title"] == "Only Alice changed this" and alice["hidden"]
    assert bob["title"] == "source-bob" and not bob["hidden"]
    assert p.repo._shared(p.users["alice"], "wrong-tenant") == []
    with p.connect() as connection:
        assert connection.execute("SELECT count(*) FROM web_events").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM web_user_source_versions").fetchone()[0] == 2
        )


def test_revoke_fences_concurrent_refresh_and_already_fetched_snapshot(postgres_database):
    p = postgres_database
    device = p.devices["alice"]
    sync_lease = p.repo.begin_sync(p.users["alice"], 0)
    refresh_lease = p.repo.lock_refresh(device["id_hash"])
    assert refresh_lease
    ready = Barrier(2)

    def refresh():
        ready.wait(timeout=5)
        return p.repo.save_refresh(
            device["id_hash"], refresh_lease, "new-test-token", 3600, "new-test-cache"
        )

    def revoke():
        ready.wait(timeout=5)
        p.repo.revoke_device(device["id_hash"], p.users["alice"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshed, revoked = pool.submit(refresh), pool.submit(revoke)
        assert isinstance(refreshed.result(timeout=10), bool)
        revoked.result(timeout=10)
    assert p.repo.device(device["id_hash"]) is None
    assert not p.repo.save_refresh(
        device["id_hash"], refresh_lease, "late-test-token", 3600, "late-test-cache"
    )
    with pytest.raises(Missing):
        p.repo.commit_snapshot(device, CHANNELS, [source("stale")], 60, sync_lease)
    with p.connect() as connection:
        assert connection.execute("SELECT count(*) FROM web_events").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM web_channel_access WHERE user_id=%s", (p.users["alice"],)
            ).fetchone()[0]
            == 0
        )
        active, credentials = connection.execute(
            """SELECT active,credential_blob
            FROM web_connections WHERE id_hash=%s""",
            (device["id_hash"],),
        ).fetchone()
    assert active == 0 and p.repo.unseal(credentials) == {}
