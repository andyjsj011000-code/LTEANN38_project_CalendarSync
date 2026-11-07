"""Behavioral checks using SQLite as a shim; not a PostgreSQL integration test."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_personal_tasks import PostgresPersonalTasks
from web_service.storage import Conflict, Missing


@pytest.fixture
def tasks(tmp_path):
    path = tmp_path / 'task.sqlite3'
    schema = (Path(__file__).parents[1] / 'web_service' / 'postgresql_schema_draft.sql').read_text(encoding='utf-8')
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute("INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES('alice','t','a','x')")
        db.execute("INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES('bob','t','b','x')")
    class Shim:
        def __init__(self, db): self.db = db
        def execute(self, sql, values=()):
            return self.db.execute(re.sub(r' FOR UPDATE', '', sql).replace('%s', '?'), values)
    @contextmanager
    def connect():
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys=ON')
            yield Shim(db)
    cipher = Fernet(Fernet.generate_key())
    seal = lambda payload: cipher.encrypt(json.dumps(payload).encode()).decode()
    unseal = lambda blob: json.loads(cipher.decrypt(blob.encode()))
    return PostgresPersonalTasks(connect, seal, unseal), path


def test_user_isolation_encryption_and_version_checks(tasks):
    repo, path = tasks
    payload = {'title': 'private task', 'start_date': '2026-11-05', 'end_date': '2026-11-05'}
    created = repo.add_task('alice', payload)
    with sqlite3.connect(path) as db:
        stored = db.execute('SELECT payload_blob FROM web_personal_tasks').fetchone()[0]
    assert 'private task' not in stored
    with pytest.raises(Missing):
        repo.task('bob', created['id'])
    updated = repo.task_state('alice', created['id'], {'completed': True}, created['version'])
    assert updated['completed'] and updated['version'] == created['version'] + 1
    with pytest.raises(Conflict):
        repo.delete_task('alice', created['id'], created['version'])
    repo.delete_task('alice', created['id'], updated['version'])
    with pytest.raises(Missing):
        repo.task('alice', created['id'])


def test_delete_removes_only_owned_personal_rules_after_version_check(tasks):
    repo, path = tasks
    payload = {'title': 'task', 'start_date': '2026-11-05', 'end_date': '2026-11-05'}
    item = repo.add_task('alice', payload)
    rules = {('alice', item['id'], 'personal'), ('alice', item['id'], 'teams'),
             ('bob', item['id'], 'personal'), ('alice', 'other-task', 'personal')}
    with sqlite3.connect(path) as db:
        db.executemany('INSERT INTO web_reminder_rules VALUES(?,?,?,?)',
                       [(*key, repo.seal({'offsets': [-30], 'custom_times': []})) for key in rules])
    changed = repo.task_state('alice', item['id'], {'completed': True}, item['version'])
    with pytest.raises(Missing):
        repo.delete_task('bob', item['id'], changed['version'])
    with pytest.raises(Conflict):
        repo.delete_task('alice', item['id'], item['version'])
    with sqlite3.connect(path) as db:
        assert set(db.execute('SELECT user_id,item_id,source FROM web_reminder_rules')) == rules
    repo.delete_task('alice', item['id'], changed['version'])
    with sqlite3.connect(path) as db:
        assert set(db.execute('SELECT user_id,item_id,source FROM web_reminder_rules')) == (
            rules - {('alice', item['id'], 'personal')})
    with pytest.raises(Missing):
        repo.task('alice', item['id'])


@pytest.mark.parametrize('operation', ['add', 'read', 'update', 'state', 'delete'])
@pytest.mark.parametrize('user_state', ['inactive', 'missing'])
def test_inactive_or_missing_user_cannot_read_or_change_tasks(tasks, operation, user_state):
    repo, path = tasks
    payload = {'title': 'private task', 'start_date': '2026-11-05', 'end_date': '2026-11-05'}
    created = repo.add_task('alice', payload)
    if user_state == 'inactive':
        with sqlite3.connect(path) as db:
            db.execute("UPDATE web_users SET active=0 WHERE id='alice'")
        user = 'alice'
    else:
        user = 'unknown'
    operations = {
        'add': lambda: repo.add_task(user, payload),
        'read': lambda: repo.task(user, created['id']),
        'update': lambda: repo.update_task(user, created['id'], {**payload, 'title': 'changed'}, 1),
        'state': lambda: repo.task_state(user, created['id'], {'completed': True}, 1),
        'delete': lambda: repo.delete_task(user, created['id'], 1),
    }
    with pytest.raises(Missing):
        operations[operation]()
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT payload_blob,completed,version FROM web_personal_tasks').fetchall()
    assert len(row) == 1
    assert repo.unseal(row[0][0])['title'] == 'private task'
    assert row[0][1:] == (0, 1)
