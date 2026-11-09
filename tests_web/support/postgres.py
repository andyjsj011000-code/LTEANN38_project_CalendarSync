"""Native PostgreSQL fixtures using only the explicit disposable-DB guards."""

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_database import PostgresDatabase
from web_service.storage import digest


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
    ddl = (Path(__file__).parents[2] / "web_service" / "postgresql_schema_draft.sql").read_text(
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
