from datetime import datetime, timezone
import json
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from web_service.beta import BETA_TENANT, BetaDatabase, create_beta_app, prepare_beta, read_beta_config
from web_service.config import Settings
from web_service.storage import Database


@pytest.fixture
def beta(tmp_path):
    clock = lambda: int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp())
    settings = Settings(database_path=tmp_path / "beta.sqlite3", data_key=Fernet.generate_key().decode(), origin="http://127.0.0.1:8080")
    db = BetaDatabase(settings.database_path, settings.data_key, clock=clock)
    app = create_beta_app(settings, db)
    codes = [db.add_participant(label) for label in ("one", "two", "three")]
    clients = []

    def client(code=None):
        c = TestClient(app, base_url=settings.origin)
        clients.append(c)
        if code:
            assert c.post('/auth/beta', json={"code": code}).status_code == 200
            c.headers['x-csrf-token'] = c.get('/api/me').json()['csrf_token']
        return c

    yield SimpleNamespace(db=db, app=app, codes=codes, client=client, settings=settings)
    for c in clients:
        c.close()


def test_beta_is_explicit_and_html_assets_are_complete(beta, env):
    ordinary = env.client()
    assert ordinary.post('/auth/beta', json={"code": beta.codes[0]}).status_code == 404
    assert ordinary.get('/').json()['ui_connected'] is False
    c = beta.client()
    root = c.get('/')
    assert 'text/html' in root.headers['content-type']
    assert 'lang="ko"' in root.text
    for name in ('beta.js', 'beta-calendar.js', 'beta.css'):
        assert '/assets/' + name in root.text
        assert c.get('/assets/' + name).status_code == 200
    info = c.get('/api/beta/info').json()
    assert info['school_login'] is False and info['synthetic_data_only'] is True
    assert c.get('/auth/login').status_code == 503
    assert c.get('/api/me').status_code == 401
    for path in ('/assets/../beta.py', '/assets/%2e%2e/beta.py', '/var/data/invitations.json', '/assets/config.json'):
        assert c.get(path).status_code == 404


def test_codes_are_private_hashes_login_origin_and_logout(beta):
    c = beta.client()
    assert c.post('/auth/beta', json={"code": beta.codes[0]}, headers={"origin": "https://elsewhere.test"}).status_code == 403
    assert c.post('/auth/beta', json={"code": 'x' * 32}).status_code == 401
    result = c.post('/auth/beta', json={"code": beta.codes[0]})
    assert result.status_code == 200
    assert 'HttpOnly' in result.headers['set-cookie'] and 'SameSite=lax' in result.headers['set-cookie']
    assert c.post('/auth/logout').status_code == 403
    csrf = c.get('/api/me').json()['csrf_token']
    assert c.post('/auth/logout', headers={'x-csrf-token': csrf}).status_code == 204
    assert c.get('/api/me').status_code == 401
    contents = beta.db.path.read_bytes()
    for code in beta.codes:
        assert code.encode() not in contents


def test_login_rate_limit_is_shared_across_requests(beta):
    c = beta.client()
    for _ in range(20):
        assert c.post('/auth/beta', json={'code': 'invalid' * 5}).status_code == 401
    assert c.post('/auth/beta', json={'code': beta.codes[0]}).status_code == 429


def test_three_accounts_and_relogin_keep_personal_data_separate(beta):
    a, b, c = [beta.client(code) for code in beta.codes]
    task = a.post('/api/tasks', json={'title': 'private alpha', 'start_date': '2026-10-22', 'end_date': '2026-10-25', 'display_mode': 'selected', 'selected_dates': ['2026-10-22']}).json()
    assert task['selected_dates'] == ['2026-10-22', '2026-10-25']
    for other in (b, c):
        assert other.get('/api/tasks/' + task['id']).status_code == 404
        assert 'private alpha' not in other.get('/api/today').text
    again = beta.client(beta.codes[0])
    assert again.get('/api/tasks/' + task['id']).json()['title'] == 'private alpha'
    a.patch('/api/tasks/' + task['id'] + '/state', json={'version': task['version'], 'completed': True})
    assert again.get('/api/tasks/' + task['id']).json()['completed'] is True
    assert b.get('/api/today').json()['teams'] == c.get('/api/today').json()['teams']


def test_seed_is_idempotent_and_keeps_cancellation_and_ranges(beta):
    c = beta.client(beta.codes[0])
    beta.client(beta.codes[0])
    with beta.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM web_events').fetchone()[0] == 5
        assert connection.execute('SELECT count(*) FROM web_personal_tasks').fetchone()[0] == 3
    events = c.get('/api/calendar?start=2026-10-01&end=2026-11-01').json()['items']
    assert any(e['cancelled'] for e in events)
    assert any(e['start_date'] < e['end_date'] for e in events)
    assert all('예시' in e['notes'] or '예시' in e['title'] for e in events)


def test_display_settings_are_atomic_and_only_affect_owner(beta):
    a, b = [beta.client(code) for code in beta.codes[:2]]
    before = a.get('/api/channels').json()
    body = {'channels': {'beta-class': False, 'not-authorized': True}, 'disabled_kinds': ['assignment']}
    assert a.put('/api/beta/display-settings', json=body).status_code == 404
    assert a.get('/api/channels').json() == before
    assert a.get('/api/settings').json()['preferences']['disabled_kinds'] == []
    body['channels'].pop('not-authorized')
    assert a.put('/api/beta/display-settings', json=body).status_code == 200
    assert a.get('/api/today').json()['teams'] == []
    assert b.get('/api/today').json()['teams']
    assert a.put('/api/beta/display-settings', json={'channels': {}, 'disabled_kinds': ['invented']}).status_code == 422


def test_hidden_and_encrypted_feedback_require_account_and_csrf(beta):
    a, b = [beta.client(code) for code in beta.codes[:2]]
    assert a.patch('/api/events/beta-homework/state', json={'hidden': True}).status_code == 200
    assert [e['id'] for e in a.get('/api/beta/hidden').json()['items']] == ['beta-homework']
    assert b.get('/api/beta/hidden').json()['items'] == []
    a.headers.pop('x-csrf-token')
    assert a.post('/api/beta/feedback', json={'message': 'private feedback'}).status_code == 403
    a.headers['x-csrf-token'] = a.get('/api/me').json()['csrf_token']
    assert a.post('/api/beta/feedback', json={'message': 'private feedback'}).status_code == 201
    with beta.db.connect() as connection:
        row = connection.execute('SELECT * FROM beta_feedback').fetchone()
        assert beta.db.unseal(row['body_blob']) == {'message': 'private feedback'}
        assert row['user_id'] == a.get('/api/me').json()['id']
    assert b'private feedback' not in beta.db.path.read_bytes()


def test_code_rotation_revokes_old_sessions(beta):
    old = beta.client(beta.codes[0])
    new_code = beta.db.add_participant('one')
    assert old.get('/api/me').status_code == 401
    assert old.post('/auth/beta', json={'code': beta.codes[0]}).status_code == 401
    assert beta.client(new_code).get('/api/me').json()['name'] == 'one'


def test_beta_refuses_ordinary_database_and_real_accounts(tmp_path):
    settings = Settings(database_path=tmp_path / 'ordinary.db', data_key=Fernet.generate_key().decode(), origin='http://127.0.0.1:8080')
    ordinary = Database(settings.database_path, settings.data_key)
    ordinary.initialize()
    with pytest.raises(RuntimeError, match='non-beta'):
        create_beta_app(settings)
    with pytest.raises(ValueError, match='isolated'):
        create_beta_app(settings, ordinary)
    other = BetaDatabase(tmp_path / 'synthetic.db', settings.data_key)
    other.initialize()
    other.save_user('real-tenant', 'real-user', {'name': 'real'})
    with pytest.raises(RuntimeError, match='real-account'):
        other.initialize()


def test_prepare_reuses_key_accounts_and_rejects_path_escape(tmp_path):
    config, codes = prepare_beta(tmp_path / 'data', 'http://127.0.0.1:8080')
    original = config.read_bytes()
    assert len(codes) == 3 and len({c['code'] for c in codes}) == 3
    config2, codes2 = prepare_beta(tmp_path / 'data', 'http://127.0.0.1:8080')
    assert config2 == config and codes2 == codes and config.read_bytes() == original
    settings = read_beta_config(config)
    assert not settings.tenant_id and not settings.client_secret
    with pytest.raises(ValueError, match='origin differs'):
        prepare_beta(tmp_path / 'data', 'http://127.0.0.1:8081')
    values = json.loads(config.read_text())
    values['database_file'] = '../ordinary.db'
    config.write_text(json.dumps(values))
    with pytest.raises(ValueError, match='inside'):
        read_beta_config(config)
