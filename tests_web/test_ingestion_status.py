"""Manual-ingestion reports must describe committed, authorized snapshots."""
import pytest

from test_server import server
from web_service.direct_source import CollectionResult
from web_service.school_graph import GraphFailure
from web_service.storage import Missing, digest


def session_device(server, client):
    session = server.db.session(client.cookies.get('__Host-calendarsync_session'))
    return session, server.db.session_device(session)


class CatalogSource:
    configured = True

    def __init__(self, *, partial=False, legacy=False):
        self.calls = 0
        self.legacy = legacy
        self.report = {
            'channels_total': 1, 'channels_scanned': 1, 'messages_seen': 2,
            'messages_analyzed': 0 if partial else 1, 'messages_cached': 1,
            'messages_failed': 1 if partial else 0, 'partial': partial,
            # Source metadata must never leak through the report API.
            'private_source': 'private-access-token',
        }

    def collect(self, token, channels, tenant, db, user):
        self.calls += 1
        events = db.catalog_events([channel['id'] for channel in channels])
        return events if self.legacy else CollectionResult(events, self.report)


def seed_catalog(server):
    event = {
        'id': 'direct:cached', 'channel_id': 'class', 'title': 'Cached deadline',
        'notes': 'A previously analyzed post', 'start_date': '2026-10-22',
        'end_date': '2026-10-22', 'time': '18:00', 'kind': 'assignment',
    }
    server.db.cache_analysis(digest('class|cached'), 'class', 'cached', 'v1', [event])
    other = {**event, 'id': 'direct:other', 'channel_id': 'other', 'title': 'Other class'}
    server.db.cache_analysis(digest('other|cached'), 'other', 'cached', 'v1', [other])


def test_partial_snapshot_preserves_authorized_cached_events_and_remains_due(server):
    client = server.client()
    _, device = session_device(server, client)
    seed_catalog(server)
    server.integration.source = CatalogSource(partial=True)

    response = client.post('/api/teams/sync')

    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'connected'
    assert result['ingestion'] == {
        'channels_total': 1, 'channels_scanned': 1, 'messages_seen': 2,
        'messages_analyzed': 0, 'messages_cached': 1, 'messages_failed': 1,
        'partial': True, 'state': 'partial',
    }
    assert 'private-access-token' not in response.text
    assert server.db.device(device['id_hash'])['ingest_at'] == device['ingest_at']
    assert [event['title'] for event in client.get('/api/today').json()['teams']] == ['Cached deadline']
    assert [channel['id'] for channel in server.db.channels(device['user_id'], device['tenant_id'])] == ['class']


@pytest.mark.parametrize('legacy', [False, True])
def test_full_collection_is_scheduled_only_after_snapshot_commit(server, monkeypatch, legacy):
    client = server.client()
    _, device = session_device(server, client)
    seed_catalog(server)
    server.integration.source = CatalogSource(legacy=legacy)
    original_mark = server.db.mark_ingested
    marked = []

    def mark(device_hash):
        assert server.db.sync_status(device['user_id'])['state'] == 'connected'
        marked.append(device_hash)
        return original_mark(device_hash)

    monkeypatch.setattr(server.db, 'mark_ingested', mark)
    result = client.post('/api/teams/sync').json()

    assert result['state'] == 'connected'
    assert marked == [device['id_hash']]
    assert server.db.device(device['id_hash'])['ingest_at'] == server.now[0] + 300
    if legacy:
        assert 'ingestion' not in result
    else:
        assert result['ingestion']['state'] == 'completed'
        assert result['ingestion']['partial'] is False


@pytest.mark.parametrize('legacy', [False, True])
def test_failed_commit_does_not_schedule_or_report_completion(server, monkeypatch, legacy):
    client = server.client()
    _, device = session_device(server, client)
    server.integration.source = CatalogSource(legacy=legacy)
    marked = []

    def fail_commit(*args):
        raise Missing()

    monkeypatch.setattr(server.db, 'commit_snapshot', fail_commit)
    monkeypatch.setattr(server.db, 'mark_ingested', marked.append)
    result = client.post('/api/teams/sync').json()

    assert result['state'] == 'source_unavailable'
    assert 'ingestion' not in result
    assert marked == []
    assert server.db.device(device['id_hash'])['ingest_at'] == device['ingest_at']
    assert server.db.channels(device['user_id'], device['tenant_id']) == []


def test_busy_manual_sync_reports_no_false_completion(server):
    client = server.client()
    _, device = session_device(server, client)
    source = CatalogSource()
    server.integration.source = source
    lease = server.db.begin_sync(device['user_id'], 0)
    assert lease

    assert server.integration.run_sync(device, ingest=False) is None
    result = client.post('/api/teams/sync').json()

    assert result['state'] == 'checking'
    assert result['ingestion'] == {'state': 'busy'}
    assert source.calls == 0
    assert server.db.sync_status(device['user_id'])['lease_until'] == lease
    assert server.db.device(device['id_hash'])['ingest_at'] == device['ingest_at']


@pytest.mark.parametrize('failure', ['reauth_required', 'verification_failed'])
def test_auth_and_verification_failures_do_not_become_partial_success(server, monkeypatch, failure):
    client = server.client()
    _, device = session_device(server, client)
    source = CatalogSource()
    server.integration.source = source
    marked = []

    def fail_collect(*args):
        raise GraphFailure(failure)

    monkeypatch.setattr(source, 'collect', fail_collect)
    monkeypatch.setattr(server.db, 'mark_ingested', marked.append)
    result = client.post('/api/teams/sync').json()

    assert result['state'] == failure
    assert 'ingestion' not in result
    assert marked == []
    assert server.db.channels(device['user_id'], device['tenant_id']) == []
