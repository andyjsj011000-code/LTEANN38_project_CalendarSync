"""SQLite SQL-shim behavior checks; real PostgreSQL concurrency is not modeled."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_devices import PostgresDevices
from web_service.postgres_sync import PostgresSync
from web_service.storage import Missing, digest


@pytest.fixture
def sync(tmp_path):
    path, now = tmp_path / 'sync.sqlite3', [1000]
    db = sqlite3.connect(path)
    try:
        with db:
            db.executescript((Path(__file__).parents[1] / 'web_service' /
                              'postgresql_schema_draft.sql').read_text(encoding='utf-8'))
            db.executemany('INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(?,?,?,?)',
                           [('alice', 'tenant', 'a', 'x'), ('bob', 'other-tenant', 'b', 'x')])
            db.execute('INSERT INTO web_sessions VALUES(?,?,?,?)',
                       (digest('session'), 'alice', 'csrf', 9999))
    finally:
        db.close()
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
    def seal(value): return cipher.encrypt(json.dumps(value).encode()).decode()
    def unseal(value): return json.loads(cipher.decrypt(value.encode()))
    args = (connect, seal, unseal)
    devices = PostgresDevices(*args, clock=lambda: now[0])
    raw, _ = devices.add_connection('session', 'alice', 'token', 3600)
    repo = PostgresSync(*args, clock=lambda: now[0])
    def execute(sql, values=()):
        with connect() as connection:
            return connection.execute(sql, values).fetchall()
    return SimpleNamespace(repo=repo, devices=devices, device=devices.device(digest(raw)),
                           now=now, execute=execute, unseal=unseal, seal=seal)


def event(**values):
    return {'id': 'direct:event', 'channel_id': 'channel', 'title': 'Private event',
            'notes': 'private notes', 'start_date': '2026-11-05', 'end_date': '2026-11-05',
            'kind': 'assignment', 'source_version': 'v1', **values}


def commit(sync, events=None, channels=None):
    lease = sync.repo.begin_sync('alice', 0)
    assert lease
    sync.repo.commit_snapshot(sync.device,
                             channels if channels is not None else [{'id': 'channel', 'name': 'Class'}],
                             events if events is not None else [event()], 300, lease)
    return lease


def test_begin_sync_revokes_cached_access_and_hides_token(sync):
    sync.execute("INSERT INTO web_channels VALUES('old','tenant','x')")
    sync.execute("INSERT INTO web_channel_access VALUES('alice','old',0,9999)")
    lease = sync.repo.begin_sync('alice', 300)
    assert isinstance(lease, str) and len(lease) == 64
    assert sync.repo.sync_status('alice') == {'checked_at': 0, 'state': 'checking', 'lease_until': 1180}
    assert sync.execute('SELECT * FROM web_channel_access') == []
    assert sync.repo.begin_sync('alice', 0) is False
    assert sync.repo.begin_sync('unknown', 0) is False
    sync.execute("UPDATE web_users SET active=0 WHERE id='bob'")
    assert sync.repo.begin_sync('bob', 0) is False


def test_same_second_retry_has_distinct_token_and_fences_old_work(sync):
    old = sync.repo.begin_sync('alice', 0)
    assert sync.repo.fail_sync('alice', 'verification_failed', old)
    new = sync.repo.begin_sync('alice', 0)
    assert new != old
    assert not sync.repo.fail_sync('alice', 'verification_failed', old)
    with pytest.raises(Missing):
        sync.repo.commit_snapshot(sync.device, [], [], 300, old)
    sync.repo.commit_snapshot(sync.device, [], [], 300, new)
    assert sync.repo.sync_status('alice') == {'checked_at': 1000, 'state': 'connected', 'lease_until': 0}
    assert sync.execute("SELECT lease_token FROM web_school_sync WHERE user_id='alice'") == [(None,)]
    assert sync.repo.begin_sync('alice', 300) is False
    assert not sync.repo.fail_sync('alice', 'verification_failed', new)


def test_expired_lease_cannot_fail_or_commit_after_renewal(sync):
    old = sync.repo.begin_sync('alice', 0)
    sync.now[0] = 1180
    assert not sync.repo.fail_sync('alice', 'verification_failed', old)
    with pytest.raises(Missing):
        sync.repo.commit_snapshot(sync.device, [], [], 300, old)
    new = sync.repo.begin_sync('alice', 0)
    assert new != old
    with pytest.raises(Missing):
        sync.repo.commit_snapshot(sync.device, [], [], 300, old)
    sync.repo.commit_snapshot(sync.device, [], [], 300, new)


def test_snapshot_encryption_and_review_reversion_preserve_accepted_baseline(sync):
    commit(sync)
    row = sync.execute("SELECT payload_blob FROM web_events WHERE id='direct:event'")[0][0]
    assert 'Private event' not in row
    assert sync.unseal(row)['title'] == 'Private event'
    assert sync.execute("SELECT valid_until FROM web_channel_access WHERE user_id='alice'") == [(1300,)]
    commit(sync, [event(title='Updated title', source_version='v2')])
    changes = sync.execute('SELECT id,status,payload_blob FROM web_changes')
    assert len(changes) == 1 and changes[0][1] == 'pending'
    assert sync.unseal(changes[0][2])['title'] == 'Updated title'
    override = sync.execute("SELECT override_blob FROM web_event_states WHERE user_id='alice'")[0][0]
    assert sync.unseal(override)['title'] == 'Private event'
    commit(sync)
    assert sync.execute('SELECT status FROM web_changes') == [('superseded',)]
    sync.now[0] += 1
    commit(sync, [event(title='Updated title', source_version='v2')])
    assert sync.execute('SELECT id,status,created_at FROM web_changes') == [(changes[0][0], 'pending', 1001)]


@pytest.mark.parametrize('mutation', [
    "UPDATE web_connections SET active=0",
    "UPDATE web_connections SET expires_at=1000",
    "UPDATE web_connections SET state='reauth_required'",
    "UPDATE web_connections SET version=version+1",
    "UPDATE web_users SET active=0 WHERE id='alice'",
])
def test_device_or_user_change_fences_fetched_snapshot(sync, mutation):
    lease = sync.repo.begin_sync('alice', 0)
    sync.execute(mutation)
    with pytest.raises(Missing):
        sync.repo.commit_snapshot(sync.device, [{'id': 'channel', 'name': 'Class'}], [event()], 300, lease)
    assert sync.execute('SELECT * FROM web_events') == []
    assert sync.execute('SELECT * FROM web_channel_access') == []


@pytest.mark.parametrize('values', [{'user_id': 'bob'}, {'tenant_id': 'other-tenant'}, {'version': True}])
def test_snapshot_cannot_forge_device_owner_tenant_or_version(sync, values):
    lease = sync.repo.begin_sync('alice', 0)
    with pytest.raises(Missing):
        sync.repo.commit_snapshot({**sync.device, **values}, [], [], 300, lease)


def test_cross_tenant_channel_conflict_rolls_back_prior_writes(sync):
    sync.execute('INSERT INTO web_channels VALUES(%s,%s,%s)', ('z-foreign', 'other-tenant', 'foreign-label'))
    sync.execute('INSERT INTO web_channels VALUES(%s,%s,%s)', ('existing', 'tenant', 'existing-label'))
    lease = sync.repo.begin_sync('alice', 0)
    sync.execute("INSERT INTO web_channel_access VALUES('alice','existing',0,9999)")
    with pytest.raises(ValueError, match='tenant'):
        sync.repo.commit_snapshot(sync.device, [{'id': 'a-new', 'name': 'New'},
                                                {'id': 'z-foreign', 'name': 'Overwrite'}], [], 300, lease)
    assert sync.execute("SELECT id FROM web_channels WHERE id='a-new'") == []
    assert sync.execute("SELECT label_blob FROM web_channels WHERE id='z-foreign'") == [('foreign-label',)]
    assert sync.execute("SELECT channel_id FROM web_channel_access WHERE user_id='alice'") == [('existing',)]
    assert sync.repo.sync_status('alice')['state'] == 'checking'


def test_existing_event_cannot_move_channels(sync):
    commit(sync)
    lease = sync.repo.begin_sync('alice', 0)
    with pytest.raises(ValueError, match='Event channel'):
        sync.repo.commit_snapshot(sync.device, [{'id': 'other', 'name': 'Other'}],
                                  [event(channel_id='other')], 300, lease)
    assert sync.execute("SELECT channel_id FROM web_events WHERE id='direct:event'") == [('channel',)]
    assert sync.execute("SELECT id FROM web_channels WHERE id='other'") == []


def test_unlisted_source_channel_and_foreign_tenant_claim_are_rejected(sync):
    lease = sync.repo.begin_sync('alice', 0)
    with pytest.raises(ValueError, match='channel'):
        sync.repo.commit_snapshot(sync.device, [], [event()], 300, lease)
    with pytest.raises(Missing):
        sync.repo.commit_snapshot(sync.device, [{'id': 'channel', 'name': 'Class',
                                                'tenant_id': 'other-tenant'}], [event()], 300, lease)
    assert sync.execute('SELECT * FROM web_events') == []


def test_shared_row_wait_cannot_commit_after_lease_or_access_expiry(sync):
    lease=sync.repo.begin_sync('alice',0)
    ticks=iter([1000,1180])
    sync.repo.clock=lambda:next(ticks)
    with pytest.raises(Missing):
        sync.repo.commit_snapshot(sync.device,[{'id':'channel','name':'Class'}],[event()],300,lease)
    assert sync.execute('SELECT * FROM web_events')==[]
    assert sync.execute('SELECT * FROM web_channel_access')==[]
