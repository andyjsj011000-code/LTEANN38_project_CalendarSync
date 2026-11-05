"""Behavior checks via SQLite SQL shim; PostgreSQL locking is not modeled."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_devices import PostgresDevices
from web_service.postgres_operations import PostgresOperations
from web_service.storage import Missing, digest


@pytest.fixture
def operations(tmp_path):
    path, now = tmp_path / 'operations.sqlite3', [1000]
    with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).parents[1] / 'web_service' /
                          'postgresql_schema_draft.sql').read_text(encoding='utf-8'))
        db.executemany('INSERT INTO web_users(id,tenant_id,object_id,profile_blob,active) VALUES(?,?,?,?,?)',
                       [('alice', 'tenant', 'a', 'x', 1), ('bob', 'tenant', 'b', 'x', 1),
                        ('inactive', 'tenant', 'i', 'x', 0)])
        db.executemany('INSERT INTO web_sessions VALUES(?,?,?,?)',
                       [(digest('session-a'), 'alice', 'csrf-a', 9999),
                        (digest('session-b'), 'bob', 'csrf-b', 9999)])
    class Shim:
        def __init__(self, connection): self.db = connection
        def execute(self, sql, params=()):
            return self.db.execute(re.sub(r' FOR UPDATE(?: OF d)?', '', sql).replace('%s', '?'), params)
    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield Shim(connection)
        finally:
            connection.close()
    cipher = Fernet(Fernet.generate_key())
    seal = lambda value: cipher.encrypt(json.dumps(value).encode()).decode()
    unseal = lambda value: json.loads(cipher.decrypt(value.encode()))
    class Repository(PostgresOperations, PostgresDevices):
        pass
    repo = Repository(connect, seal, unseal, clock=lambda: now[0], audit_key=b'private-audit-key')
    alice, _ = repo.add_connection('session-a', 'alice', 'private-token', 3600, 'cache', background=True)
    bob, _ = repo.add_connection('session-b', 'bob', 'bob-token', 3600, 'cache', background=True)
    def execute(sql, params=()):
        with connect() as db:
            return db.execute(sql, params).fetchall()
    return SimpleNamespace(repo=repo, now=now, connect=connect, execute=execute,
                           alice=digest(alice), bob=digest(bob), seal=seal, unseal=unseal)


def test_rate_limit_atomically_counts_hashed_keys_and_reopens_next_minute(operations):
    f = operations
    assert f.repo.allow('api:alice', 2)
    assert f.repo.allow('api:alice', 2)
    assert not f.repo.allow('api:alice', 2)
    assert f.repo.allow('api:bob', 2)
    rows = f.execute('SELECT key_hash,attempts FROM web_security_limits ORDER BY attempts')
    assert sorted(row[1] for row in rows) == [1, 3]
    assert all('alice' not in row[0] and 'bob' not in row[0] for row in rows)
    f.now[0] += 60
    assert f.repo.allow('api:alice', 2)
    f.now[0] += 180
    assert f.repo.allow('api:alice', 2)
    assert len(f.execute('SELECT * FROM web_security_limits')) == 1
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            f.repo.allow('api:alice', invalid)


def test_audit_uses_secret_key_and_prunes_old_records(operations):
    f = operations
    f.execute('INSERT INTO web_security_audit VALUES(%s,%s,%s)',
              (1000 - 7 * 86400 - 1, 'old', 'fingerprint'))
    f.repo.audit('sync_failed', 'alice@example.invalid')
    row, = f.execute('SELECT * FROM web_security_audit')
    assert row == (1000, 'sync_failed', f.repo.fingerprint('alice@example.invalid'))
    assert 'alice' not in row[2]
    old_hash = row[2]
    f.repo._audit_key = b'another-secret'
    assert f.repo.fingerprint('alice@example.invalid') != old_hash
    del f.repo._audit_key
    with pytest.raises(RuntimeError, match='secret audit key'):
        f.repo.allow('key', 1)


def test_feedback_is_encrypted_and_rejects_inactive_or_missing_user(operations):
    f = operations
    key = f.repo.save_feedback('alice', 'private feedback')
    row, = f.execute('SELECT id,user_id,payload_blob,created_at FROM web_feedback')
    assert row[:2] == (key, 'alice') and row[3] == 1000
    assert 'private feedback' not in row[2]
    assert f.unseal(row[2]) == {'message': 'private feedback'}
    for user in ('inactive', 'unknown'):
        with pytest.raises(Missing):
            f.repo.save_feedback(user, 'forbidden')
    assert len(f.execute('SELECT * FROM web_feedback')) == 1


def test_due_devices_rechecks_expiry_active_user_and_deterministic_limit(operations):
    f = operations
    assert {row['id_hash'] for row in f.repo.due_devices()} == {f.alice, f.bob}
    f.execute('UPDATE web_connections SET next_sync=1001 WHERE id_hash=%s', (f.alice,))
    assert [row['id_hash'] for row in f.repo.due_devices(1)] == [f.bob]
    f.execute("UPDATE web_connections SET state='reauth_required' WHERE id_hash=%s", (f.bob,))
    assert f.repo.due_devices() == []
    f.now[0] = 1001
    f.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    assert f.repo.due_devices() == []
    f.execute("UPDATE web_users SET active=1 WHERE id='alice'")
    f.execute('UPDATE web_connections SET expires_at=1001 WHERE id_hash=%s', (f.alice,))
    assert f.repo.due_devices() == []
    with pytest.raises(ValueError): f.repo.due_devices(True)
    with pytest.raises(ValueError): f.repo.due_devices(1001)


def test_defer_and_ingest_schedule_only_current_live_devices(operations):
    f = operations
    assert f.repo.defer_device(f.alice, 60)
    assert f.repo.mark_ingested(f.alice)
    assert f.execute('SELECT next_sync,ingest_at FROM web_connections WHERE id_hash=%s',
                     (f.alice,)) == [(1060, 1300)]
    f.execute('UPDATE web_connections SET active=0 WHERE id_hash=%s', (f.alice,))
    assert not f.repo.defer_device(f.alice, 300)
    assert not f.repo.mark_ingested(f.alice)
    assert not f.repo.defer_device('unknown')
    with pytest.raises(ValueError): f.repo.defer_device(f.bob, True)
    assert f.execute('SELECT next_sync,ingest_at FROM web_connections WHERE id_hash=%s',
                     (f.alice,)) == [(1060, 1300)]


def test_expired_worker_owner_cannot_unlock_new_owner(operations):
    f = operations
    assert f.repo.worker_lock('fresh-owner-a')
    assert not f.repo.worker_lock('fresh-owner-b')
    f.now[0] += 600
    assert f.repo.worker_lock('fresh-owner-b')
    assert not f.repo.worker_unlock('fresh-owner-a')
    assert not f.repo.worker_lock('fresh-owner-c')
    assert f.execute('SELECT owner,expires_at FROM web_worker_lock') == [('fresh-owner-b', 2200)]
    assert f.repo.worker_unlock('fresh-owner-b')
    assert not f.repo.worker_unlock('fresh-owner-b')
    assert f.repo.worker_lock('fresh-owner-c')


def test_worker_expiry_uses_clock_after_row_lock_wait(operations, monkeypatch):
    f = operations
    assert f.repo.worker_lock('old-owner')
    original = f.repo.connect
    @contextmanager
    def delayed_connect():
        with original() as db:
            class Delayed:
                def execute(self, sql, params=()):
                    result = db.execute(sql, params)
                    if sql.startswith('SELECT expires_at FROM web_worker_lock'):
                        f.now[0] = 2000
                    return result
            yield Delayed()
    monkeypatch.setattr(f.repo, 'connect', delayed_connect)
    assert f.repo.worker_lock('new-owner')
    assert f.execute('SELECT owner,expires_at FROM web_worker_lock') == [('new-owner', 2600)]


def test_channel_cursor_rotates_restarts_and_rejects_inactive_user(operations):
    f = operations
    channels = [{'id': 'c'}, {'id': 'a'}, {'id': 'b'}]
    assert [row['id'] for row in f.repo.next_channels('alice', channels, 2)] == ['a', 'b']
    assert [row['id'] for row in f.repo.next_channels('alice', channels, 2)] == ['c', 'a']
    assert [row['id'] for row in f.repo.next_channels('alice', channels[:1], 10)] == ['c']
    assert f.repo.next_channels('alice', channels, 0) == []
    assert f.repo.next_channels('alice', [], 3) == []
    with pytest.raises(Missing): f.repo.next_channels('inactive', channels, 1)
    with pytest.raises(ValueError): f.repo.next_channels('alice', channels, -1)
    with pytest.raises(ValueError): f.repo.next_channels('alice', [{'id': 'x'}, {'id': 'x'}], 1)


def test_maintenance_revokes_expired_device_and_prunes_only_expired_records(operations):
    f = operations
    f.execute('UPDATE web_connections SET expires_at=1000 WHERE id_hash=%s', (f.alice,))
    f.execute("INSERT INTO web_channels VALUES('channel','tenant','label')")
    f.execute("INSERT INTO web_channel_access VALUES('alice','channel',0,9999)")
    f.execute('INSERT INTO web_push_subscriptions VALUES(%s,%s,%s,%s,%s)',
              ('push', f.alice, 'endpoint', 'encrypted', 0))
    f.execute("INSERT INTO web_deliveries(id,subscription_id,created_at) VALUES('delivery','push',0)")
    f.execute("INSERT INTO web_auth_flows VALUES('expired','encrypted',999)")
    f.execute("INSERT INTO web_auth_flows VALUES('live','encrypted',1001)")
    f.execute('INSERT INTO web_source_cache VALUES(%s,%s,%s,%s,%s,%s)',
              ('old', 'channel', 'm1', 'v', 'encrypted', 1000 - 90 * 86400 - 1))
    f.execute('INSERT INTO web_source_cache VALUES(%s,%s,%s,%s,%s,%s)',
              ('current', 'channel', 'm2', 'v', 'encrypted', 1000))
    f.repo.maintenance()
    assert f.repo.device(f.alice) is None and f.repo.device(f.bob) is not None
    active, version, blob = f.execute('SELECT active,version,credential_blob FROM web_connections WHERE id_hash=%s',
                                      (f.alice,))[0]
    assert active == 0 and version == 2 and f.unseal(blob) == {}
    assert f.execute('SELECT * FROM web_push_subscriptions') == []
    assert f.execute('SELECT * FROM web_deliveries') == []
    assert f.execute('SELECT * FROM web_channel_access') == []
    assert f.execute('SELECT id_hash FROM web_auth_flows') == [('live',)]
    assert f.execute('SELECT cache_key FROM web_source_cache') == [('current',)]
    assert f.execute('SELECT user_id FROM web_sessions') == [('bob',)]


def test_expiry_is_rechecked_under_device_lock_before_revocation(operations, monkeypatch):
    f = operations
    f.execute('UPDATE web_connections SET expires_at=1000 WHERE id_hash=%s', (f.alice,))
    original = f.repo._lock_device
    def renewed_before_lock(db, device_hash, user_id=None):
        # Model another transaction extending expiry after the maintenance
        # scan but before the user/device lock is acquired.
        db.execute('UPDATE web_connections SET expires_at=9999 WHERE id_hash=%s', (device_hash,))
        return original(db, device_hash, user_id)
    monkeypatch.setattr(f.repo, '_lock_device', renewed_before_lock)
    f.repo.maintenance()
    assert f.repo.device(f.alice) is not None
    assert f.execute('SELECT active,version FROM web_connections WHERE id_hash=%s', (f.alice,)) == [(1, 1)]
