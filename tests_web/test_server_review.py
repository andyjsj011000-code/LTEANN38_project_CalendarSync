import json
from types import SimpleNamespace
import pytest
from conftest import TENANT
from test_server import server, subscription
from web_service.push import Notifications
from web_service.direct_source import DirectSource
from web_service.storage import Missing, digest


def test_full_catalog_and_source_lookup_survive_more_than_one_hundred_messages(server):
    client = server.client()
    session = server.db.session(client.cookies.get('__Host-calendarsync_session'))
    device = server.db.session_device(session)
    expected = set()
    message_body = {'body': {'content': 'Same source'}, 'subject': None, 'replies': [],
                    'deletedDateTime': None, 'lastModifiedDateTime': '2026-10-22T08:00:00Z'}
    version = digest(json.dumps(message_body, sort_keys=True, ensure_ascii=False))
    for number in range(101):
        message = str(number)
        event = {'id': 'direct:message-' + message, 'channel_id': 'class',
                 'title': 'Deadline ' + message, 'notes': '', 'start_date': '2026-10-22',
                 'end_date': '2026-10-22', 'time': '18:00', 'kind': 'assignment',
                 'source_url': 'https://teams.microsoft.com/', 'source_version': message,
                 'cancelled': False}
        expected.add(event['id'])
        server.db.cache_analysis(digest('class|' + message), 'class', message, version, [event])
        server.now[0] += 1
    assert len(server.db.cached_messages('class')) == 101
    def unexpected_analysis(*args):
        pytest.fail('An unchanged cached post must not be analyzed again')
    graph = SimpleNamespace(messages=lambda *args: [{'id': '100', **message_body}],
                            message=lambda token, channel, mid: {'id': mid, **message_body})
    extractor = SimpleNamespace(configured=True, extract=unexpected_analysis)
    source = DirectSource(graph, extractor, images=False)
    channels = server.graph.channels('', device['object_id'], TENANT)
    catalog = source.collect('', channels, TENANT, server.db, device['user_id'])
    assert {event['id'] for event in catalog} == expected
    lease = server.db.begin_sync(device['user_id'], 0)
    server.db.commit_snapshot(device, server.graph.channels('', device['object_id'], TENANT), catalog,
                              server.settings.access_seconds, lease)
    visible = server.db.calendar(device['user_id'], TENANT, '2026-10-22', '2026-10-22')
    assert {event['id'] for event in visible} == expected


def test_expired_push_endpoint_preserves_valid_sibling_subscription(server):
    client = server.client()
    assert client.get('/api/today').json()['teams']
    expired = subscription('https://fcm.googleapis.com/fcm/send/expired')
    valid = subscription('https://fcm.googleapis.com/fcm/send/valid')
    assert client.post('/api/push', json=expired).status_code == 201
    assert client.post('/api/push', json=valid).status_code == 201
    session = server.db.session(client.cookies.get('__Host-calendarsync_session'))
    device = server.db.session_device(session)
    attempts = []
    def send(value, payload):
        attempts.append(value['endpoint'])
        return 'expired' if value['endpoint'] == expired['endpoint'] else 'sent'
    server.sender.send = send
    assert Notifications(server.db, server.sender).dispatch(device) == 1
    remaining = server.db.subscriptions(device['id_hash'])
    assert [item['subscription']['endpoint'] for item in remaining] == [valid['endpoint']]
    assert set(attempts) == {expired['endpoint'], valid['endpoint']}
    server.db.unsubscribe_subscription('another-device', remaining[0]['id'])
    assert server.db.subscriptions(device['id_hash'])


@pytest.mark.parametrize('invalidate', ['logout', 'expired', 'background', 'account'])
def test_push_registration_rechecks_connection_at_commit(server, invalidate):
    client = server.client()
    session = server.db.session(client.cookies.get('__Host-calendarsync_session'))
    device = server.db.session_device(session)
    if invalidate == 'logout':
        assert client.post('/auth/logout').status_code == 204
    else:
        with server.db.connect() as db:
            if invalidate == 'expired':
                db.execute('UPDATE web_connections SET expires_at=? WHERE id_hash=?',
                           (server.now[0], device['id_hash']))
            elif invalidate == 'background':
                db.execute('UPDATE web_connections SET background=0 WHERE id_hash=?',
                           (device['id_hash'],))
            else:
                db.execute('UPDATE web_users SET active=0 WHERE id=?', (device['user_id'],))
    with pytest.raises(Missing):
        server.db.subscribe(device, subscription())
    assert server.db.subscriptions(device['id_hash']) == []


@pytest.mark.parametrize('invalidate', ['logout', 'unsubscribe', 'expired'])
def test_delivery_claim_tolerates_revocation_after_dispatch_check(server, invalidate):
    client = server.client()
    session = server.db.session(client.cookies.get('__Host-calendarsync_session'))
    device = server.db.session_device(session)
    subscription_id = server.db.subscribe(device, subscription())
    if invalidate == 'logout':
        server.db.revoke_device(device['id_hash'])
    elif invalidate == 'unsubscribe':
        server.db.unsubscribe_subscription(device['id_hash'], subscription_id)
    else:
        with server.db.connect() as db:
            db.execute('UPDATE web_connections SET expires_at=? WHERE id_hash=?',
                       (server.now[0], device['id_hash']))
    assert server.db.claim_delivery('stale-dispatch', subscription_id) is False
    with server.db.connect() as db:
        assert db.execute('SELECT count(*) FROM web_deliveries').fetchone()[0] == 0
