"""Actual PostgreSQL expiry boundaries for Teams actions and refresh readiness."""
import pytest

from test_postgres_database_integration import postgres_database
from web_service.storage import Missing, PermissionPending


CHANNELS = [{'id': 'class', 'team_id': 'team', 'name': 'Class'}]
EVENT = {'id': 'direct:expiry', 'channel_id': 'class', 'title': 'Stored schedule',
         'notes': '', 'start_date': '2026-11-01', 'end_date': '2026-11-20',
         'kind': 'assignment', 'time': None, 'cancelled': False}


def publish(p):
    lease = p.repo.begin_sync(p.users['alice'], 0)
    p.repo.commit_snapshot(p.devices['alice'], CHANNELS, [EVENT], 60, lease)


def test_expired_own_event_and_channel_proofs_are_retryable_but_unknown_ids_are_missing(postgres_database):
    p = postgres_database
    publish(p)
    p.now[0] += 61
    with pytest.raises(PermissionPending):
        p.repo.event_state(p.users['alice'], 'tenant', EVENT['id'], {'hidden': True})
    with pytest.raises(PermissionPending):
        p.repo.channel_preference(p.users['alice'], 'tenant', 'class', False)
    for user, identity in ((p.users['alice'], 'direct:unknown'), (p.users['bob'], EVENT['id'])):
        with pytest.raises(Missing) as caught:
            p.repo.event_state(user, 'tenant', identity, {'hidden': True})
        assert type(caught.value) is Missing
    with p.connect() as db:
        assert db.execute('SELECT hidden FROM web_event_states WHERE user_id=%s AND event_id=%s',
                          (p.users['alice'], EVENT['id'])).fetchone() == (0,)


def test_hide_rolls_back_if_access_expires_after_update_before_return(postgres_database, monkeypatch):
    p = postgres_database
    publish(p)
    original = p.repo._shared_in
    def expired_read(*args, **kwargs):
        p.now[0] += 61
        return original(*args, **kwargs)
    monkeypatch.setattr(p.repo, '_shared_in', expired_read)
    with pytest.raises(PermissionPending):
        p.repo.event_state(p.users['alice'], 'tenant', EVENT['id'], {'hidden': True})
    with p.connect() as db:
        assert db.execute('SELECT hidden FROM web_event_states WHERE user_id=%s AND event_id=%s',
                          (p.users['alice'], EVENT['id'])).fetchone() == (0,)


def test_refresh_skip_deadline_matches_actual_acl_expiry_after_slow_commit(postgres_database, monkeypatch):
    p = postgres_database
    original = p.repo.seal
    def slow_seal(value):
        p.now[0] += 2
        return original(value)
    monkeypatch.setattr(p.repo, 'seal', slow_seal)
    publish(p)
    with p.connect() as db:
        expires = db.execute('SELECT valid_until FROM web_channel_access WHERE user_id=%s',
                             (p.users['alice'],)).fetchone()[0]
    status = p.repo.sync_status(p.users['alice'])
    assert status['checked_at'] + 60 == expires
    assert p.now[0] > status['checked_at']
    p.now[0] = expires
    assert p.repo.begin_sync(p.users['alice'], 60)
