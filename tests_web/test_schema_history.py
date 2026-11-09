"""History adoption/upgrade contracts on isolated, existing SQLite databases."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.beta import BETA_SQL, BETA_TENANT, BetaDatabase
from web_service.planner_schema import planner_statements
from web_service.schema_catalog import PROFILES, REVISIONS, HistoryRejected, schema_plan
from web_service.schema_history import SQLiteHistory
from web_service.school_storage import SCHOOL_SCHEMA, SchoolDatabase
from web_service.storage import Database
from web_service.test_storage import EXTRA_SCHEMA, TestDatabase


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        names = sorted(
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        return {
            name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
            for name in names
            if name != "web_schema_history"
        }


def make_state(tmp_path, profile):
    path = tmp_path / "history.sqlite3"
    adapter = {
        "sqlite-foundation": Database,
        "sqlite-school": SchoolDatabase,
        "sqlite-server": TestDatabase,
        "sqlite-beta": BetaDatabase,
    }[profile](path, Fernet.generate_key().decode())
    adapter.initialize()
    tenant = BETA_TENANT if profile == "sqlite-beta" else "tenant"
    user = adapter.save_user(tenant, "test-student", {"name": "test-only-name"})
    task = adapter.add_task(
        user, {"title": "keep private task", "start_date": "2026-11-09", "end_date": "2026-11-09"}
    )
    with adapter.connect() as db:
        db.execute("CREATE TABLE sentinel(payload TEXT)")
        db.execute("INSERT INTO sentinel VALUES('keep-test-data')")
    return SimpleNamespace(
        path=path,
        adapter=adapter,
        user=user,
        task=task,
        history=SQLiteHistory(path, profile, clock=lambda: 1000),
    )


@pytest.fixture(params=[p for p in PROFILES if p.startswith("sqlite-")])
def state(tmp_path, request):
    return make_state(tmp_path, request.param)


def test_read_only_check_does_not_add_history_or_touch_existing_data(state):
    original = state.path.read_bytes()
    result = state.history.check()
    assert not result["tracked"] and result["recorded"] == []
    assert result["unrecorded"] == list(PROFILES[state.history.profile])
    assert state.path.read_bytes() == original


def test_baseline_records_observation_once_and_old_startup_still_works(state):
    original = snapshot(state.path)
    plan = state.history.plan()
    first = state.history.baseline(expected_sha256=plan["plan_sha256"])
    assert first["tracked"] and len(first["changed"]) == len(PROFILES[state.history.profile])
    assert {row["kind"] for row in first["recorded"]} == {"observed"}
    assert {row["recorded_at"] for row in first["recorded"]} == {1000}
    again = state.history.baseline(expected_sha256=plan["plan_sha256"])
    assert again["changed"] == [] and again["recorded"] == first["recorded"]
    state.adapter.initialize()
    assert state.history.check()["tracked"]
    assert snapshot(state.path) == original


def test_wrong_hash_and_future_version_are_rejected_without_adoption(state):
    original = snapshot(state.path)
    with pytest.raises(HistoryRejected, match="hash does not match"):
        state.history.baseline(expected_sha256="0" * 64)
    assert snapshot(state.path) == original
    with state.adapter.connect() as db:
        db.execute("UPDATE web_schema_version SET version=2")
    before = state.path.read_bytes()
    with pytest.raises(HistoryRejected, match="base schema version"):
        state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    assert state.path.read_bytes() == before


def test_failed_baseline_rolls_back_the_history_table_and_rows(state):
    class Failure(SQLiteHistory):
        def _record(self, db, id, kind):
            super()._record(db, id, kind)
            raise RuntimeError("test-only failure after insert")

    original = snapshot(state.path)
    history = Failure(state.path, state.history.profile)
    with pytest.raises(RuntimeError, match="update failed"):
        history.baseline(expected_sha256=history.plan()["plan_sha256"])
    assert state.history.check()["recorded"] == []
    with state.adapter.connect() as db:
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='web_schema_history'").fetchone()
            is None
        )
    assert snapshot(state.path) == original


@pytest.mark.parametrize("mutation", ["checksum", "unknown", "dependency", "profile", "structure"])
def test_modified_or_incomplete_history_is_never_rewritten(tmp_path, mutation):
    state = make_state(tmp_path, "sqlite-server")
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    with state.adapter.connect() as db:
        if mutation == "checksum":
            db.execute(
                "UPDATE web_schema_history SET checksum=? WHERE revision='sqlite-base-v1'",
                ("0" * 64,),
            )
        elif mutation == "unknown":
            db.execute(
                "INSERT INTO web_schema_history VALUES('future-revision',?,'sqlite-server','applied',1001)",
                ("0" * 64,),
            )
        elif mutation == "dependency":
            db.execute("DELETE FROM web_schema_history WHERE revision='sqlite-base-v1'")
        elif mutation == "profile":
            db.execute("UPDATE web_schema_history SET profile='sqlite-beta'")
        else:
            db.execute("DROP INDEX web_personal_dates")
    original = state.path.read_bytes()
    with pytest.raises(HistoryRejected):
        state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    assert state.path.read_bytes() == original


def test_explicit_planner_upgrade_preserves_data_and_is_repeatable(tmp_path):
    state = make_state(tmp_path, "sqlite-server")
    plan = state.history.plan("planner")
    with pytest.raises(HistoryRejected, match="baseline before upgrading"):
        state.history.upgrade("planner", expected_sha256=plan["plan_sha256"])
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    original = snapshot(state.path)
    result = state.history.upgrade("planner", expected_sha256=plan["plan_sha256"])
    assert result["changed"] == ["sqlite-planner-v1"]
    assert (
        next(row for row in result["recorded"] if row["id"] == "sqlite-planner-v1")["kind"]
        == "applied"
    )
    assert all(snapshot(state.path)[name] == values for name, values in original.items())
    assert state.history.upgrade("planner", expected_sha256=plan["plan_sha256"])["changed"] == []
    state.adapter.planner_enhancements_enabled = True
    state.adapter.initialize()
    assert state.adapter.task(state.user, state.task["id"])["title"] == "keep private task"


def test_feature_ddl_and_history_roll_back_together(tmp_path):
    state = make_state(tmp_path, "sqlite-server")
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    before = state.history.check()

    class Failure(SQLiteHistory):
        def _record(self, db, id, kind):
            super()._record(db, id, kind)
            if kind == "applied":
                raise RuntimeError("test-only post-DDL failure")

    history = Failure(state.path, "sqlite-server")
    with pytest.raises(RuntimeError, match="update failed"):
        history.upgrade("planner", expected_sha256=history.plan("planner")["plan_sha256"])
    assert state.history.check() == before


def test_existing_opt_in_is_observed_instead_of_claimed_as_a_new_execution(tmp_path):
    state = make_state(tmp_path, "sqlite-server")
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    state.adapter.planner_enhancements_enabled = True
    state.adapter.initialize()
    status = state.history.check()
    assert not status["tracked"] and status["unrecorded"] == ["sqlite-planner-v1"]
    result = state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    assert (
        next(row for row in result["recorded"] if row["id"] == "sqlite-planner-v1")["kind"]
        == "observed"
    )


def test_concurrent_baseline_records_each_revision_once(tmp_path):
    state = make_state(tmp_path, "sqlite-server")
    plan = state.history.plan()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: SQLiteHistory(state.path, "sqlite-server").baseline(
                    expected_sha256=plan["plan_sha256"]
                ),
                range(2),
            )
        )
    assert sorted(len(result["changed"]) for result in results) == [0, 3]
    assert len(state.history.check()["recorded"]) == 3


def test_missing_file_wrong_profile_and_partial_feature_are_not_initialized(tmp_path):
    path = tmp_path / "missing.sqlite3"
    history = SQLiteHistory(path, "sqlite-server")
    with pytest.raises(HistoryRejected, match="existing SQLite"):
        history.check()
    assert not path.exists()
    state = make_state(tmp_path, "sqlite-server")
    with pytest.raises(HistoryRejected, match="profile does not match"):
        SQLiteHistory(state.path, "sqlite-foundation").check()
    with state.adapter.connect() as db:
        db.execute("CREATE TABLE web_task_drafts(user_id TEXT)")
    with pytest.raises(HistoryRejected, match="partial or incompatible"):
        state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])


def test_pinned_sources_match_existing_schema_builders():
    root = Path(__file__).parents[1] / "web_service"
    normalize = lambda text: text.replace("\r\n", "\n").strip() + "\n"
    for id, source in (
        ("sqlite-base-v1", (root / "schema.sql").read_text(encoding="utf-8")),
        ("postgres-base-v1", (root / "postgresql_schema_draft.sql").read_text(encoding="utf-8")),
        ("sqlite-school-v1", SCHOOL_SCHEMA),
        ("sqlite-server-v1", EXTRA_SCHEMA),
        ("sqlite-beta-v1", BETA_SQL),
        ("sqlite-planner-v1", ";\n".join(planner_statements()) + ";\n"),
    ):
        assert REVISIONS[id].source() == normalize(source)
    assert (
        schema_plan("sqlite-server")["plan_sha256"]
        != schema_plan("sqlite-server", "planner")["plan_sha256"]
    )


def test_default_cli_plan_is_offline_and_untracked_check_exits_two(tmp_path, monkeypatch, capsys):
    from web_service.schema_history_cli import main

    monkeypatch.setenv("DATABASE_URL", "private-invalid-test-value")
    monkeypatch.setattr("sys.argv", ["schema_history_cli", "--profile", "postgres-server"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "baseline" and result["automatic_migration"] is False
    assert "private-invalid-test-value" not in json.dumps(result)
    state = make_state(tmp_path, "sqlite-server")
    monkeypatch.setattr(
        "sys.argv",
        [
            "schema_history_cli",
            "--check",
            "--profile",
            "sqlite-server",
            "--database",
            str(state.path),
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["tracked"] is False


def test_published_script_changes_are_rejected_before_target_access(tmp_path, monkeypatch):
    import web_service.schema_catalog as catalog

    folder = tmp_path / "revisions"
    folder.mkdir()
    for revision in REVISIONS.values():
        (folder / revision.script).write_text(revision.source(), encoding="utf-8", newline="\n")
    monkeypatch.setattr(catalog, "_ROOT", folder)
    # CRLF copies represent the same pinned source on Windows and Linux.
    path = folder / "sqlite_base_v1.sql"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert schema_plan("sqlite-server")["plan_sha256"]
    path.write_bytes(path.read_bytes() + b"CREATE TABLE unexpected(id TEXT);\n")
    target = tmp_path / "must-not-be-created.sqlite3"
    with pytest.raises(HistoryRejected, match="checksum has changed"):
        SQLiteHistory(target, "sqlite-server")
    assert not target.exists()


def test_shared_revision_matches_the_existing_additive_installer():
    import ast
    import inspect

    from web_service.postgres_shared_schema import enable_shared_channel_schema

    tree = ast.parse(inspect.getsource(enable_shared_channel_schema))
    source = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("CREATE TABLE")
    )
    assert REVISIONS["postgres-shared-v1"].statements() == (source.strip(),)
