"""A missing permission proof is not a successful empty calendar response."""
from datetime import date
import json
import logging

import pytest

from test_ingestion_status_orchestration import tracked_server
from web_service.api import create_app
from web_service.school_graph import GraphFailure
from web_service.storage import PermissionPending


@pytest.fixture
def ready_calendar(tracked_server):
    s = tracked_server
    s.integration.run_sync(s.device)
    with s.db.connect() as connection:
        row = connection.execute('SELECT id_hash,user_id FROM web_sessions').fetchone()
    s.session = {**dict(row), 'tenant_id': s.device['tenant_id']}
    return s


def test_connected_verified_status_does_not_request_continuous_retries(ready_calendar):
    s = ready_calendar
    result = s.integration.status(s.session)
    assert result['state'] == 'connected'
    assert result['teams_verified'] is True
    assert result['retryable'] is False


@pytest.mark.parametrize('failure', ['source_unavailable', 'verification_failed', 'refresh_unavailable', 'refresh_busy'])
def test_transient_failure_is_unverified_not_a_successful_empty_read(ready_calendar, failure):
    s = ready_calendar
    s.now[0] += 1
    s.graph.channels = lambda *args: (_ for _ in ()).throw(GraphFailure(failure))
    s.integration.run_sync(s.device, force=True)
    reads = []
    with pytest.raises(PermissionPending) as raised:
        s.integration.read_teams(s.session, lambda: reads.append('read'))
    assert reads == []
    assert raised.value.connection_state == failure
    assert raised.value.retryable is True
    status = s.integration.status(s.session)
    assert status['teams_verified'] is False and status['retryable'] is True
    with s.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM web_events').fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['reauth_required', 'account_not_allowed', 'source_not_configured'])
def test_unrecoverable_connection_states_do_not_offer_automatic_retry(ready_calendar, failure):
    s = ready_calendar
    lease = s.db.begin_sync(s.device['user_id'], 0, preserve_access=True)
    s.db.fail_sync(s.device['user_id'], failure, lease)
    with pytest.raises(PermissionPending) as raised:
        s.integration.read_teams(s.session, lambda: pytest.fail('No unverified source read'))
    assert raised.value.connection_state == failure
    assert raised.value.retryable is False


def test_fresh_authoritative_zero_channels_is_a_normal_empty_calendar(ready_calendar):
    s = ready_calendar
    s.graph.channels = lambda *args: []
    s.integration.run_sync(s.device, force=True)
    assert s.integration.read_teams(s.session, lambda: []) == []
    assert s.integration.status(s.session)['teams_verified'] is True
    assert s.integration.status(s.session)['retryable'] is False


def test_original_unexpired_access_is_usable_while_checking_without_renewal(ready_calendar):
    s = ready_calendar
    user = s.device['user_id']
    s.db.begin_sync(user, 0, preserve_access=True)
    before = s.db.sync_status(user)['checked_at']
    assert s.integration.read_teams(s.session, lambda: ['authorized']) == ['authorized']
    assert s.db.sync_status(user)['checked_at'] == before
    assert s.integration.status(s.session)['retryable'] is True
    s.now[0] += s.integration.settings.access_seconds
    with pytest.raises(PermissionPending):
        s.integration.read_teams(s.session, lambda: [])


@pytest.mark.parametrize('path', ['/api/today', '/api/calendar', '/api/hidden', '/api/channels'])
def test_api_rechecks_original_proof_after_building_the_response(ready_calendar, monkeypatch, path):
    s = ready_calendar
    app = create_app(settings=s.integration.settings, database=s.db, integration=s.integration)
    endpoint = next(route.endpoint for route in app.routes if route.path == path and 'GET' in route.methods)
    original = s.integration.read_teams
    def expire_during_response(session, operation):
        def body():
            result = operation()
            s.now[0] += s.integration.settings.access_seconds
            return result
        return original(session, body)
    monkeypatch.setattr(s.integration, 'read_teams', expire_during_response)
    args = {'session': s.session}
    if path in {'/api/calendar', '/api/hidden'}:
        args.update(start=date(2026, 10, 1), end=date(2026, 10, 31))
    elif path == '/api/today':
        args['day'] = date(2026, 10, 22)
    with pytest.raises(PermissionPending) as raised:
        endpoint(**args)
    assert raised.value.connection_state == 'connected'
    assert raised.value.retryable is True


def test_acl_change_during_read_does_not_publish_previous_scope(ready_calendar):
    s = ready_calendar
    def body():
        s.graph.channels = lambda *args: []
        s.integration.run_sync(s.device, force=True)
        return ['stale private source']
    with pytest.raises(PermissionPending) as raised:
        s.integration.read_teams(s.session, body)
    assert raised.value.connection_state == 'connected'
    assert raised.value.retryable is True
    assert s.integration.read_teams(s.session, lambda: []) == []


def test_personal_only_api_still_works_when_teams_readiness_is_unknown(ready_calendar):
    s = ready_calendar
    s.now[0] += s.integration.settings.access_seconds
    app = create_app(settings=s.integration.settings, database=s.db, integration=s.integration)
    endpoint = next(route.endpoint for route in app.routes if route.path == '/api/personal/today')
    calls = []
    s.graph.channels = lambda *args: calls.append('unexpected graph')
    result = endpoint(day=date(2026, 10, 22), session=s.session)
    assert result == {'date': '2026-10-22', 'personal': []}
    assert calls == []


def test_permission_error_body_has_fixed_content_free_connection_metadata(ready_calendar):
    s = ready_calendar
    app = create_app(settings=s.integration.settings, database=s.db, integration=s.integration)
    handler = app.exception_handlers[PermissionPending]
    # This handler is a synchronous JSONResponse constructor in an async
    # function. Drive it without creating an OS event loop or a test socket.
    coroutine = handler(None, PermissionPending(state='source_unavailable'))
    with pytest.raises(StopIteration) as completed:
        coroutine.send(None)
    response = completed.value.value
    result = json.loads(response.body)
    assert response.status_code == 503
    assert result['code'] == 'permission_check_pending'
    assert result['connection_state'] == 'source_unavailable'
    assert result['teams_access_unverified'] is True and result['retryable'] is True
    assert 'Cached deadline' not in response.body.decode()


@pytest.mark.parametrize('sqlstate', [None, '40001', '40001 private-token'])
def test_sync_exception_diagnostic_omits_messages_sql_and_credentials(ready_calendar, monkeypatch, caplog, sqlstate):
    s = ready_calendar
    class OperationalError(Exception):
        pass
    error = OperationalError('private-access-token SELECT secret_body FROM private_table')
    error.sqlstate = sqlstate
    def rejected_commit(*args, **kwargs):
        raise error
    monkeypatch.setattr(s.db, 'commit_snapshot', rejected_commit)
    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        s.integration.run_sync(s.device, force=True)
    diagnostics = [record.getMessage() for record in caplog.records if 'exception_type=' in record.getMessage()]
    assert len(diagnostics) == 1
    assert 'exception_type=OperationalError' in diagnostics[0]
    assert 'internal_file=server.py' in diagnostics[0]
    assert 'internal_line=0' not in diagnostics[0]
    assert ('sqlstate=40001' if sqlstate == '40001' else 'sqlstate=none') in diagnostics[0]
    assert all(value not in caplog.text for value in ('private-access-token', 'SELECT secret_body', 'private_table'))
