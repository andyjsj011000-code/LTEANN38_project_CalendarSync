"""Planner installation is opt-in, additive, and transactional."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from test_hosted import config_env as config_env

from web_service import hosted, planner_schema
from web_service.postgres_connection import PostgresConnectionFactory
from web_service.postgres_database import PostgresDatabase
from web_service.test_storage import TestDatabase


def test_hosted_planner_requires_explicit_opt_in(config_env, monkeypatch):
    import web_service.server as server

    calls, databases = [], []
    monkeypatch.setattr(
        planner_schema,
        "enable_planner_schema",
        lambda connection, tenant: calls.append((connection.schema, tenant)),
    )
    monkeypatch.setattr(
        server,
        "create_test_server",
        lambda settings, database, *args, **kwargs: databases.append(database) or "app",
    )
    assert hosted.configuration(config_env).describe()["planner_enhancements"] is False
    assert hosted.create_hosted_app(config_env, worker=False) == "app"
    assert calls == [] and databases[-1].planner_enhancements_enabled is False
    env = {**config_env, "WEB_PLANNER_ENHANCEMENTS": "1"}
    assert hosted.configuration(env).describe()["planner_enhancements"] is True
    assert hosted.create_hosted_app(env, worker=False) == "app"
    assert calls == [("calendarsync_web", config_env["WEB_ENTRA_TENANT_ID"])]
    assert databases[-1].planner_enhancements_enabled is True


@pytest.mark.parametrize("value", ["", "true", "yes", "2", 1, True, None])
def test_hosted_planner_rejects_ambiguous_flag_values(config_env, value):
    with pytest.raises(ValueError):
        hosted.configuration({**config_env, "WEB_PLANNER_ENHANCEMENTS": value})


def test_database_preflight_with_planner_flag_never_calls_installer(
    config_env, monkeypatch, capsys
):
    import sys

    calls = []
    config = hosted.configuration({**config_env, "WEB_PLANNER_ENHANCEMENTS": "1"})
    monkeypatch.setattr(hosted, "configuration", lambda: config)
    monkeypatch.setattr(
        planner_schema,
        "enable_planner_schema",
        lambda *args: pytest.fail("Read-only preflight cannot install schema"),
    )
    monkeypatch.setattr(
        PostgresDatabase, "initialize", lambda db: calls.append(db.planner_enhancements_enabled)
    )
    monkeypatch.setattr(sys, "argv", ["hosted", "--check-database"])
    hosted.main()
    assert calls == [True]
    assert '"database_checked": true' in capsys.readouterr().out


@pytest.fixture
def sqlite_planner(tmp_path):
    db = TestDatabase(tmp_path / "planner.sqlite3", Fernet.generate_key().decode())
    db.initialize()
    user = db.save_user("school", "alice", {"name": "Preserved profile"})
    task = db.add_task(
        user, {"title": "Preserved task", "start_date": "2026-11-07", "end_date": "2026-11-07"}
    )
    return SimpleNamespace(db=db, user=user, task=task)


def feature_tables(db):
    with db.connect() as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } & planner_schema.PLANNER_COLUMNS.keys()


def test_sqlite_default_keeps_existing_schema_and_does_not_install_sidecars(sqlite_planner):
    p = sqlite_planner
    assert not p.db.planner_enhancements_enabled
    assert feature_tables(p.db) == set()
    assert p.db.planner_maintenance() == {"drafts_expired": 0, "trash_expired": 0}


def test_sqlite_repeated_and_concurrent_install_preserves_data(sqlite_planner):
    p = sqlite_planner
    with p.db.connect() as connection:
        original = tuple(connection.execute("SELECT * FROM web_personal_tasks").fetchone())
    p.db.planner_enhancements_enabled = True
    p.db.initialize()
    with p.db.connect() as connection:
        connection.execute(
            "INSERT INTO web_task_drafts VALUES(?,?,?,?,?)",
            (p.user, "preserved-ciphertext", 3, 100, 200),
        )
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: p.db.initialize(), range(3)))
    with p.db.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM web_personal_tasks").fetchone()) == original
        assert tuple(connection.execute("SELECT * FROM web_task_drafts").fetchone()) == (
            p.user,
            "preserved-ciphertext",
            3,
            100,
            200,
        )
    assert feature_tables(p.db) == set(planner_schema.PLANNER_COLUMNS)


def test_sqlite_partial_install_rolls_back_new_sidecars(sqlite_planner):
    p = sqlite_planner
    with p.db.connect() as connection:
        connection.execute("CREATE TABLE web_task_trash(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO web_task_trash VALUES('preserve-malformed-existing-table')")
    p.db.planner_enhancements_enabled = True
    with pytest.raises(Exception):
        p.db.initialize()
    assert feature_tables(p.db) == {"web_task_trash"}
    with p.db.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM web_task_trash").fetchone()) == (
            "preserve-malformed-existing-table",
        )
        assert connection.execute("SELECT count(*) FROM web_personal_tasks").fetchone()[0] == 1


def test_planner_maintenance_only_runs_when_enabled(sqlite_planner, monkeypatch):
    db, calls = sqlite_planner.db, []
    monkeypatch.setattr(db, "prune_task_drafts", lambda: calls.append("drafts") or 2)
    monkeypatch.setattr(db, "prune_task_trash", lambda: calls.append("trash") or 3)
    assert db.planner_maintenance() == {"drafts_expired": 0, "trash_expired": 0}
    assert calls == []
    db.planner_enhancements_enabled = True
    assert db.planner_maintenance() == {"drafts_expired": 2, "trash_expired": 3}
    assert calls == ["drafts", "trash"]


class RecordingFactory(PostgresConnectionFactory):
    def __init__(self, *, schema="calendarsync_test", current=None, version=1, foreign=False):
        super().__init__("postgresql://test:test@db.example.test/test", schema)
        self.current, self.version, self.foreign = current or schema, version, foreign
        self.statements = []

    @contextmanager
    def __call__(self):
        factory = self

        class Connection:
            def execute(self, statement, params=()):
                factory.statements.append(statement)
                if statement == "SELECT current_schema()":
                    rows = [(factory.current,)]
                elif statement == "SELECT version FROM web_schema_version":
                    rows = [(factory.version,)]
                elif "tenant_id<>" in statement:
                    rows = [(1,)] if factory.foreign else []
                else:
                    rows = []
                return SimpleNamespace(
                    fetchone=lambda: rows[0] if rows else None, fetchall=lambda: rows
                )

        yield Connection()


@pytest.mark.parametrize("options", [{"current": "public"}, {"version": 2}, {"foreign": True}])
def test_postgres_install_rejects_incompatible_base_before_any_create(options):
    factory = RecordingFactory(**options)
    with pytest.raises(ValueError):
        planner_schema.enable_planner_schema(factory, "school")
    assert not any(query.startswith("CREATE") for query in factory.statements)


@pytest.mark.parametrize("schema", ["public", "information_schema"])
def test_postgres_installer_rejects_shared_or_system_namespace_without_connecting(schema):
    factory = RecordingFactory(schema=schema)
    with pytest.raises(ValueError):
        planner_schema.enable_planner_schema(factory, "school")
    assert factory.statements == []


def test_postgres_install_has_only_closed_create_targets_and_serializes_starts():
    factory = RecordingFactory()
    planner_schema.enable_planner_schema(factory, "school")
    statements = factory.statements
    lock = statements.index("SELECT pg_advisory_xact_lock(731904227)")
    creates = [query for query in statements if query.startswith("CREATE")]
    assert len(creates) == 5 and all("IF NOT EXISTS" in query for query in creates)
    assert all(statements.index(query) > lock for query in creates)
    assert not any(
        query.startswith(("ALTER", "DROP", "UPDATE", "DELETE", "INSERT")) for query in statements
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_postgres_initialize_is_always_read_only_and_flag_gates_feature_validation(enabled):
    factory = RecordingFactory()
    db = PostgresDatabase(
        factory, Fernet.generate_key().decode(), "school", planner_enhancements_enabled=enabled
    )
    db.initialize()
    assert all(query.startswith("SELECT") for query in factory.statements)
    assert any("FROM web_task_drafts" in query for query in factory.statements) is enabled
