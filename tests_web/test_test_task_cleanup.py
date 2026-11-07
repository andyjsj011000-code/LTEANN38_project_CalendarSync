"""Personal task cleanup parity for the local school/test server adapter."""
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet

from web_service.storage import Conflict, Missing
from web_service.test_storage import TestDatabase


@pytest.fixture
def local_tasks(tmp_path):
    repo = TestDatabase(tmp_path / 'tasks.sqlite3', Fernet.generate_key().decode())
    repo.initialize()
    alice = repo.save_user('tenant', 'alice', {'name': 'Alice'})
    bob = repo.save_user('tenant', 'bob', {'name': 'Bob'})
    item = repo.add_task(alice, {
        'title': 'task', 'start_date': '2026-11-05', 'end_date': '2026-11-06'})
    return repo, alice, bob, item


def test_delete_removes_only_owned_personal_rules_after_version_check(local_tasks):
    repo, alice, bob, item = local_tasks
    rules = {(alice, item['id'], 'personal'), (alice, item['id'], 'teams'),
             (bob, item['id'], 'personal'), (alice, 'other-task', 'personal')}
    for key in rules:
        repo.save_rules(*key, {'offsets': [-30], 'custom_times': []})
    changed = repo.task_state(alice, item['id'], {'completed': True}, item['version'])
    with pytest.raises(Missing):
        repo.delete_task(bob, item['id'], changed['version'])
    with pytest.raises(Conflict):
        repo.delete_task(alice, item['id'], item['version'])
    with repo.connect() as db:
        assert {tuple(row) for row in db.execute(
            'SELECT user_id,item_id,source FROM web_reminder_rules')} == rules
    repo.delete_task(alice, item['id'], changed['version'])
    with repo.connect() as db:
        assert {tuple(row) for row in db.execute(
            'SELECT user_id,item_id,source FROM web_reminder_rules')} == (
                rules - {(alice, item['id'], 'personal')})
    with pytest.raises(Missing):
        repo.task(alice, item['id'])


def test_reminder_cleanup_failure_rolls_back_task_deletion(local_tasks, monkeypatch):
    repo, alice, _, item = local_tasks
    rule = {'offsets': [-30], 'custom_times': []}
    repo.save_rules(alice, item['id'], 'personal', rule)
    connect = repo.connect

    @contextmanager
    def failing_connect():
        with connect() as db:
            class Connection:
                def execute(self, query, values=()):
                    if query.startswith('DELETE FROM web_reminder_rules'):
                        raise RuntimeError('Simulated reminder cleanup failure')
                    return db.execute(query, values)
            yield Connection()

    monkeypatch.setattr(repo, 'connect', failing_connect)
    with pytest.raises(RuntimeError, match='Simulated reminder cleanup failure'):
        repo.delete_task(alice, item['id'], item['version'])
    assert repo.task(alice, item['id'])['version'] == item['version']
    assert repo.rules(alice, item['id'], 'personal') == rule
