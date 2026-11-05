"""Behavioral tests using a SQLite SQL shim; PostgreSQL locking is not modeled."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_devices import PostgresDevices
from web_service.storage import Missing, digest


@pytest.fixture
def devices(tmp_path):
    path = tmp_path / 'devices.sqlite3'
    ddl = (Path(__file__).parents[1] / 'web_service' / 'postgresql_schema_draft.sql').read_text(encoding='utf-8')
    now = [1000]
    with sqlite3.connect(path) as db:
        db.executescript(ddl)
        db.executemany('INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(?,?,?,?)',
                       [('alice', 'tenant', 'a', 'x'), ('bob', 'tenant', 'b', 'x')])
        db.executemany('INSERT INTO web_sessions VALUES(?,?,?,?)',
                       [(digest('session-a'), 'alice', 'csrf-a', 9999),
                        (digest('session-b'), 'bob', 'csrf-b', 9999)])
        db.execute("INSERT INTO web_channels VALUES('channel','tenant','x')")
        db.execute("INSERT INTO web_channel_access VALUES('alice','channel',0,9999)")
    class Shim:
        def __init__(self, db): self.db = db
        def execute(self, sql, params=()):
            sql = re.sub(r' FOR UPDATE(?: OF d)?', '', sql)
            return self.db.execute(sql.replace('%s', '?'), params)
    @contextmanager
    def connect():
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys=ON')
            yield Shim(db)
    cipher = Fernet(Fernet.generate_key())
    def seal(value): return cipher.encrypt(json.dumps(value).encode()).decode()
    def unseal(blob): return json.loads(cipher.decrypt(blob.encode()))
    return PostgresDevices(connect, seal, unseal, clock=lambda: now[0]), path, now


def add(repo):
    raw, lifetime = repo.add_connection('session-a', 'alice', 'private-token', 3600,
                                        cache='private-cache', background=True)
    assert lifetime == 30 * 86400
    return digest(raw)


def test_connection_is_linked_to_its_actual_session_owner_and_credentials_encrypted(devices):
    repo, path, _ = devices
    with pytest.raises(Missing):
        repo.add_connection('session-b', 'alice', 'token', 3600)
    key = add(repo)
    device = repo.device(key)
    assert device['user_id'] == 'alice' and device['tenant_id'] == 'tenant'
    assert 'private-token' not in device['credential_blob']
    assert repo.unseal(device['credential_blob'])['cache'] == 'private-cache'
    assert repo.session_device({'id_hash': digest('session-a'), 'user_id': 'alice'})['id_hash'] == key
    with pytest.raises(Missing):
        repo.session_device({'id_hash': digest('session-a'), 'user_id': 'bob'})


def test_expired_or_superseded_refresh_cannot_overwrite_credentials(devices):
    repo, _, now = devices
    key = add(repo)
    old = repo.lock_refresh(key)
    assert old and repo.lock_refresh(key) is None
    assert not repo.save_refresh(key, 'wrong', 'wrong-token', 3600, 'cache')
    now[0] += 31
    assert not repo.save_refresh(key, old, 'late-token', 3600, 'cache')
    new = repo.lock_refresh(key)
    assert new and new != old
    repo.fail_connection(key, old)
    assert repo.device(key)['state'] == 'connected'
    assert repo.save_refresh(key, new, 'updated-token', 3600, 'updated-cache')
    assert repo.unseal(repo.device(key)['credential_blob'])['token'] == 'updated-token'


def test_revocation_clears_access_sessions_push_and_blocks_late_refresh(devices):
    repo, path, _ = devices
    key = add(repo)
    lease = repo.lock_refresh(key)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO web_push_subscriptions VALUES(?,?,?,?,?)', ('push', key, 'endpoint', 'encrypted', 1000))
    repo.revoke_device(key, 'bob')
    assert repo.device(key) is not None
    repo.revoke_device(key, 'alice')
    assert repo.device(key) is None
    assert not repo.save_refresh(key, lease, 'late-token', 3600, 'cache')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM web_channel_access WHERE user_id=?', ('alice',)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM web_push_subscriptions WHERE device_hash=?', (key,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM web_sessions WHERE id_hash=?', (digest('session-a'),)).fetchone()[0] == 0
        blob = db.execute('SELECT credential_blob FROM web_connections WHERE id_hash=?', (key,)).fetchone()[0]
    assert repo.unseal(blob) == {}


def test_current_refresh_failure_requires_reauthentication(devices):
    repo, _, _ = devices
    key = add(repo)
    lease = repo.lock_refresh(key)
    repo.fail_connection(key, lease)
    assert repo.device(key)['state'] == 'reauth_required'
    assert repo.device(key)['version'] == 2
    assert repo.lock_refresh(key) is None
    with pytest.raises(Missing):
        repo.session_device({'id_hash': digest('session-a'), 'user_id': 'alice'})


def test_second_connection_for_same_session_rolls_back_new_credentials(devices):
    repo, path, _ = devices
    key = add(repo)
    with pytest.raises(sqlite3.IntegrityError):
        add(repo)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM web_connections').fetchone()[0] == 1
    assert repo.session_device({'id_hash': digest('session-a'), 'user_id': 'alice'})['id_hash'] == key


def test_expired_session_or_inactive_user_cannot_create_connection(devices):
    repo, path, now = devices
    now[0] = 9999
    with pytest.raises(Missing):
        repo.add_connection('session-a', 'alice', 'token', 3600)
    now[0] = 1000
    with sqlite3.connect(path) as db:
        db.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    with pytest.raises(Missing):
        repo.add_connection('session-a', 'alice', 'token', 3600)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM web_connections').fetchone()[0] == 0


def test_user_deactivation_blocks_existing_device_read_and_refresh(devices):
    repo, path, _ = devices
    key = add(repo)
    lease = repo.lock_refresh(key)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    assert repo.device(key) is None
    assert repo.lock_refresh(key) is None
    assert not repo.save_refresh(key, lease, 'new-token', 3600, 'cache')
    with pytest.raises(Missing):
        repo.session_device({'id_hash': digest('session-a'), 'user_id': 'alice'})


def test_school_session_touches_view_only_and_rechecks_its_real_device(devices):
    repo, path, now = devices
    key = add(repo)
    expires = repo.device(key)['expires_at']
    now[0] += 40
    session = repo.school_session('session-a', 60)
    assert session == {'id_hash': digest('session-a'), 'user_id': 'alice',
                       'csrf_token': 'csrf-a', 'expires_at': 9999,
                       'tenant_id': 'tenant', 'object_id': 'a', 'profile_blob': 'x'}
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT last_seen FROM web_session_links WHERE session_hash=?',
                          (digest('session-a'),)).fetchone()[0] == 1040
    assert repo.device(key)['expires_at'] == expires
    now[0] += 40
    assert repo.school_session('session-a', 60) is not None
    assert repo.device(key)['expires_at'] == expires


@pytest.mark.parametrize('mutation', [
    "UPDATE web_connections SET active=0 WHERE user_id='alice'",
    "UPDATE web_connections SET expires_at=1000 WHERE user_id='alice'",
    "UPDATE web_connections SET state='reauth_required' WHERE user_id='alice'",
    "UPDATE web_users SET active=0 WHERE id='alice'",
    "UPDATE web_sessions SET expires_at=1000 WHERE user_id='alice'",
    "UPDATE web_session_links SET last_seen=940",
    "DELETE FROM web_session_links",
])
def test_school_session_commits_invalid_session_deletion(devices, mutation):
    repo, path, _ = devices
    key = add(repo)
    with sqlite3.connect(path) as db:
        db.execute(mutation)
    assert repo.school_session('session-a', 60) is None
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM web_sessions WHERE id_hash=?',
                          (digest('session-a'),)).fetchone()[0] == 0
        # An expired screen session alone must not erase encrypted device data.
        assert repo.unseal(db.execute('SELECT credential_blob FROM web_connections WHERE id_hash=?',
                                     (key,)).fetchone()[0])['cache'] == 'private-cache'
        assert db.execute('SELECT count(*) FROM web_sessions WHERE user_id=?',
                          ('bob',)).fetchone()[0] == 1


def test_school_session_never_uses_device_belonging_to_another_user(devices):
    repo, path, _ = devices
    key = add(repo)
    bob_raw, _ = repo.add_connection('session-b', 'bob', 'bob-token', 3600)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE web_session_links SET device_hash=? WHERE session_hash=?',
                   (digest(bob_raw), digest('session-a')))
    assert repo.school_session('session-a', 60) is None
    assert repo.school_session('session-b', 60)['user_id'] == 'bob'
    assert repo.device(key) is not None


def test_revoke_all_clears_every_owned_connection_but_preserves_other_user(devices):
    repo, path, _ = devices
    first = add(repo)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO web_sessions VALUES(?,?,?,?)',
                   (digest('session-a2'), 'alice', 'csrf-a2', 9999))
    raw_second, _ = repo.add_connection('session-a2', 'alice', 'second-token', 3600,
                                        cache='second-cache', background=True)
    second = digest(raw_second)
    raw_bob, _ = repo.add_connection('session-b', 'bob', 'bob-token', 3600)
    bob = digest(raw_bob)
    leases = {key: repo.lock_refresh(key) for key in (first, second)}
    with sqlite3.connect(path) as db:
        for index, key in enumerate((first, second, bob)):
            db.execute('INSERT INTO web_push_subscriptions VALUES(?,?,?,?,?)',
                       ('push-' + str(index), key, 'endpoint-' + str(index), 'encrypted', 1000))
        db.execute('INSERT INTO web_school_sync(user_id,state,lease_until,lease_token) VALUES(?,?,?,?)',
                   ('alice', 'checking', 1180, 'old-sync'))
    repo.revoke_all('alice')
    assert repo.device(first) is None and repo.device(second) is None
    assert repo.device(bob)['user_id'] == 'bob'
    for key, lease in leases.items():
        assert not repo.save_refresh(key, lease, 'late-token', 3600, 'late-cache')
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM web_sessions WHERE user_id='alice'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM web_channel_access WHERE user_id='alice'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM web_school_sync WHERE user_id='alice'").fetchone()[0] == 0
        assert db.execute('SELECT device_hash FROM web_push_subscriptions').fetchall() == [(bob,)]
        rows = db.execute('SELECT active,credential_blob,refresh_lease,refresh_until,token_expires,version '
                          "FROM web_connections WHERE user_id='alice'").fetchall()
    assert len(rows) == 2
    assert all(row[0] == 0 and repo.unseal(row[1]) == {} and row[2:] == (None, 0, 0, 2)
               for row in rows)
    with pytest.raises(Missing):
        repo.add_connection('session-a', 'alice', 'old-session-token', 3600)
    # A new authentication ordered after logout is a separate allowed login.
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO web_sessions VALUES(?,?,?,?)',
                   (digest('fresh-session'), 'alice', 'fresh-csrf', 9999))
    raw, _ = repo.add_connection('fresh-session', 'alice', 'fresh-token', 3600)
    assert repo.device(digest(raw)) is not None


def test_revoke_all_rolls_back_session_and_push_deletes_on_storage_failure(devices):
    repo, path, _ = devices
    key = add(repo)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO web_push_subscriptions VALUES(?,?,?,?,?)',
                   ('push', key, 'endpoint', 'encrypted', 1000))
        db.execute("""CREATE TRIGGER fail_revocation BEFORE UPDATE ON web_connections
            BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        repo.revoke_all('alice')
    assert repo.device(key) is not None
    assert repo.school_session('session-a', 60) is not None
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT device_hash FROM web_push_subscriptions').fetchall() == [(key,)]
        assert db.execute("SELECT count(*) FROM web_channel_access WHERE user_id='alice'").fetchone()[0] == 1


@pytest.mark.parametrize('raw', [None, '', 'x' * 129, 123])
def test_school_session_rejects_malformed_cookie_without_using_digest(devices, raw):
    repo, _, _ = devices
    assert repo.school_session(raw, 60) is None
