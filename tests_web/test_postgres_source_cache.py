"""SQLite-shim checks; real PostgreSQL verification is still required."""

import json
import sqlite3
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_source_cache import PostgresSourceCache
from web_service.storage import digest


@pytest.fixture
def cache(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE web_source_cache (
            cache_key TEXT PRIMARY KEY,channel_id TEXT NOT NULL,message_id TEXT NOT NULL,
            version TEXT NOT NULL,payload_blob TEXT NOT NULL,updated_at BIGINT NOT NULL);""")

    class Shim:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, values=()):
            return self.db.execute(sql.replace("%s", "?"), values)

    @contextmanager
    def connect():
        with sqlite3.connect(path) as db:
            yield Shim(db)

    cipher = Fernet(Fernet.generate_key())
    seal = lambda payload: cipher.encrypt(json.dumps(payload).encode()).decode()
    unseal = lambda blob: json.loads(cipher.decrypt(blob.encode()))
    now = [1000]
    return PostgresSourceCache(connect, seal, unseal, lambda: now[0]), path, now


def event(channel, ident):
    return {"channel_id": channel, "id": ident, "title": "private cached title"}


def key(channel, message):
    return digest(channel + "|" + message)


def test_complete_catalog_filters_channels_deduplicates_and_encrypts(cache):
    repo, path, _ = cache
    repo.cache_analysis(key("a", "m1"), "a", "m1", "v1", [event("a", "one")])
    repo.cache_analysis(key("b", "m2"), "b", "m2", "v1", [event("b", "two")])
    assert repo.catalog_events(["a", "a"]) == [event("a", "one")]
    assert repo.catalog_events([]) == []
    assert repo.cached_analysis(key("a", "m1"), "old") is None
    assert repo.cached_analysis(key("a", "m1"), "v1") == [event("a", "one")]
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT payload_blob FROM web_source_cache").fetchone()[0]
    assert "private cached title" not in raw


def test_identity_and_channel_mismatches_cannot_overwrite_cache(cache):
    repo, _, _ = cache
    cache_key = key("a", "m")
    repo.cache_analysis(cache_key, "a", "m", "v1", [event("a", "one")])
    with pytest.raises(ValueError, match="key mismatch"):
        repo.cache_analysis(cache_key, "b", "other", "v2", [event("b", "two")])
    with pytest.raises(ValueError, match="channel mismatch"):
        repo.cache_analysis(cache_key, "a", "m", "v2", [event("b", "two")])
    assert repo.cached_analysis(cache_key, "v1") == [event("a", "one")]


def test_same_source_update_replaces_version_and_payload(cache):
    repo, _, now = cache
    cache_key = key("a", "m")
    repo.cache_analysis(cache_key, "a", "m", "v1", [event("a", "one")])
    now[0] += 1
    repo.cache_analysis(cache_key, "a", "m", "v2", [event("a", "two")])
    assert repo.cached_analysis(cache_key, "v1") is None
    assert repo.cached_messages("a") == {"m": {"version": "v2", "events": [event("a", "two")]}}


def test_existing_conflicting_identity_is_preserved(cache):
    repo, path, _ = cache
    # Simulate a legacy or corrupted row whose identity does not match its key.
    original = (key("a", "m"), "b", "other", "v1", repo.seal([event("b", "one")]), 1)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO web_source_cache VALUES(?,?,?,?,?,?)", original)
    with pytest.raises(ValueError, match="identity changed"):
        repo.cache_analysis(key("a", "m"), "a", "m", "v2", [event("a", "two")])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM web_source_cache").fetchall() == [original]


def test_encryption_failure_never_opens_a_write_transaction(cache, monkeypatch):
    repo, _, _ = cache
    cache_key = key("a", "m")
    repo.cache_analysis(cache_key, "a", "m", "v1", [event("a", "one")])

    def fail_seal(payload):
        raise ValueError("cannot seal")

    def fail_connect():
        pytest.fail("A failed encryption must not open a database connection")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "seal", fail_seal)
        patch.setattr(repo, "connect", fail_connect)
        with pytest.raises(ValueError, match="cannot seal"):
            repo.cache_analysis(cache_key, "a", "m", "v2", [event("a", "two")])
    assert repo.cached_analysis(cache_key, "v1") == [event("a", "one")]


@pytest.mark.parametrize("reader", ["analysis", "messages", "catalog"])
def test_readers_reject_payload_from_another_channel(cache, reader):
    repo, path, _ = cache
    cache_key = key("a", "m")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO web_source_cache VALUES(?,?,?,?,?,?)",
            (cache_key, "a", "m", "v1", repo.seal([event("b", "one")]), 1),
        )
    with pytest.raises(ValueError, match="channel mismatch"):
        if reader == "analysis":
            repo.cached_analysis(cache_key, "v1")
        elif reader == "messages":
            repo.cached_messages("a")
        else:
            repo.catalog_events(["a"])


def test_touch_rotates_oldest_message_recheck_order(cache):
    repo, _, now = cache
    repo.cache_analysis(key("a", "m1"), "a", "m1", "v1", [])
    now[0] += 1
    repo.cache_analysis(key("a", "m2"), "a", "m2", "v1", [])
    assert list(repo.cached_messages("a")) == ["m1", "m2"]
    now[0] += 1
    repo.touch_analysis(key("a", "m1"))
    assert list(repo.cached_messages("a")) == ["m2", "m1"]
