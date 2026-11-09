"""Behavioral checks through a SQLite shim, not PostgreSQL validation."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_auth_store import PostgresAuthStore
from web_service.storage import Missing


@pytest.fixture
def auth(tmp_path):
    path = tmp_path / "auth.sqlite3"
    schema = (Path(__file__).parents[1] / "web_service" / "postgresql_schema_draft.sql").read_text(
        encoding="utf-8"
    )
    with sqlite3.connect(path) as db:
        db.executescript(schema)

    class Shim:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, params=()):
            return self.db.execute(sql.replace("%s", "?").replace(" FOR UPDATE", ""), params)

    @contextmanager
    def connect():
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            yield Shim(db)

    key = Fernet(Fernet.generate_key())

    def seal(payload):
        return key.encrypt(json.dumps(payload).encode()).decode()

    def unseal(blob):
        return json.loads(key.decrypt(blob.encode()))

    now = [1000]
    return PostgresAuthStore(connect, seal, unseal, lambda: now[0]), path, now


def test_flow_is_encrypted_single_use_and_expired_flow_is_deleted(auth):
    store, path, now = auth
    raw = store.store_flow({"nonce": "sensitive-test-nonce"})
    with sqlite3.connect(path) as db:
        stored = db.execute("SELECT id_hash,flow_blob FROM web_auth_flows").fetchone()
    assert raw not in stored and "sensitive-test-nonce" not in stored[1]
    assert store.consume_flow(raw)["nonce"] == "sensitive-test-nonce"
    with pytest.raises(Missing):
        store.consume_flow(raw)
    expired = store.store_flow({"nonce": "old"})
    now[0] += 600
    with pytest.raises(Missing):
        store.consume_flow(expired)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM web_auth_flows").fetchone()[0] == 0


def test_session_is_scoped_revocable_and_rejects_disabled_user(auth):
    store, path, now = auth
    alice = store.save_user("tenant", "alice", {"name": "Alice"})
    raw = store.issue_session(alice, 60)
    assert store.session(raw)["user_id"] == alice
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id_hash FROM web_sessions").fetchone()[0] != raw
        db.execute("UPDATE web_users SET active=0 WHERE id=?", (alice,))
    assert store.session(raw) is None
    with pytest.raises(Missing):
        store.issue_session(alice, 60)
    store.revoke_session(raw)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 0


def test_session_lifetime_rejects_invalid_values_and_expiry_is_enforced(auth):
    store, path, now = auth
    user = store.save_user("tenant", "alice", {"name": "Alice"})
    for lifetime in (0, -1, True, None, 1.5, "60"):
        with pytest.raises(ValueError):
            store.issue_session(user, lifetime)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 0
    raw = store.issue_session(user, 60)
    assert store.session(raw)["user_id"] == user
    now[0] += 60
    assert store.session(raw) is None
