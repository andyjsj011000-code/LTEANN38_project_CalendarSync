"""Opt-in integration tests: use only a dedicated disposable PostgreSQL DB."""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_personal_tasks import PostgresPersonalTasks
from web_service.storage import Conflict, Missing


@pytest.fixture
def postgres_tasks():
    dsn = os.environ.get('CALENDARSYNC_DISPOSABLE_POSTGRES_DSN')
    if not dsn:
        pytest.skip('No dedicated disposable PostgreSQL database configured')
    if os.environ.get('CALENDARSYNC_ALLOW_TEMP_SCHEMA') != 'yes':
        pytest.skip('Disposable database and temporary schema creation must be explicitly enabled')
    psycopg = pytest.importorskip('psycopg')
    schema = 'calendarsync_test_' + uuid.uuid4().hex
    ddl = (Path(__file__).parents[1] / 'web_service' / 'postgresql_schema_draft.sql').read_text(encoding='utf-8')
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute('CREATE SCHEMA ' + schema)

    @contextmanager
    def connect():
        with psycopg.connect(dsn) as db:
            db.execute('SET LOCAL search_path TO ' + schema)
            yield db

    try:
        with connect() as db:
            # Strip line comments before splitting: comments may contain ';'.
            statements = '\n'.join(line for line in ddl.splitlines()
                                   if not line.lstrip().startswith('--'))
            for statement in statements.split(';'):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES('alice','t','a','x'),('bob','t','b','x')")
        cipher = Fernet(Fernet.generate_key())
        def seal(payload): return cipher.encrypt(json.dumps(payload).encode()).decode()
        def unseal(blob): return json.loads(cipher.decrypt(blob.encode()))
        yield PostgresPersonalTasks(connect, seal, unseal), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute('DROP SCHEMA ' + schema + ' CASCADE')


def test_task_conflict_and_access_control(postgres_tasks):
    repo, connect = postgres_tasks
    payload = {'title': 'private content', 'start_date': '2026-11-05', 'end_date': '2026-11-06'}
    item = repo.add_task('alice', payload)
    with connect() as db:
        blob = db.execute('SELECT payload_blob FROM web_personal_tasks WHERE id=%s', (item['id'],)).fetchone()[0]
    assert 'private content' not in blob
    with pytest.raises(Missing):
        repo.task('bob', item['id'])

    def update(_):
        try:
            return repo.task_state('alice', item['id'], {'completed': True}, item['version'])
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, range(2)))
    assert sum(result is not None for result in results) == 1
    assert repo.task('alice', item['id'])['version'] == item['version'] + 1


def test_private_task_cannot_be_modified_by_another_user(postgres_tasks):
    repo, _ = postgres_tasks
    payload = {'title': 'private content', 'start_date': '2026-11-05', 'end_date': '2026-11-06'}
    item = repo.add_task('alice', payload)
    with pytest.raises(Missing):
        repo.update_task('bob', item['id'], payload, item['version'])
    with pytest.raises(Missing):
        repo.delete_task('bob', item['id'], item['version'])
    with pytest.raises(Missing):
        repo.task_state('bob', item['id'], {'completed': True}, item['version'])
    assert repo.task('alice', item['id'])['version'] == item['version']


def test_update_and_delete_obey_latest_version(postgres_tasks):
    repo, _ = postgres_tasks
    payload = {'title': 'initial', 'start_date': '2026-11-05', 'end_date': '2026-11-06'}
    item = repo.add_task('alice', payload)
    changed = repo.update_task('alice', item['id'], {**payload, 'title': 'changed'}, item['version'])
    assert changed['title'] == 'changed'
    assert changed['version'] == item['version'] + 1
    with pytest.raises(Conflict):
        repo.delete_task('alice', item['id'], item['version'])
    repo.delete_task('alice', item['id'], changed['version'])
    with pytest.raises(Missing):
        repo.task('alice', item['id'])
