"""Real PostgreSQL evidence for cross-account channel leases and late writers."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from test_postgres_server_integration import postgres_server
from web_service.postgres_shared_schema import enable_shared_channel_schema
from web_service.storage import digest


@pytest.fixture
def shared(postgres_server):
    server = postgres_server
    enable_shared_channel_schema(server.db.connect, server.settings.tenant_id)
    server.db.shared_ingestion_enabled = True
    return server


def channel():
    return {'id': 'shared-class', 'team_id': 'verified-team', 'name': 'School · General'}


def test_additive_schema_is_idempotent_and_preserves_existing_rows(shared):
    s = shared
    c = s.client()
    with s.db.connect() as db:
        before = db.execute('SELECT id,profile_blob FROM web_users ORDER BY id').fetchall()
    enable_shared_channel_schema(s.db.connect, s.settings.tenant_id)
    with s.db.connect() as db:
        assert db.execute('SELECT id,profile_blob FROM web_users ORDER BY id').fetchall() == before
    assert c.get('/api/me').status_code == 200


def test_shared_channel_only_one_collector_and_empty_success_is_fresh(shared):
    s, ch = shared, channel()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: s.db.claim_channel_ingestion(ch), range(2)))
    owners = [lease for lease in claims if lease]
    assert len(owners) == 1
    assert not s.db.channel_ingestion_fresh(ch)
    assert s.db.finish_channel_ingestion(ch, owners[0], True)
    assert s.db.channel_ingestion_fresh(ch)
    assert s.db.claim_channel_ingestion(ch) is None
    assert s.db.catalog_events([ch['id']]) == []
    s.now[0] += 301
    assert s.db.claim_channel_ingestion(ch)


def test_expired_collector_cannot_replace_or_touch_newer_analysis(shared):
    s, ch = shared, channel()
    old = s.db.claim_channel_ingestion(ch)
    key = digest(ch['id'] + '|post')
    first = {'channel_id': ch['id'], 'id': 'direct:one', 'title': 'First'}
    assert s.db.cache_analysis(key, ch['id'], 'post', 'v1', [first], ingestion_lease=old)
    s.now[0] += 181
    new = s.db.claim_channel_ingestion(ch)
    latest = {**first, 'title': 'Newer'}
    assert s.db.cache_analysis(key, ch['id'], 'post', 'v2', [latest], ingestion_lease=new)
    assert s.db.cache_analysis(key, ch['id'], 'post', 'v1', [first], ingestion_lease=old) is False
    assert s.db.touch_analysis(key, ingestion_lease=old) is False
    assert s.db.finish_channel_ingestion(ch, old, True) is False
    assert s.db.cached_analysis(key, 'v2') == [latest]
    assert s.db.finish_channel_ingestion(ch, new, True)


def test_failed_scan_remains_due_and_channel_identity_is_fixed(shared):
    s, ch = shared, channel()
    lease = s.db.claim_channel_ingestion(ch)
    assert s.db.finish_channel_ingestion(ch, lease, False)
    assert not s.db.channel_ingestion_fresh(ch)
    assert s.db.claim_channel_ingestion(ch)
    with pytest.raises(ValueError):
        s.db.claim_channel_ingestion({**ch, 'team_id': 'other-team'})
    with pytest.raises(ValueError):
        s.db.claim_channel_ingestion({**ch, 'tenant_id': 'other-school'})


@pytest.mark.parametrize('operation', ['cache_write', 'touch'])
def test_lease_expiry_during_cache_row_lock_wait_prevents_late_mutation(shared, monkeypatch, operation):
    s, ch = shared, channel()
    lease = s.db.claim_channel_ingestion(ch)
    key = digest(ch['id'] + '|blocked-post')
    prior = {'channel_id': ch['id'], 'id': 'direct:blocked', 'title': 'Prior analysis'}
    assert s.db.cache_analysis(key, ch['id'], 'blocked-post', 'v1', [prior], ingestion_lease=lease)

    first_check = Event()
    checks = []
    active_lease = s.db._active_channel_lease
    def observed_check(db, channel_id, supplied_lease):
        valid = active_lease(db, channel_id, supplied_lease)
        checks.append(valid)
        if len(checks) == 1:
            first_check.set()
        return valid
    monkeypatch.setattr(s.db, '_active_channel_lease', observed_check)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with s.db.connect() as blocker:
            before = blocker.execute('''SELECT version,payload_blob,updated_at FROM web_source_cache
                WHERE cache_key=%s FOR UPDATE''', (key,)).fetchone()
            if operation == 'cache_write':
                pending = pool.submit(s.db.cache_analysis, key, ch['id'], 'blocked-post', 'v2',
                                      [{**prior, 'title': 'Late replacement'}], ingestion_lease=lease)
            else:
                pending = pool.submit(s.db.touch_analysis, key, ingestion_lease=lease)
            try:
                assert first_check.wait(timeout=3), 'Collector must validate its initial live lease'
                assert checks[0] is True
                # The collector passed the first check but cannot acquire the
                # cache row until this transaction releases it.
                s.now[0] += 181
            finally:
                blocker.commit()
        assert pending.result(timeout=5) is False

    assert checks[-1] is False
    with s.db.connect() as db:
        assert db.execute('''SELECT version,payload_blob,updated_at FROM web_source_cache
            WHERE cache_key=%s''', (key,)).fetchone() == before
    assert s.db.cached_analysis(key, 'v1') == [prior]
    assert s.db.finish_channel_ingestion(ch, lease, True) is False
    assert not s.db.channel_ingestion_fresh(ch)
