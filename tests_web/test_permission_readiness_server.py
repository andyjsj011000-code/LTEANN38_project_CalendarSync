"""Access refresh must not masquerade as an empty calendar or a missing item."""
import pytest

from conftest import CLIENT
from test_server import server
from test_postgres_server_integration import postgres_server
from test_ingestion_status import CatalogSource, seed_catalog, session_device


@pytest.fixture(params=['server', 'postgres_server'])
def access_server(request):
    # Native PostgreSQL remains opt-in through its disposable-database fixture.
    return request.getfixturevalue(request.param)


def connected(s):
    s.db.shared_ingestion_enabled = True
    s.integration.source = CatalogSource()
    client = s.client()
    session, device = session_device(s, client)
    seed_catalog(s)
    response = client.get('/api/today')
    assert response.status_code == 200
    assert [event['title'] for event in response.json()['teams']] == ['Cached deadline']
    return client, session, device


@pytest.mark.parametrize('ingest', [False, True])
def test_every_shared_refresh_preserves_unexpired_access_during_graph(access_server, monkeypatch, ingest):
    s = access_server
    client, session, device = connected(s)
    original_channels = s.graph.channels
    observed = []

    def channels(*args):
        observed.append(s.db.sync_status(session['user_id'])['state'])
        assert s.db.channels(session['user_id'], session['tenant_id'])
        response = client.get('/api/today')
        assert response.status_code == 200
        assert [event['title'] for event in response.json()['teams']] == ['Cached deadline']
        return original_channels(*args)

    monkeypatch.setattr(s.graph, 'channels', channels)
    s.integration.run_sync(device, ingest=ingest, force=True)

    assert observed == ['checking'] * (2 if ingest else 1)
    assert s.db.sync_status(session['user_id'])['state'] == 'connected'


def test_hide_can_use_still_valid_access_while_another_check_runs(access_server):
    s = access_server
    client, session, _ = connected(s)
    assert s.db.begin_sync(session['user_id'], 0, preserve_access=True)

    response = client.patch('/api/events/direct:cached/state', json={'hidden': True})

    assert response.status_code == 200
    assert response.json()['hidden'] is True
    assert s.graph.calls == 1


@pytest.mark.parametrize('path', [
    '/api/today',
    '/api/calendar?start=2026-10-01&end=2026-10-31',
    '/api/channels',
    '/api/hidden?start=2026-10-01&end=2026-10-31',
    '/api/changes',
])
def test_expired_access_during_check_returns_retryable_response_instead_of_empty_data(access_server, path):
    s = access_server
    client, session, _ = connected(s)
    assert s.db.begin_sync(session['user_id'], 0, preserve_access=True)
    s.now[0] += s.settings.access_seconds + 1

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()['code'] == 'permission_check_pending'
    assert response.headers['retry-after'] == '2'
    assert 'Cached deadline' not in response.text
    assert s.graph.calls == 1


def test_pending_hide_is_not_executed_and_succeeds_once_after_verification(access_server, monkeypatch):
    s = access_server
    client, session, device = connected(s)
    lease = s.db.begin_sync(session['user_id'], 0, preserve_access=True)
    assert lease
    s.now[0] += s.settings.access_seconds + 1
    original_state = s.db.event_state
    mutations = []

    def event_state(*args):
        mutations.append(args)
        return original_state(*args)

    monkeypatch.setattr(s.db, 'event_state', event_state)
    response = client.patch('/api/events/direct:cached/state', json={'hidden': True})
    assert response.status_code == 503
    assert response.json()['code'] == 'permission_check_pending'
    assert mutations == []

    # Complete the owning verification, then emulate the user's explicit retry.
    channels = [{'id': 'class', 'team_id': CLIENT, 'name': 'Class'}]
    s.db.commit_snapshot(device, channels, s.db.catalog_events(['class']), s.settings.access_seconds, lease)
    response = client.patch('/api/events/direct:cached/state', json={'hidden': True})

    assert response.status_code == 200
    assert response.json()['hidden'] is True
    assert len(mutations) == 1


def test_genuinely_empty_permission_result_and_unknown_event_keep_normal_semantics(access_server):
    s = access_server
    client, session, device = connected(s)
    assert client.patch('/api/events/direct:unknown/state', json={'hidden': True}).status_code == 404
    s.graph.allowed = False
    s.integration.run_sync(device, force=True)

    response = client.get('/api/today')

    assert response.status_code == 200 and response.json()['teams'] == []
    assert s.db.sync_status(session['user_id'])['state'] == 'connected'
    assert client.patch('/api/events/direct:cached/state', json={'hidden': True}).status_code == 404


@pytest.mark.parametrize('method', ['GET', 'PUT'])
def test_teams_reminder_routes_return_pending_without_reading_or_saving_rules(access_server, monkeypatch, method):
    s = access_server
    client, session, _ = connected(s)
    assert s.db.begin_sync(session['user_id'], 0, preserve_access=True)
    s.now[0] += s.settings.access_seconds + 1
    calls = []
    monkeypatch.setattr(s.db, 'rules', lambda *args: calls.append('read'))
    monkeypatch.setattr(s.db, 'save_rules', lambda *args: calls.append('save'))
    kwargs = {'json': {'offsets': [-30], 'custom_times': []}} if method == 'PUT' else {}

    response = client.request(method, '/api/reminders/teams/direct:cached', **kwargs)

    assert response.status_code == 503
    assert response.json()['code'] == 'permission_check_pending'
    assert response.headers['retry-after'] == '2'
    assert calls == []
