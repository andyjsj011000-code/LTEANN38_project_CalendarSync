"""Reminder target access and writes share one real PostgreSQL transaction."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from test_postgres_database_integration import CHANNELS, postgres_database, source
from web_service.storage import Missing, PermissionPending


RULES = {'offsets': [-30], 'custom_times': []}


def publish(p):
    event = source('Shared deadline')
    lease = p.repo.begin_sync(p.users['alice'], 0)
    p.repo.commit_snapshot(p.devices['alice'], CHANNELS, [event], 60, lease)
    return event


def personal(p, user='alice'):
    return p.repo.add_task(p.users[user], {
        'title': 'Personal task', 'notes': '', 'time': None,
        'start_date': '2026-11-05', 'end_date': '2026-11-06',
        'display_mode': 'daily', 'selected_dates': [],
    })


def stored(p, item, kind):
    with p.connect() as db:
        row = db.execute('''SELECT payload_blob FROM web_reminder_rules
            WHERE user_id=%s AND item_id=%s AND source=%s''',
            (p.users['alice'], item, kind)).fetchone()
    return p.repo.unseal(row[0]) if row else None


def test_reminder_writes_require_current_target_and_owner(postgres_database):
    p = postgres_database
    event, task = publish(p), personal(p)
    p.repo.save_rules(p.users['alice'], event['id'], 'teams', RULES)
    p.repo.save_rules(p.users['alice'], task['id'], 'personal', RULES)
    assert stored(p, event['id'], 'teams') == stored(p, task['id'], 'personal') == RULES
    for identity, kind in ((event['id'], 'teams'), (task['id'], 'personal')):
        with pytest.raises(Missing):
            p.repo.save_rules(p.users['bob'], identity, kind, RULES)
    with pytest.raises(Missing):
        p.repo.save_rules(p.users['alice'], event['id'], 'other', RULES)


def test_task_deleted_after_api_lookup_cannot_get_an_orphan_rule(postgres_database):
    p = postgres_database
    task = personal(p)
    assert p.repo.task(p.users['alice'], task['id'])
    p.repo.delete_task(p.users['alice'], task['id'], task['version'])
    with pytest.raises(Missing):
        p.repo.save_rules(p.users['alice'], task['id'], 'personal', RULES)
    assert stored(p, task['id'], 'personal') is None


def test_permission_revoked_after_api_lookup_cannot_get_a_rule(postgres_database):
    p = postgres_database
    event = publish(p)
    assert p.repo._shared(p.users['alice'], 'tenant')
    p.repo.revoke_device(p.devices['alice']['id_hash'])
    with pytest.raises(Missing):
        p.repo.save_rules(p.users['alice'], event['id'], 'teams', RULES)
    assert stored(p, event['id'], 'teams') is None


def test_expired_proof_preserves_the_previous_rule(postgres_database):
    p = postgres_database
    event = publish(p)
    p.repo.save_rules(p.users['alice'], event['id'], 'teams', RULES)
    p.now[0] += 61
    with pytest.raises(PermissionPending):
        p.repo.save_rules(p.users['alice'], event['id'], 'teams', {'offsets': [], 'custom_times': []})
    assert stored(p, event['id'], 'teams') == RULES


def test_proof_expiring_while_waiting_for_event_lock_saves_nothing(postgres_database, monkeypatch):
    p = postgres_database
    event = publish(p)
    waiting = Event()
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:
            class Connection:
                def execute(self, query, values=()):
                    if query.startswith('SELECT channel_id FROM web_events WHERE id='):
                        waiting.set()
                    return db.execute(query, values)
            yield Connection()

    monkeypatch.setattr(p.repo, 'connect', intercepted)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with p.connect() as held:
            held.execute('SELECT id FROM web_events WHERE id=%s FOR UPDATE', (event['id'],))
            result = pool.submit(p.repo.save_rules, p.users['alice'], event['id'], 'teams', RULES)
            assert waiting.wait(2), 'The writer must reach the event lock'
            p.now[0] += 61
        with pytest.raises(PermissionPending):
            result.result(timeout=5)
    assert stored(p, event['id'], 'teams') is None


def test_expiry_after_rule_write_rolls_back_the_transaction(postgres_database, monkeypatch):
    p = postgres_database
    event = publish(p)
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:
            class Connection:
                def execute(self, query, values=()):
                    result = db.execute(query, values)
                    if query.startswith('INSERT INTO web_reminder_rules'):
                        p.now[0] += 61
                    return result
            yield Connection()

    monkeypatch.setattr(p.repo, 'connect', intercepted)
    with pytest.raises(PermissionPending):
        p.repo.save_rules(p.users['alice'], event['id'], 'teams', RULES)
    assert stored(p, event['id'], 'teams') is None
