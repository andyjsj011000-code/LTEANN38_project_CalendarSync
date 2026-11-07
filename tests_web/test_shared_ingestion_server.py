"""Shared ingestion serves verified cache before deferred source processing."""
import pytest

from test_server import server
from test_ingestion_status import CatalogSource, seed_catalog, session_device
from web_service.school_graph import GraphFailure
from web_service.storage import Missing


class DeferredTasks:
    def __init__(self):
        self.items = []

    def add_task(self, function, *args, **kwargs):
        self.items.append((function, args, kwargs))


def enable_shared(server):
    server.db.shared_ingestion_enabled = True
    server.integration.source = CatalogSource()
    client = server.client()
    session, device = session_device(server, client)
    seed_catalog(server)
    return client, session, device


def test_manual_sync_publishes_only_authorized_cache_before_queuing_work(server):
    _, session, device = enable_shared(server)
    server.db.mark_ingested(device['id_hash'])
    server.db.defer_device(device['id_hash'], 300)
    tasks = DeferredTasks()

    result = server.integration.sync(session, tasks)

    assert result['state'] == 'connected'
    assert result['ingestion'] == {'state': 'queued'}
    assert server.integration.source.calls == 0
    assert len(tasks.items) == 1
    assert tasks.items[0][0] == server.integration.run_deferred_ingestion
    current = server.db.device(device['id_hash'])
    assert current['ingest_at'] == 0
    assert current['next_sync'] == server.now[0]
    events = server.db.agenda(session['user_id'], session['tenant_id'], '2026-10-22')['teams']
    assert [event['title'] for event in events] == ['Cached deadline']


def test_background_collection_releases_snapshot_lease_and_allows_permission_rechecks(server, monkeypatch):
    client, session, device = enable_shared(server)
    original_collect = server.integration.source.collect

    def collect(*args):
        state = server.db.sync_status(session['user_id'])
        assert state['state'] == 'connected' and state['lease_until'] == 0
        assert server.db.channels(session['user_id'], session['tenant_id'])
        # A long analysis must not prevent the ordinary access recheck after
        # the short permission TTL, or erase the existing authorized agenda.
        server.now[0] += 61
        assert [event['title'] for event in client.get('/api/today').json()['teams']] == ['Cached deadline']
        return original_collect(*args)

    monkeypatch.setattr(server.integration.source, 'collect', collect)
    report = server.integration.run_sync(device, ingest=True)

    assert report['state'] == 'completed'
    assert server.graph.calls == 3  # Initial proof, view recheck, final proof.
    assert server.db.device(device['id_hash'])['ingest_at'] == server.now[0] + 300


def test_final_snapshot_rechecks_membership_after_background_collection(server, monkeypatch):
    _, session, device = enable_shared(server)
    original_collect = server.integration.source.collect

    def collect(*args):
        result = original_collect(*args)
        server.graph.allowed = False
        return result

    monkeypatch.setattr(server.integration.source, 'collect', collect)
    report = server.integration.run_sync(device, ingest=True)

    assert report['state'] == 'completed'
    assert server.graph.calls == 2
    assert server.db.channels(session['user_id'], session['tenant_id']) == []
    assert server.db.agenda(session['user_id'], session['tenant_id'], '2026-10-22')['teams'] == []


def test_revoked_device_cannot_publish_final_background_snapshot(server, monkeypatch):
    _, session, device = enable_shared(server)
    original_collect = server.integration.source.collect
    marked = []

    def collect(*args):
        result = original_collect(*args)
        server.db.revoke_device(device['id_hash'])
        return result

    monkeypatch.setattr(server.integration.source, 'collect', collect)
    monkeypatch.setattr(server.db, 'mark_ingested', marked.append)

    assert server.integration.run_sync(device, ingest=True) is None
    assert marked == []
    assert server.db.sync_status(session['user_id'])['state'] == 'reauth_required'
    assert server.db.channels(session['user_id'], session['tenant_id']) == []


def test_background_verification_failure_remains_fail_closed(server, monkeypatch):
    _, session, device = enable_shared(server)

    def collect(*args):
        assert server.db.sync_status(session['user_id'])['state'] == 'connected'
        raise GraphFailure('verification_failed')

    monkeypatch.setattr(server.integration.source, 'collect', collect)

    assert server.integration.run_sync(device, ingest=True) is None
    assert server.db.sync_status(session['user_id'])['state'] == 'verification_failed'
    assert server.db.channels(session['user_id'], session['tenant_id']) == []
    assert server.db.device(device['id_hash'])['ingest_at'] == 0


@pytest.mark.parametrize('busy', [False, True])
def test_manual_sync_does_not_queue_without_successful_permission_commit(server, monkeypatch, busy):
    _, session, device = enable_shared(server)
    tasks = DeferredTasks()
    queued = []
    monkeypatch.setattr(server.db, 'request_ingestion', queued.append)
    if busy:
        assert server.db.begin_sync(session['user_id'], 0)
    else:
        def channels(*args):
            raise GraphFailure('verification_failed')
        monkeypatch.setattr(server.graph, 'channels', channels)

    result = server.integration.sync(session, tasks)

    assert result['state'] == ('checking' if busy else 'verification_failed')
    assert result.get('ingestion') == ({'state': 'busy'} if busy else None)
    assert tasks.items == [] and queued == []
    assert server.integration.source.calls == 0


def test_final_commit_failure_does_not_mark_background_ingestion_complete(server, monkeypatch):
    _, session, device = enable_shared(server)
    original_commit = server.db.commit_snapshot
    commits = []
    marked = []

    def commit(*args):
        commits.append(1)
        if len(commits) == 2:
            raise Missing()
        return original_commit(*args)

    monkeypatch.setattr(server.db, 'commit_snapshot', commit)
    monkeypatch.setattr(server.db, 'mark_ingested', marked.append)

    assert server.integration.run_sync(device, ingest=True) is None
    assert len(commits) == 2 and marked == []
    assert server.db.sync_status(session['user_id'])['state'] == 'source_unavailable'


def test_worker_picks_up_durable_request_without_response_background_task(server):
    _, session, device = enable_shared(server)
    result = server.integration.sync(session)
    assert result['ingestion'] == {'state': 'queued'}
    assert server.integration.source.calls == 0

    server.integration.tick()

    assert server.integration.source.calls == 1
    assert server.db.device(device['id_hash'])['ingest_at'] == server.now[0] + 300
    assert server.db.sync_status(session['user_id'])['state'] == 'connected'


def test_shared_manual_route_returns_queued_and_executes_deferred_callback(server):
    client, _, device = enable_shared(server)

    response = client.post('/api/teams/sync')

    assert response.status_code == 200
    assert response.json()['ingestion'] == {'state': 'queued'}
    # TestClient waits for response background callbacks before returning.
    assert server.integration.source.calls == 1
    assert server.db.device(device['id_hash'])['ingest_at'] == server.now[0] + 300


def test_deferred_database_failure_preserves_worker_retry_without_failing_response(server, monkeypatch):
    _, session, device = enable_shared(server)
    assert server.integration.sync(session)['ingestion'] == {'state': 'queued'}
    audits = []

    def unavailable(*args, **kwargs):
        raise RuntimeError('simulated database failure')

    monkeypatch.setattr(server.integration, 'run_sync', unavailable)
    monkeypatch.setattr(server.db, 'audit', lambda event, user: audits.append((event, user)))

    server.integration.run_deferred_ingestion(device)

    assert audits == [('worker_failed', session['user_id'])]
    assert server.db.device(device['id_hash'])['ingest_at'] == 0
