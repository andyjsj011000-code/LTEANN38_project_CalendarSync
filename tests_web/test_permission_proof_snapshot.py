"""Single-statement proof snapshots; PostgreSQL SQL is exercised by a SQLite shim."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_sync import PostgresSync
from web_service.test_storage import TestDatabase


EMPTY = {'state': 'not_connected', 'checked_at': 0, 'channels': frozenset()}


@pytest.fixture(params=['sqlite', 'postgres-shim'])
def proof_store(request, tmp_path):
    now = [1000]
    storage = TestDatabase(tmp_path / 'proof.sqlite3', Fernet.generate_key().decode(),
                           clock=lambda: now[0])
    storage.initialize()
    alice = storage.save_user('school', 'alice', {'name': 'Alice'})
    bob = storage.save_user('school', 'bob', {'name': 'Bob'})
    other = storage.save_user('other-school', 'other', {'name': 'Other'})
    original_connect = storage.connect
    with original_connect() as db:
        db.execute('''INSERT INTO web_school_sync(user_id,checked_at,state,lease_until)
            VALUES(?,990,'connected',0)''', (alice,))
        for channel, tenant in [('live-a', 'school'), ('live-b', 'school'),
                                ('expired', 'school'), ('foreign', 'other-school'),
                                ('bob-only', 'school')]:
            db.execute('INSERT INTO web_channels(id,tenant_id,label_blob) VALUES(?,?,?)',
                       (channel, tenant, storage.seal({'name': channel})))
        db.executemany('INSERT INTO web_channel_access VALUES(?,?,?,?)', [
            (alice, 'live-a', 990, 1060), (alice, 'live-b', 990, 1200),
            (alice, 'expired', 990, 1000), (alice, 'foreign', 990, 1200),
            (bob, 'bob-only', 990, 1200)])

    queries, connections = [], []
    class ReadOnlyConnection:
        def __init__(self, db): self.db = db
        def execute(self, sql, values=()):
            assert sql.lstrip().upper().startswith('SELECT ')
            queries.append((sql, values))
            return self.db.execute(sql.replace('%s', '?'), values)
    @contextmanager
    def read_connect():
        connections.append(True)
        with original_connect() as db:
            yield ReadOnlyConnection(db)

    if request.param == 'sqlite':
        storage.connect = read_connect
        repo = storage
    else:
        repo = PostgresSync(read_connect, storage.seal, storage.unseal, clock=lambda: now[0])
    def execute(sql, values=()):
        with original_connect() as db:
            return [tuple(row) for row in db.execute(sql, values)]
    return SimpleNamespace(repo=repo, alice=alice, bob=bob, other=other, now=now,
                           execute=execute, queries=queries, connections=connections)


def test_reads_only_current_same_tenant_channels_without_mutating_proof(proof_store):
    s = proof_store
    before = s.execute('SELECT * FROM web_channel_access ORDER BY user_id,channel_id')
    state = s.execute('SELECT * FROM web_school_sync ORDER BY user_id')
    assert s.repo.teams_read_proof(s.alice, 'school') == {
        'state': 'connected', 'checked_at': 990, 'channels': frozenset({'live-a', 'live-b'})}
    assert len(s.connections) == 1 and len(s.queries) == 1
    assert s.execute('SELECT * FROM web_channel_access ORDER BY user_id,channel_id') == before
    assert s.execute('SELECT * FROM web_school_sync ORDER BY user_id') == state


def test_exact_expiry_removes_scope_without_renewing_checked_at(proof_store):
    s = proof_store
    s.now[0] = 1060
    assert s.repo.teams_read_proof(s.alice, 'school') == {
        'state': 'connected', 'checked_at': 990, 'channels': frozenset({'live-b'})}
    s.now[0] = 1200
    assert s.repo.teams_read_proof(s.alice, 'school') == {
        'state': 'connected', 'checked_at': 990, 'channels': frozenset()}
    assert s.execute('SELECT valid_until FROM web_channel_access WHERE user_id=? AND channel_id=?',
                     (s.alice, 'live-a')) == [(1060,)]


def test_successful_zero_channel_snapshot_keeps_its_original_status(proof_store):
    s = proof_store
    s.execute('DELETE FROM web_channel_access WHERE user_id=?', (s.alice,))
    assert s.repo.teams_read_proof(s.alice, 'school') == {
        'state': 'connected', 'checked_at': 990, 'channels': frozenset()}
    assert len(s.connections) == 1 and len(s.queries) == 1


@pytest.mark.parametrize('invalid', ['wrong-tenant', 'missing', 'inactive'])
def test_invalid_user_does_not_disclose_status_or_scope(proof_store, invalid):
    s = proof_store
    user, tenant = s.alice, 'school'
    if invalid == 'wrong-tenant': tenant = 'other-school'
    elif invalid == 'missing': user = 'unknown-user'
    else: s.execute('UPDATE web_users SET active=0 WHERE id=?', (s.alice,))
    assert s.repo.teams_read_proof(user, tenant) == EMPTY
    assert len(s.connections) == 1 and len(s.queries) == 1


def test_absent_sync_row_does_not_create_one(proof_store):
    s = proof_store
    assert s.repo.teams_read_proof(s.other, 'other-school') == EMPTY
    assert s.execute('SELECT 1 FROM web_school_sync WHERE user_id=?', (s.other,)) == []
    assert len(s.connections) == 1 and len(s.queries) == 1


def test_checking_retains_only_the_original_unexpired_scope(proof_store):
    s = proof_store
    s.execute("UPDATE web_school_sync SET state='checking',lease_until=1180 WHERE user_id=?", (s.alice,))
    assert s.repo.teams_read_proof(s.alice, 'school') == {
        'state': 'checking', 'checked_at': 990, 'channels': frozenset({'live-a', 'live-b'})}
    assert s.execute('SELECT checked_at,lease_until FROM web_school_sync WHERE user_id=?',
                     (s.alice,)) == [(990, 1180)]
