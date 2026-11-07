"""SQLite publication mirrors the existing permission and review boundaries."""
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.storage import Missing, PermissionPending, digest
from web_service.test_storage import TestDatabase


@pytest.fixture
def progress_db(tmp_path):
    now = [2000000000]
    repo = TestDatabase(tmp_path / 'progress.sqlite3', Fernet.generate_key().decode(),
                        clock=lambda: now[0])
    repo.shared_ingestion_enabled = True
    repo.initialize()
    user = repo.save_user('tenant', 'alice', {'name': 'Alice'})
    raw = repo.issue_session(user, 3600)
    connection, _ = repo.add_connection(raw, user, 'test-token', 3600)
    device = repo.device(digest(connection))
    channels = [{'id': 'class', 'name': 'Class'}]
    lease = repo.begin_sync(user, 0)
    repo.commit_snapshot(device, channels, [], 60, lease)
    return SimpleNamespace(repo=repo, now=now, user=user, device=device, channels=channels)


def event(identity='first', channel='class', version='v1'):
    return {'id': 'direct:' + identity, 'channel_id': channel, 'title': 'Deadline ' + identity,
            'notes': '', 'start_date': '2026-11-07', 'end_date': '2026-11-07',
            'time': '12:00', 'kind': 'assignment', 'cancelled': False,
            'source_url': 'https://teams.microsoft.com/', 'source_version': version}


def cache(p, item):
    p.repo.cache_analysis(digest(item['channel_id'] + '|' + item['id']), item['channel_id'],
                          item['id'], item['source_version'], [item])


def snapshot_metadata(p):
    with p.repo.connect() as db:
        return (tuple(tuple(row) for row in db.execute('SELECT * FROM web_channel_access')),
                tuple(tuple(row) for row in db.execute('SELECT * FROM web_school_sync')),
                tuple(db.execute('SELECT version,ingest_at,next_sync,expires_at FROM web_connections').fetchone()))


def published(p):
    with p.repo.connect() as db:
        return [row[0] for row in db.execute('SELECT id FROM web_events ORDER BY id')]


def test_posts_appear_incrementally_without_renewing_permissions(progress_db):
    p = progress_db
    original = snapshot_metadata(p)
    cache(p, event())
    cache(p, event('not-allowed', 'another-class'))
    assert published(p) == []
    assert p.repo.materialize_cached_events(p.device) == 1
    assert published(p) == ['direct:first']
    p.now[0] += 5
    cache(p, event('second'))
    assert p.repo.materialize_cached_events(p.device) == 2
    assert published(p) == ['direct:first', 'direct:second']
    assert snapshot_metadata(p) == original


def test_existing_check_keeps_its_lease_and_original_acl(progress_db):
    p = progress_db
    cache(p, event())
    assert p.repo.begin_sync(p.user, 0, preserve_access=True)
    original = snapshot_metadata(p)
    assert p.repo.materialize_cached_events(p.device) == 1
    assert snapshot_metadata(p) == original
    assert p.repo.sync_status(p.user)['state'] == 'checking'


def test_expired_channel_is_excluded_while_other_proof_remains_valid(progress_db):
    p = progress_db
    with p.repo.connect() as db:
        db.execute('INSERT INTO web_channels VALUES(?,?,?)',
                   ('old-class', 'tenant', p.repo.seal({'name': 'Old class'})))
        db.execute('INSERT INTO web_channel_access VALUES(?,?,?,?)',
                   (p.user, 'old-class', p.now[0] - 60, p.now[0]))
    cache(p, event())
    cache(p, event('expired-channel', 'old-class'))
    before = snapshot_metadata(p)
    assert p.repo.materialize_cached_events(p.device) == 1
    assert published(p) == ['direct:first']
    assert snapshot_metadata(p) == before


def test_repeated_publication_preserves_user_state_and_change_review(progress_db):
    p = progress_db
    original = event()
    cache(p, original)
    p.repo.materialize_cached_events(p.device)
    p.repo.event_state(p.user, 'tenant', original['id'], {'hidden': True, 'completed': True})
    with p.repo.connect() as db:
        db.execute('UPDATE web_event_states SET override_blob=? WHERE user_id=? AND event_id=?',
                   (p.repo.seal({**original, 'title': 'My title'}), p.user, original['id']))
    updated = {**original, 'title': 'Changed source', 'source_version': 'v2'}
    cache(p, updated)
    p.repo.materialize_cached_events(p.device)
    p.now[0] += 1
    p.repo.materialize_cached_events(p.device)
    visible = p.repo._shared(p.user, 'tenant')[0]
    assert visible['title'] == 'My title' and visible['hidden'] and visible['completed']
    changes = p.repo.changes(p.user, 'tenant')
    assert len(changes) == 1 and changes[0]['after']['title'] == 'Changed source'
    p.repo.resolve_change(p.user, 'tenant', changes[0]['id'], False)
    p.repo.materialize_cached_events(p.device)
    assert p.repo.changes(p.user, 'tenant') == []
    assert p.repo._shared(p.user, 'tenant')[0]['title'] == 'My title'


@pytest.mark.parametrize('change', ['version', 'tenant', 'inactive', 'expired', 'reauth'])
def test_stale_or_invalid_device_cannot_publish(progress_db, change):
    p = progress_db
    cache(p, event())
    device = dict(p.device)
    with p.repo.connect() as db:
        if change == 'version':
            db.execute('UPDATE web_connections SET version=version+1')
        elif change == 'tenant':
            device['tenant_id'] = 'another-tenant'
        elif change == 'inactive':
            db.execute('UPDATE web_users SET active=0')
        elif change == 'expired':
            db.execute('UPDATE web_connections SET expires_at=?', (p.now[0],))
        else:
            db.execute("UPDATE web_connections SET state='reauth_required'")
    with pytest.raises(Missing):
        p.repo.materialize_cached_events(device)
    assert not p.repo.ingestion_pending(device)
    assert published(p) == []


def test_expired_acl_is_pending_and_failure_does_not_restore_it(progress_db):
    p = progress_db
    cache(p, event())
    p.now[0] += 60
    with pytest.raises(PermissionPending):
        p.repo.materialize_cached_events(p.device)
    assert published(p) == []
    lease = p.repo.begin_sync(p.user, 0, preserve_access=True)
    p.repo.fail_sync(p.user, 'verification_failed', lease)
    assert p.repo.materialize_cached_events(p.device) == 0
    assert not p.repo.ingestion_pending(p.device)
    assert not p.repo.channels(p.user, 'tenant')
    assert published(p) == []


@pytest.mark.parametrize('expiry', ['permission', 'device'])
def test_expiry_during_publication_rolls_back_all_rows(progress_db, monkeypatch, expiry):
    p = progress_db
    cache(p, event())
    if expiry == 'device':
        with p.repo.connect() as db:
            db.execute('UPDATE web_connections SET expires_at=?', (p.now[0] + 60,))
    original = p.repo._publish_cached_events

    def delayed(*args):
        original(*args)
        p.now[0] += 60

    monkeypatch.setattr(p.repo, '_publish_cached_events', delayed)
    with pytest.raises(PermissionPending if expiry == 'permission' else Missing):
        p.repo.materialize_cached_events(p.device)
    assert published(p) == []
    with p.repo.connect() as db:
        assert db.execute('SELECT count(*) FROM web_user_source_versions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM web_event_states').fetchone()[0] == 0


def test_completed_and_queued_ingestion_metadata_does_not_renew_access(progress_db):
    p = progress_db
    assert p.repo.ingestion_pending(p.device)
    p.repo.mark_ingested(p.device['id_hash'])
    assert not p.repo.ingestion_pending(p.device)
    p.repo.request_ingestion(p.device['id_hash'])
    before = snapshot_metadata(p)
    assert p.repo.ingestion_pending(p.device)
    assert snapshot_metadata(p) == before
    p.repo.shared_ingestion_enabled = False
    cache(p, event())
    assert not p.repo.ingestion_pending(p.device)
    assert p.repo.materialize_cached_events(p.device) == 0
    assert published(p) == []


def test_normal_zero_channel_proof_has_no_progress_to_publish(progress_db):
    p = progress_db
    with p.repo.connect() as db:
        db.execute('DELETE FROM web_channel_access WHERE user_id=?', (p.user,))
    cache(p, event())
    assert p.repo.materialize_cached_events(p.device) == 0
    assert published(p) == []
