"""Candidate behavior via SQLite shim; not a real PostgreSQL integration test."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_channels import PostgresChannels
from web_service.storage import Missing


@pytest.fixture
def channels(tmp_path):
    path = tmp_path / 'test.sqlite3'
    schema = (Path(__file__).parents[1] / 'web_service' / 'postgresql_schema_draft.sql').read_text(encoding='utf-8')
    cipher = Fernet(Fernet.generate_key())
    seal = lambda payload: cipher.encrypt(json.dumps(payload).encode()).decode()
    unseal = lambda blob: json.loads(cipher.decrypt(blob.encode()))
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.executemany('INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(?,?,?,?)',
                       [('alice','tenant','a','x'), ('bob','tenant','b','x')])
        db.executemany('INSERT INTO web_channels VALUES(?,?,?)',
                       [('class','tenant',seal({'name':'Class'})), ('foreign','other',seal({'name':'Other'}))])
        db.executemany('INSERT INTO web_channel_access VALUES(?,?,?,?)',
                       [('alice','class',900,1100), ('bob','class',900,1000), ('alice','foreign',900,1100)])
    class Shim:
        def __init__(self, db): self.db = db
        def execute(self, sql, values=()):
            return self.db.execute(re.sub(r'FOR UPDATE(?: OF a)?', '', sql).replace('%s', '?'), values)
    @contextmanager
    def connect():
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys=ON')
            yield Shim(db)
    return PostgresChannels(connect, seal, unseal, clock=lambda:1000)


def test_channel_access_requires_current_tenant_and_unexpired_proof(channels):
    assert channels.channels('alice','tenant') == [{'id':'class','name':'Class','enabled':True}]
    assert channels.channels('bob','tenant') == []
    with pytest.raises(Missing): channels.channel_preference('bob','tenant','class',False)
    with pytest.raises(Missing): channels.channel_preference('alice','tenant','foreign',False)
    channels.channel_preference('alice','tenant','class',False)
    assert channels.channels('alice','tenant')[0]['enabled'] is False
    assert channels.channels('alice','other') == []
    with pytest.raises(Missing): channels.channel_preference('alice','other','foreign',False)


def test_inactive_user_cannot_list_or_change_channels(channels):
    with channels.connect() as db: db.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    assert channels.channels('alice','tenant') == []
    with pytest.raises(Missing): channels.channel_preference('alice','tenant','class',False)


def test_preferences_are_user_specific_and_default_is_available(channels):
    default = channels.preferences('alice')
    assert default['morning_enabled'] is True
    custom = {**default,'morning_time':'08:00'}
    channels.save_preferences('alice', custom)
    assert channels.preferences('alice') == custom
    assert channels.preferences('bob') == default


def test_preference_rejects_proof_that_expires_during_lock_wait(channels):
    now = iter([1000, 1100])
    channels.clock = lambda: next(now)
    with pytest.raises(Missing):
        channels.channel_preference('alice','tenant','class',False)
