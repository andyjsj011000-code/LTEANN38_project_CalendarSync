"""Explicit Oracle existence checks must not confuse window omissions with deletion."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet
from conftest import TENANT, CLIENT
from test_server import server
from web_service.oracle_source import OracleSource, OracleConfig, KST


def make_oracle():
    key = Fernet.generate_key()
    cipher = Fernet(key)
    state = SimpleNamespace(rows={}, listed=set(), calls=[], fail_lookup=False)
    def row(item_id, channel='class', when=None):
        when = when or datetime(2026, 10, 22, 18)
        return (item_id, channel, cipher.encrypt(b'Task').decode(), cipher.encrypt(b'Notes').decode(), when, when, 'message')
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, **params):
            state.calls.append((sql, params))
            self.params = params
            if 'id IN (' in sql and state.fail_lookup:
                raise RuntimeError('Oracle read failed')
        def fetchmany(self, size):
            ids = {value for key, value in self.params.items() if key.startswith('id_')}
            if not ids: ids = state.listed
            return [value for key, value in state.rows.items() if key in ids and value[1] == self.params['channel_id']][:size]
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return Cursor()
        def rollback(self): state.calls.append(('ROLLBACK', {}))
    source = OracleSource(OracleConfig('read-only', 'fake', 'fake', key.decode()), connect=lambda **kwargs: Connection())
    return source, state, row


def old_event(item_id='deadline', channel='class'):
    return {'id': item_id, 'channel_id': channel, 'title': 'Known task', 'notes': '',
            'start_date': '2026-10-22', 'end_date': '2026-10-22', 'time': '18:00',
            'kind': 'assignment', 'cancelled': False, 'source_url': 'https://teams.microsoft.com/'}


def test_oracle_omitted_window_row_is_fetched_without_assuming_deletion():
    source, state, row = make_oracle()
    later = datetime.now(KST).replace(tzinfo=None) + timedelta(days=730)
    state.rows['deadline'] = row('deadline', when=later)
    events = source.events([{'id': 'class', 'team_id': CLIENT}], TENANT, [old_event()])
    assert len(events) == 1 and not events[0].get('cancelled', False)
    assert events[0]['end_date'] == later.date().isoformat()
    lookup = next(call for call in state.calls if 'id IN (' in call[0])
    assert 'start_at' not in lookup[1] and lookup[1] == {'channel_id': 'class', 'id_0': 'deadline'}


def test_oracle_deleted_rows_require_bound_read_only_lookup_in_small_batches():
    source, state, row = make_oracle()
    known = [old_event('id-' + str(i)) for i in range(51)]
    known.append(old_event('private-id', channel='denied'))
    result = source.events([{'id': 'class', 'team_id': CLIENT}], TENANT, known)
    assert len(result) == 51 and all(event['cancelled'] for event in result)
    lookups = [params for sql, params in state.calls if 'id IN (' in sql]
    assert [len(params)-1 for params in lookups] == [50, 1]
    assert all(params['channel_id'] == 'class' and 'private-id' not in params.values() for params in lookups)
    assert state.calls[0][0] == 'SET TRANSACTION READ ONLY' and state.calls[-1][0] == 'ROLLBACK'
    assert all(sql.startswith(('SELECT ', 'SET TRANSACTION ', 'ROLLBACK')) for sql, params in state.calls)


def test_oracle_lookup_failure_does_not_return_false_cancellations():
    source, state, row = make_oracle()
    state.fail_lookup = True
    with pytest.raises(RuntimeError):
        source.events([{'id': 'class', 'team_id': CLIENT}], TENANT, [old_event()])


def test_oracle_deleted_event_enters_user_review_and_unauthorized_channel_is_skipped(server):
    source, state, row = make_oracle()
    state.rows['deadline'] = row('deadline')
    state.listed.add('deadline')
    server.integration.source = source
    client = server.client()
    initial = client.get('/api/today').json()['teams'][0]
    state.rows.clear(); state.listed.clear(); server.now[0] += 61
    assert client.post('/api/teams/sync').status_code == 200
    changes = client.get('/api/changes').json()['items']
    assert len(changes) == 1 and changes[0]['after']['cancelled'] is True
    assert client.get('/api/today').json()['teams'][0]['cancelled'] is False
    session = server.db.session(client.cookies.get('__Host-calendarsync_session'))
    assert server.integration.notifications.due(session['user_id'], TENANT) == []
    server.graph.allowed = False; server.now[0] += 61; state.calls.clear()
    client.post('/api/teams/sync')
    assert state.calls == []
    server.graph.allowed = True; server.now[0] += 61
    client.post('/api/teams/sync')
    change = client.get('/api/changes').json()['items'][0]
    assert client.post('/api/changes/' + change['id'], json={'accept': True}).status_code == 200
    assert client.get('/api/today').json()['teams'][0]['cancelled'] is True
    assert server.db.known_oracle_events(session['user_id'], TENANT, [{'id': 'class'}]) == []
    assert server.db.known_oracle_events('another-user', TENANT, [{'id': 'class'}]) == []
