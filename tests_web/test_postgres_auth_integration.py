"""Opt-in; the fixture only accepts an explicitly disposable PostgreSQL DB."""
import json
from concurrent.futures import ThreadPoolExecutor

from cryptography.fernet import Fernet
import pytest

from test_postgres_personal_tasks_integration import postgres_tasks
from test_postgres_database_integration import postgres_database, CHANNELS, source
from web_service.postgres_auth_store import PostgresAuthStore
from web_service.storage import Missing, digest


def test_oauth_flow_has_one_winner_when_callbacks_race(postgres_tasks):
    _, connect = postgres_tasks
    cipher = Fernet(Fernet.generate_key())
    def seal(payload): return cipher.encrypt(json.dumps(payload).encode()).decode()
    def unseal(blob): return json.loads(cipher.decrypt(blob.encode()))
    store = PostgresAuthStore(connect, seal, unseal, lambda: 1000)
    raw = store.store_flow({'nonce': 'test-nonce'})
    def consume(_):
        try:
            return store.consume_flow(raw)
        except Missing:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))
    assert sum(result is not None for result in results) == 1
    assert next(result for result in results if result is not None)['nonce'] == 'test-nonce'


def two_devices_with_cached_snapshot(p):
    user = p.users['alice']
    raw_session = p.repo.issue_session(user, 3600)
    raw_device, _ = p.repo.add_connection(raw_session, user, 'second-test-token', 3600,
                                         'second-test-cache', background=True)
    second = p.repo.device(digest(raw_device))
    event = source('Cached deadline')
    p.repo.cache_analysis(digest('class|cached'), 'class', 'cached', 'v1', [event])
    p.repo.shared_ingestion_enabled = True
    lease = p.repo.begin_sync(user, 0, preserve_access=True)
    p.repo.commit_snapshot(p.devices['alice'], CHANNELS, [event], 60, lease)
    assert p.repo.channels(user, 'tenant')
    return raw_session, second, event


@pytest.mark.parametrize('operation', ['revoke_device', 'fail_connection'])
def test_other_device_can_reverify_immediately_after_device_access_is_removed(postgres_database, operation):
    p = postgres_database
    raw_session, second, event = two_devices_with_cached_snapshot(p)
    user = p.users['alice']
    original = p.devices['alice']
    getattr(p.repo, operation)(original['id_hash'])
    assert p.repo.school_session(raw_session, 1800) is not None
    assert p.repo.device(second['id_hash'])['state'] == 'connected'
    assert p.repo.channels(user, 'tenant') == []
    assert p.repo.sync_status(user)['checked_at'] == 0
    # Model the next request's normal permission check, with no TTL wait.
    lease = p.repo.begin_sync(user, 60, preserve_access=True)
    assert lease
    with pytest.raises(Missing):
        p.repo.commit_snapshot(original, CHANNELS, [event], 60, lease)
    p.repo.commit_snapshot(second, CHANNELS, [event], 60, lease)
    assert [item['title'] for item in p.repo.calendar(user, 'tenant', '2026-11-05', '2026-11-06')] == ['Cached deadline']


@pytest.mark.parametrize('operation', ['revoke_device', 'fail_connection'])
def test_revoking_one_device_cannot_steal_another_devices_active_snapshot_lease(postgres_database, operation):
    p = postgres_database
    _, second, event = two_devices_with_cached_snapshot(p)
    user = p.users['alice']
    lease = p.repo.begin_sync(user, 0, preserve_access=True)
    before = p.repo.sync_status(user)
    getattr(p.repo, operation)(p.devices['alice']['id_hash'])
    assert p.repo.sync_status(user) == {**before, 'checked_at': 0}
    with p.connect() as db:
        assert db.execute('SELECT lease_token FROM web_school_sync WHERE user_id=%s', (user,)).fetchone()[0] == lease
    assert p.repo.begin_sync(user, 60, preserve_access=True) is False
    p.repo.commit_snapshot(second, CHANNELS, [event], 60, lease)
    assert p.repo.channels(user, 'tenant')
