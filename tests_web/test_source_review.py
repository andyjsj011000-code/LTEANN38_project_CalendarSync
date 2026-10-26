"""Regression checks for recurring source revisions and incomplete image reads."""
import pytest
import httpx
from conftest import TENANT, CLIENT
from test_server import server
from web_service.direct_source import DirectSource, TeamsGraph
from web_service.school_graph import GraphFailure


def sync_time(server, client, value):
    server.source.time = value
    server.now[0] += 61
    assert client.post('/api/teams/sync').status_code == 200
    return client.get('/api/changes').json()['items']


def test_source_revision_revert_discards_obsolete_pending_change(server):
    client = server.client()
    assert client.get('/api/today').json()['teams'][0]['time'] == '18:00'
    pending = sync_time(server, client, '19:00')[0]
    assert sync_time(server, client, '18:00') == []
    assert client.post('/api/changes/' + pending['id'], json={'accept': True}).status_code == 404
    assert client.get('/api/today').json()['teams'][0]['time'] == '18:00'


def test_source_revision_reappearing_superseded_change_can_be_reviewed(server):
    client = server.client()
    client.get('/api/today')
    sync_time(server, client, '19:00')
    sync_time(server, client, '20:00')
    changes = sync_time(server, client, '19:00')
    assert len(changes) == 1 and changes[0]['after']['time'] == '19:00'
    assert client.post('/api/changes/' + changes[0]['id'], json={'accept': True}).status_code == 200
    assert client.get('/api/today').json()['teams'][0]['time'] == '19:00'


@pytest.mark.parametrize('accept', [False, True])
def test_source_revision_decisions_persist_until_a_new_revision_is_seen(server, accept):
    client = server.client()
    client.get('/api/today')
    change = sync_time(server, client, '19:00')[0]
    assert client.post('/api/changes/' + change['id'], json={'accept': accept}).status_code == 200
    assert sync_time(server, client, '19:00') == []
    assert client.get('/api/today').json()['teams'][0]['time'] == ('19:00' if accept else '18:00')
    change_c = sync_time(server, client, '20:00')[0]
    assert sync_time(server, client, '19:00') == []
    assert client.post('/api/changes/' + change_c['id'], json={'accept': True}).status_code == 404
    change_c = sync_time(server, client, '20:00')[0]
    assert client.post('/api/changes/' + change_c['id'], json={'accept': True}).status_code == 200
    recurring_b = sync_time(server, client, '19:00')
    assert len(recurring_b) == 1 and recurring_b[0]['after']['time'] == '19:00'


@pytest.mark.parametrize('failure', [401, 403, 404, 429, 503, 'mime', 'oversize', 'empty', 'network'])
def test_source_revision_image_failure_preserves_cache_and_retries(server, failure):
    channel = {'id': 'class', 'team_id': CLIENT, 'name': 'Class'}
    image_url = 'https://graph.microsoft.com/v1.0/teams/' + CLIENT + '/channels/class/messages/m/hostedContents/h/$value'
    state = {'version': '1', 'failure': None}
    def response(request):
        if request.url.path.endswith('/$value'):
            mode = state['failure']
            if mode == 'network':
                raise httpx.ConnectError('Temporary outage', request=request)
            status = mode if isinstance(mode, int) else 200
            body = b'x' * 3_000_001 if mode == 'oversize' else (b'' if mode == 'empty' else b'fake image bytes')
            mime = 'text/html' if mode == 'mime' else 'image/png'
            return httpx.Response(status, content=body, headers={'content-type': mime})
        return httpx.Response(200, json={'value': [{'id': 'm', 'createdDateTime': '2026-10-22T01:00:00Z',
            'lastModifiedDateTime': state['version'], 'body': {'content': '<img src="' + image_url + '">'}, 'replies': []}]})
    graph = TeamsGraph(lambda: httpx.Client(transport=httpx.MockTransport(response)))
    class Extractor:
        configured = True
        calls = 0
        def extract(self, text, images, previous):
            self.calls += 1
            if not images:
                return []
            return [{'key': 'task', 'title': 'Image deadline', 'notes': '', 'start_date': '2026-10-22',
                     'end_date': '2026-10-22', 'time': '18:00', 'kind': 'assignment', 'cancelled': False}]
    extractor = Extractor()
    source = DirectSource(graph, extractor)
    initial = source.collect('token', [channel], TENANT, server.db, 'fake-user')
    old = server.db.cached_messages('class')['m']
    state.update(version='2', failure=failure)
    with pytest.raises(GraphFailure):
        source.collect('token', [channel], TENANT, server.db, 'fake-user')
    assert extractor.calls == 1
    assert server.db.cached_messages('class')['m'] == old
    assert server.db.catalog_events(['class']) == initial
    state['failure'] = None
    retried = source.collect('token', [channel], TENANT, server.db, 'fake-user')
    assert extractor.calls == 2 and not retried[0]['cancelled']
    assert retried[0]['source_version'] != initial[0]['source_version']
