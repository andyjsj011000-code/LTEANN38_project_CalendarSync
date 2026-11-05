"""SQLite-backed logic checks, not evidence of PostgreSQL compatibility."""
import json
import re
import sqlite3
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_preferences import PostgresPreferences
from web_service.storage import Missing


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'preferences.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE web_preferences(user_id TEXT PRIMARY KEY,payload_blob TEXT NOT NULL);
            CREATE TABLE web_reminder_rules(user_id TEXT NOT NULL,item_id TEXT NOT NULL,
                source TEXT NOT NULL,payload_blob TEXT NOT NULL,PRIMARY KEY(user_id,item_id,source));
            CREATE TABLE web_users(id TEXT PRIMARY KEY,active INTEGER NOT NULL);
            INSERT INTO web_users VALUES('alice',1),('bob',1);''')
    class Shim:
        def __init__(self, db): self.db = db
        def execute(self, query, values=()):
            return self.db.execute(re.sub(r' FOR UPDATE', '', query).replace('%s', '?'), values)
    @contextmanager
    def connect():
        with sqlite3.connect(path) as db: yield Shim(db)
    cipher = Fernet(Fernet.generate_key())
    seal = lambda data: cipher.encrypt(json.dumps(data).encode()).decode()
    unseal = lambda blob: json.loads(cipher.decrypt(blob.encode()))
    return PostgresPreferences(connect, seal, unseal), path


def test_preferences_and_rules_are_isolated_and_encrypted(store):
    repo, path = store
    assert repo.preferences('alice')['morning_time'] == '07:00'
    assert repo.rules('alice', 'task', 'personal') == {'offsets': [-1440, -60], 'custom_times': []}
    repo.save_preferences('alice', {'morning_time': '06:30'})
    repo.save_rules('alice', 'task', 'personal', {'offsets': [-30], 'custom_times': []})
    assert repo.preferences('alice')['morning_time'] == '06:30'
    assert repo.preferences('bob')['morning_time'] == '07:00'
    assert repo.rules('bob', 'task', 'personal')['offsets'] == [-1440, -60]
    assert repo.rules('alice', 'task', 'teams')['offsets'] == [-1440, -60]
    with sqlite3.connect(path) as db:
        blob = db.execute('SELECT payload_blob FROM web_preferences').fetchone()[0]
    assert '06:30' not in blob


@pytest.mark.parametrize('operation', ['preferences', 'rules', 'save_preferences', 'save_rules'])
@pytest.mark.parametrize('user_state', ['inactive', 'missing'])
def test_preferences_require_active_user_and_preserve_stored_values(store, operation, user_state):
    repo, path = store
    repo.save_preferences('alice', {'morning_time': '06:30'})
    repo.save_rules('alice', 'task', 'personal', {'offsets': [-30], 'custom_times': []})
    with sqlite3.connect(path) as db:
        original_preferences = db.execute('SELECT * FROM web_preferences').fetchall()
        original_rules = db.execute('SELECT * FROM web_reminder_rules').fetchall()
        if user_state == 'inactive':
            db.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    user = 'alice' if user_state == 'inactive' else 'unknown'
    operations = {
        'preferences': lambda: repo.preferences(user),
        'rules': lambda: repo.rules(user, 'task', 'personal'),
        'save_preferences': lambda: repo.save_preferences(user, {'morning_time': '10:00'}),
        'save_rules': lambda: repo.save_rules(user, 'task', 'personal', {'offsets': [], 'custom_times': []}),
    }
    with pytest.raises(Missing):
        operations[operation]()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM web_preferences').fetchall() == original_preferences
        assert db.execute('SELECT * FROM web_reminder_rules').fetchall() == original_rules
