"""Offline and mocked transaction checks; no live connection or subprocesses."""
import builtins
from contextlib import contextmanager
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from web_service import postgres_bootstrap as bootstrap
from web_service.postgres_connection import PostgresConnectionFactory


def test_default_plan_is_offline_and_contains_no_destination_or_credentials(monkeypatch):
    original = builtins.__import__
    def no_driver(name, *args, **kwargs):
        if name == 'psycopg' or name.startswith('psycopg.'):
            pytest.fail('Offline planning must not import the PostgreSQL driver')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_driver)
    monkeypatch.setenv('DATABASE_URL', 'private-destination-that-must-not-be-read')
    plan = bootstrap.bootstrap_plan()
    assert plan['schema'] == 'calendarsync_web' and plan['applied'] is False
    assert plan['operation'] == 'create-new-schema-only'
    assert plan['existing_schema_action'] == 'reject' and not plan['automatic_migration']
    assert plan['create_tables'] > 20 and plan['create_indexes'] >= 4
    assert plan['draft_sha256'] == hashlib.sha256(bootstrap._DRAFT.read_bytes()).hexdigest()
    assert 'private-destination' not in json.dumps(plan)


def test_cli_without_apply_only_prints_an_offline_plan(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['postgres_bootstrap'])
    monkeypatch.setenv('DATABASE_URL', 'private-invalid-uri')
    monkeypatch.setenv('WEB_DATABASE_SCHEMA', 'calendarsync_web')
    def forbidden(*args, **kwargs): pytest.fail('Plan mode must not construct a connection')
    monkeypatch.setattr(bootstrap, 'PostgresConnectionFactory', forbidden)
    bootstrap.main()
    result = json.loads(capsys.readouterr().out)
    assert result['applied'] is False and result['schema'] == 'calendarsync_web'


@pytest.mark.parametrize('schema', ['public', 'information_schema', 'pg_temp', 'pg_catalog',
                                  'one,two', 'one.two', 'one;DROP SCHEMA other', '', None])
def test_plan_requires_a_single_dedicated_non_system_schema(schema):
    with pytest.raises(bootstrap.BootstrapRejected, match='dedicated'):
        bootstrap.bootstrap_plan(schema)


@pytest.mark.parametrize('extra', [
    'DROP TABLE web_schema_version', 'ALTER TABLE web_schema_version ADD bad TEXT',
    'ALTER TABLE web_worker_lock OWNER TO another_user',
    'TRUNCATE web_schema_version', 'DELETE FROM web_schema_version',
    'CREATE TABLE IF NOT EXISTS public.web_bad (id BIGINT)',
    'CREATE TABLE IF NOT EXISTS web_bad (id BIGINT REFERENCES public.other(id))',
    'CREATE TABLE IF NOT EXISTS web_bad (id BIGINT REFERENCES web_missing(id))',
    'CREATE TABLE IF NOT EXISTS web_bad (id BIGINT); UPDATE web_schema_version SET version=2',
])
def test_changed_draft_cannot_be_used_as_a_migration_or_cross_schema_runner(tmp_path, monkeypatch, extra):
    draft = tmp_path / 'draft.sql'
    original = bootstrap._DRAFT.read_text(encoding='utf-8')
    draft.write_text(original + '\n' + extra + ';', encoding='utf-8')
    monkeypatch.setattr(bootstrap, '_DRAFT', draft)
    with pytest.raises(bootstrap.BootstrapRejected): bootstrap.bootstrap_plan()


def test_creation_draft_accepts_worker_lock_owner_as_a_column_name(tmp_path, monkeypatch):
    draft = tmp_path / 'draft.sql'
    draft.write_text('''CREATE TABLE IF NOT EXISTS web_schema_version (version BIGINT PRIMARY KEY);
        INSERT INTO web_schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;
        CREATE TABLE IF NOT EXISTS web_worker_lock
            (id BIGINT PRIMARY KEY,owner TEXT NOT NULL,expires_at BIGINT NOT NULL);''', encoding='utf-8')
    monkeypatch.setattr(bootstrap, '_DRAFT', draft)
    plan = bootstrap.bootstrap_plan()
    assert plan['create_tables'] == 2 and plan['applied'] is False


@pytest.fixture
def fake_database(monkeypatch):
    state = SimpleNamespace(exists=False, created=False, statements=[], outcome=[],
                            fail_after=None, ddl_count=0, selected=None)
    class SQL:
        def __init__(self, value): self.value = value
        def format(self, identifier): return SimpleNamespace(sql=self.value, identifier=identifier.value)
    class Identifier:
        def __init__(self, value): self.value = value
    module = ModuleType('psycopg')
    module.sql = SimpleNamespace(SQL=SQL, Identifier=Identifier)
    monkeypatch.setitem(sys.modules, 'psycopg', module)
    class Result:
        def __init__(self, row=None, rows=()): self.row, self.rows = row, rows
        def fetchone(self): return self.row
        def fetchall(self): return list(self.rows)
    class Connection:
        def execute(self, sql, params=()):
            state.statements.append((sql, params))
            if not isinstance(sql, str):
                if sql.sql == 'CREATE SCHEMA {}': state.created = True
                if sql.sql == 'SET LOCAL search_path TO {}': state.selected = sql.identifier
                return Result()
            if 'pg_catalog.pg_namespace' in sql:
                return Result((1,) if state.exists else None)
            if sql == 'SELECT current_schema()': return Result((state.selected,))
            if sql == 'SELECT version FROM web_schema_version': return Result(rows=[(1,)])
            state.ddl_count += 1
            if state.fail_after == state.ddl_count:
                raise RuntimeError('private-connection-error-that-must-not-leak')
            return Result()
    @contextmanager
    def connect(factory):
        before = state.created, state.selected
        try:
            yield Connection()
        except BaseException:
            state.created, state.selected = before
            state.outcome.append('rollback')
            raise
        else:
            state.outcome.append('commit')
    monkeypatch.setattr(PostgresConnectionFactory, '__call__', connect)
    state.factory = PostgresConnectionFactory('postgresql://test_user@127.0.0.1/test_db',
                                             'calendarsync_web', sslmode='disable')
    return state


def test_explicit_apply_creates_only_the_new_schema_and_commits_once(fake_database):
    d = fake_database
    expected = bootstrap.bootstrap_plan()['draft_sha256']
    result = bootstrap.apply_bootstrap(d.factory, expected_sha256=expected)
    assert result['applied'] is True and d.created and d.outcome == ['commit']
    created = [sql for sql, _ in d.statements if not isinstance(sql, str) and sql.sql == 'CREATE SCHEMA {}']
    assert len(created) == 1 and created[0].identifier == 'calendarsync_web'
    assert d.selected == 'calendarsync_web'
    assert not any(isinstance(sql, str) and sql.lstrip().startswith(('ALTER', 'DROP', 'TRUNCATE', 'DELETE'))
                   for sql, _ in d.statements)


def test_existing_namespace_is_rejected_before_any_ddl(fake_database):
    d = fake_database; d.exists = True
    with pytest.raises(bootstrap.BootstrapRejected, match='already exists'):
        bootstrap.apply_bootstrap(d.factory)
    assert not d.created and d.outcome == ['rollback']
    assert len(d.statements) == 1 and 'pg_catalog.pg_namespace' in d.statements[0][0]


def test_mid_ddl_failure_rolls_back_namespace_and_redacts_diagnostics(fake_database):
    d = fake_database; d.fail_after = 2
    with pytest.raises(RuntimeError, match='bootstrap failed') as caught:
        bootstrap.apply_bootstrap(d.factory)
    assert not d.created and d.outcome == ['rollback']
    assert 'private-connection-error' not in str(caught.value) and caught.value.__suppress_context__


def test_mismatched_review_hash_refuses_before_connecting(fake_database):
    d = fake_database
    with pytest.raises(bootstrap.BootstrapRejected, match='hash'):
        bootstrap.apply_bootstrap(d.factory, expected_sha256='0' * 64)
    assert d.statements == [] and d.outcome == []


def test_existence_helper_is_read_only_and_uses_only_the_explicit_schema(fake_database):
    d = fake_database
    assert bootstrap.schema_exists(d.factory) is False
    d.exists = True
    assert bootstrap.schema_exists(d.factory) is True
    assert d.outcome == ['commit', 'commit'] and not d.created
    assert len(d.statements) == 2
    assert all('pg_catalog.pg_namespace' in sql and params == ('calendarsync_web',)
               for sql, params in d.statements)
