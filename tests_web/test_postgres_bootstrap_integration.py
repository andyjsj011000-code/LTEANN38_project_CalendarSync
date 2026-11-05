"""Opt-in bootstrap checks on an explicitly disposable PostgreSQL database."""
from contextlib import contextmanager
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_bootstrap import apply_bootstrap, bootstrap_plan, schema_exists, BootstrapRejected
from web_service.postgres_connection import PostgresConnectionFactory
from web_service.postgres_database import PostgresDatabase


@pytest.fixture
def bootstrap_database():
    dsn = os.environ.get('CALENDARSYNC_DISPOSABLE_POSTGRES_DSN')
    if not dsn or os.environ.get('CALENDARSYNC_ALLOW_TEMP_SCHEMA') != 'yes':
        pytest.skip('A dedicated disposable PostgreSQL database must be explicitly enabled')
    psycopg = pytest.importorskip('psycopg')
    schema = 'calendarsync_test_' + uuid.uuid4().hex
    mode = 'disable' if urlsplit(dsn).hostname in {'127.0.0.1', 'localhost', '::1'} else None
    factory = PostgresConnectionFactory(dsn, schema, sslmode=mode,
                                        statement_timeout_ms=5000, lock_timeout_ms=4000)
    try:
        yield SimpleNamespace(factory=factory, schema=schema)
    finally:
        # This identifier was generated here, never read from a user parameter.
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
            connection.execute("SET statement_timeout='5s'")
            connection.execute('DROP SCHEMA IF EXISTS ' + schema + ' CASCADE')


def test_new_namespace_and_schema_are_created_atomically(bootstrap_database):
    p = bootstrap_database
    assert not schema_exists(p.factory)
    result = apply_bootstrap(p.factory, expected_sha256=bootstrap_plan(p.schema)['draft_sha256'])
    assert result['applied'] is True
    assert schema_exists(p.factory)
    db = PostgresDatabase(p.factory, Fernet.generate_key().decode(), 'tenant')
    db.initialize()
    with p.factory() as connection:
        assert connection.execute('SELECT current_schema()').fetchone() == (p.schema,)
        assert connection.execute('SELECT version FROM web_schema_version').fetchall() == [(1,)]
        assert connection.execute('SELECT count(*) FROM web_users').fetchone()[0] == 0


def test_existing_namespace_and_its_data_are_preserved(bootstrap_database):
    from psycopg import sql
    p = bootstrap_database
    with p.factory() as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(p.schema)))
        connection.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(p.schema)))
        connection.execute('CREATE TABLE sentinel(id BIGINT PRIMARY KEY, payload TEXT)')
        connection.execute("INSERT INTO sentinel VALUES(1,'preserve-test-data')")
    with pytest.raises(BootstrapRejected, match='already exists'):
        apply_bootstrap(p.factory)
    with p.factory() as connection:
        assert connection.execute('SELECT payload FROM sentinel').fetchall() == [('preserve-test-data',)]
        assert connection.execute("SELECT to_regclass('web_users')").fetchone() == (None,)


def test_middle_ddl_error_rolls_back_the_namespace_itself(bootstrap_database):
    p = bootstrap_database
    class FailingFactory(PostgresConnectionFactory):
        @contextmanager
        def __call__(self):
            with super().__call__() as connection:
                class Proxy:
                    table_count = 0
                    def execute(self, statement, params=None):
                        if isinstance(statement, str) and statement.startswith('CREATE TABLE'):
                            self.table_count += 1
                            if self.table_count == 2:
                                connection.execute('SELECT 1/0')
                        return (connection.execute(statement) if params is None else
                                connection.execute(statement, params))
                yield Proxy()
    # Only reuse the explicitly designated disposable target; no DSN is put
    # into fixture metadata, assertion text or returned diagnostics.
    dsn = os.environ['CALENDARSYNC_DISPOSABLE_POSTGRES_DSN']
    failing = FailingFactory(dsn, p.schema, sslmode=p.factory.sslmode,
                             statement_timeout_ms=5000, lock_timeout_ms=4000)
    with pytest.raises(RuntimeError, match='bootstrap failed'):
        apply_bootstrap(failing)
    with p.factory() as connection:
        assert connection.execute('SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s',
                                  (p.schema,)).fetchone() is None


def test_hosted_first_start_creates_schema_and_restart_preserves_existing_data(bootstrap_database):
    from conftest import TENANT,CLIENT
    import test_server as contract
    from web_service.hosted import create_hosted_app
    p=bootstrap_database
    env={'WEB_STORAGE_BACKEND':'postgresql','WEB_BOOTSTRAP_NEW_SCHEMA':'1',
         'DATABASE_URL':os.environ['CALENDARSYNC_DISPOSABLE_POSTGRES_DSN'],
         'WEB_DATABASE_SCHEMA':p.schema,'WEB_DATABASE_SSLMODE':p.factory.sslmode,
         'WEB_DATA_KEY':Fernet.generate_key().decode(),'WEB_OPENAI_API_KEY':'test-only-key',
         'WEB_ENTRA_TENANT_ID':TENANT,'WEB_ENTRA_CLIENT_ID':CLIENT,
         'WEB_ENTRA_CLIENT_SECRET':'test-only-secret','RENDER_EXTERNAL_URL':'https://beta.example.test'}
    def app():
        return create_hosted_app(env,identity=contract.Identity(lambda:1000),graph=contract.Graph(),
                                 source=contract.Source(),sender=contract.Sender(),worker=False)
    assert app().state.database.tenant_id==TENANT
    with p.factory() as connection:
        connection.execute('CREATE TABLE sentinel(id BIGINT PRIMARY KEY,payload TEXT)')
        connection.execute("INSERT INTO sentinel VALUES(1,'preserve-on-restart')")
    assert app().state.database.tenant_id==TENANT
    with p.factory() as connection:
        assert connection.execute('SELECT payload FROM sentinel').fetchall()==[('preserve-on-restart',)]
