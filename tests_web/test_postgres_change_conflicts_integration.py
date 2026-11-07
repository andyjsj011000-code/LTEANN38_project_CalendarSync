"""HTTP conflict behavior on disposable PostgreSQL; Graph is simulated."""
from urllib.parse import quote

from conftest import BOB
from test_postgres_server_integration import postgres_server


def test_stale_review_http_conflict_preserves_latest_change_and_account_isolation(postgres_server):
    s = postgres_server
    client = s.client()
    original = client.get('/api/today').json()['teams'][0]
    session = s.db.session(client.cookies.get('__Host-calendarsync_session'))
    device = s.db.session_device(session)

    s.source.time = '19:00'
    s.integration.run_sync(device, ingest=True)
    first = client.get('/api/changes').json()['items'][0]
    s.source.time = '20:00'
    s.integration.run_sync(device, ingest=True)
    latest = client.get('/api/changes').json()['items'][0]
    assert latest['id'] != first['id']

    stale = client.post('/api/changes/' + quote(first['id'], safe=''), json={'accept': True})
    assert stale.status_code == 409
    assert client.get('/api/changes').json()['items'][0]['id'] == latest['id']
    assert client.get('/api/today').json()['teams'][0]['time'] == original['time']

    other = s.client(BOB)
    assert other.post('/api/changes/' + quote(first['id'], safe=''), json={'accept': True}).status_code == 404
    assert client.post('/api/changes/unknown-change', json={'accept': True}).status_code == 404

    current_path = '/api/changes/' + quote(latest['id'], safe='')
    assert client.post(current_path, json={'accept': True}).status_code == 200
    assert client.post(current_path, json={'accept': False}).status_code == 409
    assert client.get('/api/today').json()['teams'][0]['time'] == '20:00'
    assert client.get('/api/changes').json()['items'] == []
