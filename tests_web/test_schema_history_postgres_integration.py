"""Native history/upgrade checks using only the existing disposable DB guard."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from test_postgres_bootstrap_integration import bootstrap_database as bootstrap_database

from web_service.postgres_bootstrap import apply_bootstrap
from web_service.postgres_database import PostgresDatabase
from web_service.postgres_shared_schema import enable_shared_channel_schema
from web_service.schema_catalog import PROFILES, HistoryRejected
from web_service.schema_history import PostgresHistory
from web_service.schema_inspection import validate_structure


@pytest.fixture
def state(bootstrap_database):
    factory = bootstrap_database.factory
    apply_bootstrap(factory)
    repo = PostgresDatabase(factory, Fernet.generate_key().decode(), "tenant")
    repo.initialize()
    user = repo.save_user("tenant", "student", {"name": "synthetic name"})
    task = repo.add_task(
        user, {"title": "keep private task", "start_date": "2026-11-09", "end_date": "2026-11-09"}
    )
    return SimpleNamespace(
        factory=factory,
        repo=repo,
        user=user,
        task=task,
        history=PostgresHistory(factory, "tenant", clock=lambda: 1000),
    )


def business_rows(state):
    with state.factory() as db:
        return (
            db.execute("SELECT * FROM web_users ORDER BY id").fetchall(),
            db.execute("SELECT * FROM web_personal_tasks ORDER BY id").fetchall(),
            db.execute("SELECT version FROM web_schema_version").fetchall(),
        )


def test_read_only_metadata_matches_pinned_postgres_profile(state):
    with state.factory() as db:
        db.execute("SET TRANSACTION READ ONLY")
        assert db.execute("SHOW transaction_read_only").fetchone() == ("on",)
        validate_structure(db, "postgres", PROFILES["postgres-server"], history=False)
    before = business_rows(state)
    assert not state.history.check()["tracked"]
    with state.factory() as db:
        assert db.execute("SELECT to_regclass('web_schema_history')").fetchone() == (None,)
    assert business_rows(state) == before


def test_observed_baseline_is_repeatable_and_existing_adapter_still_works(state):
    before = business_rows(state)
    first = state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    assert first["changed"] == ["postgres-base-v1"]
    assert first["recorded"][0]["kind"] == "observed"
    assert (
        state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])["changed"] == []
    )
    state.repo.initialize()
    assert state.repo.task(state.user, state.task["id"])["title"] == "keep private task"
    assert business_rows(state) == before


def test_wrong_tenant_cannot_adopt_or_change_history(state):
    before = business_rows(state)
    history = PostgresHistory(state.factory, "another-tenant")
    with pytest.raises(HistoryRejected, match="another school"):
        history.baseline(expected_sha256=history.plan()["plan_sha256"])
    assert state.history.check()["recorded"] == []
    assert business_rows(state) == before


@pytest.mark.parametrize(
    "feature, id", [("planner", "postgres-planner-v1"), ("shared-ingestion", "postgres-shared-v1")]
)
def test_feature_upgrade_records_execution_without_changing_existing_rows(state, feature, id):
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    before = business_rows(state)
    plan = state.history.plan(feature)
    result = state.history.upgrade(feature, expected_sha256=plan["plan_sha256"])
    assert result["changed"] == [id]
    assert next(row for row in result["recorded"] if row["id"] == id)["kind"] == "applied"
    assert state.history.upgrade(feature, expected_sha256=plan["plan_sha256"])["changed"] == []
    assert business_rows(state) == before
    state.repo.initialize()


@pytest.mark.parametrize("feature", ["planner", "shared-ingestion"])
def test_feature_ddl_and_history_roll_back_in_one_transaction(state, feature):
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    before = state.history.check()
    rows = business_rows(state)

    class Failure(PostgresHistory):
        def _record(self, db, id, kind):
            super()._record(db, id, kind)
            if kind == "applied":
                raise RuntimeError("test-only failure after feature DDL and history insert")

    history = Failure(state.factory, "tenant")
    with pytest.raises(RuntimeError, match="update failed"):
        history.upgrade(feature, expected_sha256=history.plan(feature)["plan_sha256"])
    assert state.history.check() == before
    assert business_rows(state) == rows


@pytest.mark.parametrize("mutation", ["checksum", "unknown", "version"])
def test_modified_history_or_future_version_is_never_repaired_implicitly(state, mutation):
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    with state.factory() as db:
        if mutation == "checksum":
            db.execute("UPDATE web_schema_history SET checksum=%s", ("0" * 64,))
        elif mutation == "unknown":
            db.execute(
                "INSERT INTO web_schema_history VALUES('future',%s,'postgres-server','applied',1001)",
                ("0" * 64,),
            )
        else:
            db.execute("UPDATE web_schema_version SET version=2")
        before = db.execute("SELECT * FROM web_schema_history ORDER BY revision").fetchall()
    with pytest.raises(HistoryRejected):
        state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    with state.factory() as db:
        assert db.execute("SELECT * FROM web_schema_history ORDER BY revision").fetchall() == before


def test_parallel_baseline_and_upgrade_do_not_duplicate_revisions(state):
    baseline = state.history.plan()["plan_sha256"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: PostgresHistory(state.factory, "tenant").baseline(
                    expected_sha256=baseline
                ),
                range(2),
            )
        )
    assert sorted(len(result["changed"]) for result in results) == [0, 1]
    upgrade = state.history.plan("planner")["plan_sha256"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: PostgresHistory(state.factory, "tenant").upgrade(
                    "planner", expected_sha256=upgrade
                ),
                range(2),
            )
        )
    assert sorted(len(result["changed"]) for result in results) == [0, 1]
    assert len(state.history.check()["recorded"]) == 2


def test_old_additive_installer_remains_usable_and_is_recorded_as_observed(state):
    state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    enable_shared_channel_schema(state.factory, "tenant")
    status = state.history.check()
    assert not status["tracked"] and status["unrecorded"] == ["postgres-shared-v1"]
    result = state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
    assert (
        next(row for row in result["recorded"] if row["id"] == "postgres-shared-v1")["kind"]
        == "observed"
    )


def test_shared_ingestion_for_another_tenant_cannot_be_adopted(state):
    enable_shared_channel_schema(state.factory, "tenant")
    with state.factory() as db:
        db.execute(
            "INSERT INTO web_channel_ingestion(channel_id,tenant_id,team_id) VALUES('test-channel','another-tenant','team')"
        )
    with pytest.raises(HistoryRejected, match="another school"):
        state.history.baseline(expected_sha256=state.history.plan()["plan_sha256"])
