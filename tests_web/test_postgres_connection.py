"""Configuration and mocked-driver contract only; no sockets or PostgreSQL."""

import builtins
from contextlib import contextmanager
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from web_service.postgres_connection import PostgresConnectionFactory


URI = "postgresql://private-user:private%40password@private-db.example.test:5433/private_database"


@pytest.fixture
def driver(monkeypatch):
    calls, executions, results = [], [], []

    class DriverError(Exception):
        pass

    class SQL:
        def __init__(self, value):
            self.value = value

        def format(self, identifier):
            return SimpleNamespace(sql=self.value, identifier=identifier.value)

    class Identifier:
        def __init__(self, value):
            self.value = value

    class Connection:
        fail_setup = False

        def execute(self, sql, params=()):
            executions.append((sql, params))
            if self.fail_setup:
                raise DriverError(URI)

    connection = Connection()

    @contextmanager
    def connect(**options):
        calls.append(options)
        try:
            yield connection
        except BaseException:
            results.append("rollback-close")
            raise
        else:
            results.append("commit-close")

    module = ModuleType("psycopg")
    module.Error, module.connect = DriverError, connect
    module.sql = SimpleNamespace(SQL=SQL, Identifier=Identifier)
    rows = ModuleType("psycopg.rows")
    rows.tuple_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    return SimpleNamespace(
        module=module,
        rows=rows,
        connection=connection,
        calls=calls,
        executions=executions,
        results=results,
    )


def test_validation_and_description_do_not_import_driver_or_expose_connection(monkeypatch):
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            pytest.fail("Configuration validation must not import a database driver")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    factory = PostgresConnectionFactory(URI, "calendarsync_web")
    text = repr(factory) + json.dumps(factory.describe())
    for value in (URI, "private-user", "private@password", "private-db", "private_database"):
        assert value not in text
    assert factory.describe()["sslmode"] == "verify-full"


def test_context_uses_explicit_parameters_tuple_rows_schema_identifier_and_local_timeouts(driver):
    factory = PostgresConnectionFactory(URI, "calendarsync_web", sslrootcert="reviewed-ca.pem")
    with factory() as connection:
        assert connection is driver.connection
        connection.execute("SELECT application_work")
    assert driver.calls == [
        {
            "host": "private-db.example.test",
            "port": 5433,
            "user": "private-user",
            "password": "private@password",
            "dbname": "private_database",
            "sslmode": "verify-full",
            "sslrootcert": "reviewed-ca.pem",
            "connect_timeout": 5,
            "autocommit": False,
            "row_factory": driver.rows.tuple_row,
        }
    ]
    schema, _ = driver.executions[0]
    assert schema.sql == "SET LOCAL search_path TO {}" and schema.identifier == "calendarsync_web"
    assert driver.executions[1][1] == ("15000ms",)
    assert driver.executions[2][1] == ("5000ms",)
    assert driver.results == ["commit-close"]
    assert not any(
        isinstance(sql, str) and sql.startswith(("CREATE", "ALTER", "DROP"))
        for sql, _ in driver.executions
    )


def test_business_exception_rolls_back_and_is_not_replaced(driver):
    class DomainMissing(Exception):
        pass

    with pytest.raises(DomainMissing, match="domain"):
        with PostgresConnectionFactory(URI, "calendarsync_web")():
            raise DomainMissing("domain")
    assert driver.results == ["rollback-close"]


@pytest.mark.parametrize("where", ["connect", "setup", "application"])
def test_driver_failures_are_redacted_and_never_retried_with_weaker_tls(driver, where):
    if where == "connect":

        def fail(**options):
            driver.calls.append(options)
            raise driver.module.Error(URI)

        driver.module.connect = fail
    elif where == "setup":
        driver.connection.fail_setup = True
    with pytest.raises(RuntimeError, match="connection or transaction failed") as caught:
        with PostgresConnectionFactory(URI, "calendarsync_web")():
            if where == "application":
                raise driver.module.Error(URI)
    assert URI not in str(caught.value) and caught.value.__suppress_context__
    assert len(driver.calls) == 1 and driver.calls[0]["sslmode"] == "verify-full"
    if where != "connect":
        assert driver.results == ["rollback-close"]


def test_driver_import_is_lazy_and_missing_installation_has_a_safe_error(monkeypatch):
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "psycopg":
            raise ImportError("sensitive driver import diagnostic")
        return original(name, *args, **kwargs)

    factory = PostgresConnectionFactory(URI, "calendarsync_web")
    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(RuntimeError, match="optional PostgreSQL driver") as caught:
        with factory():
            pytest.fail("Missing driver must not yield a connection")
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize(
    "uri",
    [
        "dbname=private_database host=private-db password=secret",
        "https://user:secret@example.test/db",
        "postgresql:///db",
        "postgresql://user:secret@example.test/",
        "postgresql://:secret@example.test/db",
        "postgresql://user@example.test/db",
        "postgresql://user:secret@example.test:0/db",
        "postgresql://user:secret@example.test:65536/db",
        "postgresql://user:secret@example.test:bad/db",
        "postgresql://user:secret@example.test/db#fragment",
        "postgresql://user:secret@example.test/db?host=other.example.test",
        "postgresql://user:secret@example.test/db?options=-c%20search_path%3Dpublic",
        "postgresql://user:secret@example.test/db?sslmode=require&sslmode=verify-full",
        "postgresql://user:secret@example.test/db?sslmode=prefer",
        "postgresql://user:secret@example.test/db?sslmode=",
        "postgresql://user:bad%0Asecret@example.test/db",
        "postgresql://user:bad%GGsecret@example.test/db",
        "postgresql://user:secret@example.test/db%2Fanother",
        "postgresql://user:secret@127.0.0.1,other.example.test/db",
        "postgresql://user:secret@0.0.0.0/db",
        "postgresql://user:secret@[::]/db",
    ],
)
def test_invalid_uri_never_echoes_its_value(uri):
    with pytest.raises(ValueError, match="Invalid PostgreSQL URI") as caught:
        PostgresConnectionFactory(uri, "calendarsync_web")
    assert uri not in str(caught.value) and caught.value.__suppress_context__


@pytest.mark.parametrize(
    "schema",
    ["public,other", "pg_catalog", "x;DROP TABLE users", '"schema"', "x.y", "a" * 64, "", None],
)
def test_schema_must_be_a_single_closed_identifier(schema):
    with pytest.raises(ValueError, match="schema identifier"):
        PostgresConnectionFactory(URI, schema)


@pytest.mark.parametrize(
    "options",
    [
        {"connect_timeout": 0},
        {"connect_timeout": True},
        {"connect_timeout": 31},
        {"statement_timeout_ms": 0},
        {"statement_timeout_ms": 120001},
        {"lock_timeout_ms": 0},
        {"lock_timeout_ms": 15001},
        {"lock_timeout_ms": "5000"},
    ],
)
def test_timeouts_are_bounded_positive_integers(options):
    with pytest.raises(ValueError, match="timeout configuration"):
        PostgresConnectionFactory(URI, "calendarsync_web", **options)


def test_tls_downgrade_is_explicit_and_plaintext_is_loopback_only(driver):
    assert (
        PostgresConnectionFactory(URI + "?sslmode=require", "calendarsync_web").sslmode == "require"
    )
    with pytest.raises(ValueError, match="Conflicting"):
        PostgresConnectionFactory(
            URI + "?sslmode=require", "calendarsync_web", sslmode="verify-full"
        )
    with pytest.raises(ValueError, match="loopback"):
        PostgresConnectionFactory(URI, "calendarsync_web", sslmode="disable")
    for host in ("localhost", "127.0.0.1", "[::1]"):
        local = PostgresConnectionFactory(
            "postgresql://test_user@" + host + "/test_db", "calendarsync_web", sslmode="disable"
        )
        with local():
            pass
    assert all(call["sslmode"] == "disable" for call in driver.calls)


@pytest.mark.parametrize("value", ["", "\nsecret", "a" * 1025, 1])
def test_invalid_ca_configuration_is_rejected_without_echo(value):
    with pytest.raises(ValueError, match="certificate configuration"):
        PostgresConnectionFactory(URI, "calendarsync_web", sslrootcert=value)
