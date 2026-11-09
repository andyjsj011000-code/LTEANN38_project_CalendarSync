"""Opt-in integration tests: use only a dedicated disposable PostgreSQL DB."""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from web_service.planner_drafts import DRAFT_RETENTION_SECONDS, DRAFT_SCHEMA_SQL, PostgresTaskDrafts
from web_service.postgres_personal_tasks import PostgresPersonalTasks
from web_service.storage import Conflict, Missing


@pytest.fixture
def postgres_tasks():
    dsn = os.environ.get("CALENDARSYNC_DISPOSABLE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("No dedicated disposable PostgreSQL database configured")
    if os.environ.get("CALENDARSYNC_ALLOW_TEMP_SCHEMA") != "yes":
        pytest.skip("Disposable database and temporary schema creation must be explicitly enabled")
    psycopg = pytest.importorskip("psycopg")
    schema = "calendarsync_test_" + uuid.uuid4().hex
    ddl = (Path(__file__).parents[1] / "web_service" / "postgresql_schema_draft.sql").read_text(
        encoding="utf-8"
    )
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("CREATE SCHEMA " + schema)

    @contextmanager
    def connect():
        with psycopg.connect(dsn) as db:
            db.execute("SET LOCAL search_path TO " + schema)
            yield db

    try:
        with connect() as db:
            # Strip line comments before splitting: comments may contain ';'.
            statements = "\n".join(
                line for line in ddl.splitlines() if not line.lstrip().startswith("--")
            )
            for statement in statements.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(
                "INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES('alice','t','a','x'),('bob','t','b','x')"
            )
        cipher = Fernet(Fernet.generate_key())

        def seal(payload):
            return cipher.encrypt(json.dumps(payload).encode()).decode()

        def unseal(blob):
            return json.loads(cipher.decrypt(blob.encode()))

        yield PostgresPersonalTasks(connect, seal, unseal), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("DROP SCHEMA " + schema + " CASCADE")


def test_task_conflict_and_access_control(postgres_tasks):
    repo, connect = postgres_tasks
    payload = {"title": "private content", "start_date": "2026-11-05", "end_date": "2026-11-06"}
    item = repo.add_task("alice", payload)
    with connect() as db:
        blob = db.execute(
            "SELECT payload_blob FROM web_personal_tasks WHERE id=%s", (item["id"],)
        ).fetchone()[0]
    assert "private content" not in blob
    with pytest.raises(Missing):
        repo.task("bob", item["id"])

    def update(_):
        try:
            return repo.task_state("alice", item["id"], {"completed": True}, item["version"])
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, range(2)))
    assert sum(result is not None for result in results) == 1
    assert repo.task("alice", item["id"])["version"] == item["version"] + 1


def test_private_task_cannot_be_modified_by_another_user(postgres_tasks):
    repo, _ = postgres_tasks
    payload = {"title": "private content", "start_date": "2026-11-05", "end_date": "2026-11-06"}
    item = repo.add_task("alice", payload)
    with pytest.raises(Missing):
        repo.update_task("bob", item["id"], payload, item["version"])
    with pytest.raises(Missing):
        repo.delete_task("bob", item["id"], item["version"])
    with pytest.raises(Missing):
        repo.task_state("bob", item["id"], {"completed": True}, item["version"])
    assert repo.task("alice", item["id"])["version"] == item["version"]


def test_update_and_delete_obey_latest_version(postgres_tasks):
    repo, _ = postgres_tasks
    payload = {"title": "initial", "start_date": "2026-11-05", "end_date": "2026-11-06"}
    item = repo.add_task("alice", payload)
    changed = repo.update_task(
        "alice", item["id"], {**payload, "title": "changed"}, item["version"]
    )
    assert changed["title"] == "changed"
    assert changed["version"] == item["version"] + 1
    with pytest.raises(Conflict):
        repo.delete_task("alice", item["id"], item["version"])
    repo.delete_task("alice", item["id"], changed["version"])
    with pytest.raises(Missing):
        repo.task("alice", item["id"])


def test_delete_removes_only_owned_personal_rules_after_version_check(postgres_tasks):
    repo, connect = postgres_tasks
    payload = {"title": "task", "start_date": "2026-11-05", "end_date": "2026-11-06"}
    item = repo.add_task("alice", payload)
    rules = {
        ("alice", item["id"], "personal"),
        ("alice", item["id"], "teams"),
        ("bob", item["id"], "personal"),
        ("alice", "other-task", "personal"),
    }
    with connect() as db:
        for key in rules:
            db.execute(
                "INSERT INTO web_reminder_rules VALUES(%s,%s,%s,%s)",
                (*key, repo.seal({"offsets": [-30], "custom_times": []})),
            )
    changed = repo.task_state("alice", item["id"], {"completed": True}, item["version"])
    with pytest.raises(Missing):
        repo.delete_task("bob", item["id"], changed["version"])
    with pytest.raises(Conflict):
        repo.delete_task("alice", item["id"], item["version"])
    with connect() as db:
        assert set(db.execute("SELECT user_id,item_id,source FROM web_reminder_rules")) == rules
    repo.delete_task("alice", item["id"], changed["version"])
    with connect() as db:
        assert set(db.execute("SELECT user_id,item_id,source FROM web_reminder_rules")) == (
            rules - {("alice", item["id"], "personal")}
        )
    with pytest.raises(Missing):
        repo.task("alice", item["id"])


def test_reminder_cleanup_failure_rolls_back_task_deletion(postgres_tasks, monkeypatch):
    repo, connect = postgres_tasks
    item = repo.add_task(
        "alice", {"title": "task", "start_date": "2026-11-05", "end_date": "2026-11-06"}
    )
    with connect() as db:
        db.execute(
            "INSERT INTO web_reminder_rules VALUES(%s,%s,%s,%s)",
            ("alice", item["id"], "personal", repo.seal({"offsets": [-30], "custom_times": []})),
        )

    @contextmanager
    def failing_connect():
        with connect() as db:

            class Connection:
                def execute(self, query, values=()):
                    if query.startswith("DELETE FROM web_reminder_rules"):
                        raise RuntimeError("Simulated reminder cleanup failure")
                    return db.execute(query, values)

            yield Connection()

    monkeypatch.setattr(repo, "connect", failing_connect)
    with pytest.raises(RuntimeError, match="Simulated reminder cleanup failure"):
        repo.delete_task("alice", item["id"], item["version"])
    assert repo.task("alice", item["id"])["version"] == item["version"]
    with connect() as db:
        assert db.execute(
            "SELECT 1 FROM web_reminder_rules WHERE user_id=%s AND item_id=%s",
            ("alice", item["id"]),
        ).fetchone() == (1,)


def test_personal_agenda_obeys_owner_tenant_visibility_dates_and_order(postgres_tasks):
    repo, connect = postgres_tasks
    base = {"notes": "", "start_date": "2026-11-05", "end_date": "2026-11-07", "selected_dates": []}
    daily = repo.add_task(
        "alice", {**base, "title": "Daily", "time": "10:00", "display_mode": "daily"}
    )
    selected = repo.add_task(
        "alice",
        {
            **base,
            "title": "Selected",
            "time": "09:00",
            "display_mode": "selected",
            "selected_dates": ["2026-11-05"],
        },
    )
    final = repo.add_task(
        "alice", {**base, "title": "Final", "time": "08:00", "display_mode": "final"}
    )
    hidden = repo.add_task(
        "alice", {**base, "title": "Hidden", "time": None, "display_mode": "daily"}
    )
    repo.task_state("alice", daily["id"], {"completed": True}, daily["version"])
    repo.task_state("alice", hidden["id"], {"hidden": True}, hidden["version"])
    repo.add_task("bob", {**base, "title": "Another user", "time": None, "display_mode": "daily"})

    first = repo.personal_agenda("alice", "t", "2026-11-05")
    assert [item["id"] for item in first["personal"]] == [selected["id"], daily["id"]]
    assert first["personal"][1]["completed"] is True
    assert [
        item["id"] for item in repo.personal_agenda("alice", "t", "2026-11-06")["personal"]
    ] == [daily["id"]]
    assert [
        item["id"] for item in repo.personal_agenda("alice", "t", "2026-11-07")["personal"]
    ] == [final["id"], selected["id"], daily["id"]]
    assert repo.personal_agenda("alice", "wrong-tenant", "2026-11-05")["personal"] == []
    assert repo.personal_agenda("missing-user", "t", "2026-11-05")["personal"] == []
    with connect() as db:
        db.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    assert repo.personal_agenda("alice", "t", "2026-11-05")["personal"] == []


@pytest.fixture
def postgres_drafts(postgres_tasks):
    tasks, connect = postgres_tasks
    with connect() as db:
        for statement in DRAFT_SCHEMA_SQL.split(";"):
            if statement.strip():
                db.execute(statement)

    class Repository(PostgresTaskDrafts, PostgresPersonalTasks):
        pass

    repo = Repository(connect, tasks.seal, tasks.unseal)
    now = [2000000000]
    repo.clock = lambda: now[0]
    return repo, connect, now


def draft_payload(title="Personal draft"):
    return {
        "task_id": None,
        "base_version": None,
        "fields": {
            "title": title,
            "notes": "Private draft notes",
            "start_date": "2026-11-07",
            "end_date": "",
            "time": "",
            "display_mode": "daily",
            "selected_dates": [],
        },
    }


def test_draft_roundtrip_is_encrypted_private_and_personal_only(postgres_drafts):
    repo, connect, now = postgres_drafts
    saved = repo.save_task_draft("alice", "t", draft_payload(), 0)
    assert saved == {
        "draft": draft_payload(),
        "version": 1,
        "updated_at": now[0],
        "expires_at": now[0] + DRAFT_RETENTION_SECONDS,
    }
    assert repo.get_task_draft("alice", "t") == saved
    assert repo.get_task_draft("bob", "t")["draft"] is None
    with connect() as db:
        blob = db.execute(
            "SELECT payload_blob FROM web_task_drafts WHERE user_id='alice'"
        ).fetchone()[0]
    assert "Personal draft" not in blob and "Private draft notes" not in blob
    task = repo.add_task(
        "bob", {"title": "Bob task", "start_date": "2026-11-07", "end_date": "2026-11-07"}
    )
    with pytest.raises(Missing):
        repo.save_task_draft(
            "alice", "t", {**draft_payload(), "task_id": task["id"], "base_version": 1}, 1
        )
    assert repo.get_task_draft("alice", "t") == saved


def test_parallel_draft_creation_has_one_winner_and_clear_fences_late_requests(postgres_drafts):
    repo, _, _ = postgres_drafts

    def save(index):
        try:
            return repo.save_task_draft("alice", "t", draft_payload(str(index)), 0)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(4)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1 and repo.get_task_draft("alice", "t") == winners[0]
    cleared = repo.clear_task_draft("alice", "t", 1)
    assert cleared["draft"] is None and cleared["version"] == 2
    for old in (0, 1):
        with pytest.raises(Conflict):
            repo.save_task_draft("alice", "t", draft_payload("late"), old)
    assert repo.get_task_draft("alice", "t") == cleared


def test_draft_expiration_race_drops_content_without_revision_aba(postgres_drafts):
    repo, connect, now = postgres_drafts
    saved = repo.save_task_draft("alice", "t", draft_payload(), 0)
    now[0] = saved["expires_at"]

    def late_save():
        try:
            repo.save_task_draft("alice", "t", draft_payload("late"), 1)
            return "unexpected-save"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(late_save)
        read = pool.submit(repo.get_task_draft, "alice", "t")
        assert old.result(timeout=10) == "conflict"
        expired = read.result(timeout=10)
    assert expired["draft"] is None and expired["version"] == 2
    now[0] += 10 * DRAFT_RETENTION_SECONDS
    assert repo.prune_task_drafts() == 0
    with connect() as db:
        assert db.execute(
            "SELECT payload_blob,version FROM web_task_drafts WHERE user_id='alice'"
        ).fetchone() == (None, 2)
    with pytest.raises(Conflict):
        repo.save_task_draft("alice", "t", draft_payload(), 0)
    assert repo.save_task_draft("alice", "t", draft_payload("fresh"), 2)["version"] == 3


@pytest.mark.parametrize("invalid", ["tenant", "inactive", "missing"])
def test_draft_reads_writes_and_clears_recheck_active_tenant_owner(postgres_drafts, invalid):
    repo, connect, _ = postgres_drafts
    repo.save_task_draft("alice", "t", draft_payload(), 0)
    user, tenant = "alice", "t"
    if invalid == "tenant":
        tenant = "foreign-school"
    elif invalid == "inactive":
        with connect() as db:
            db.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    else:
        user = "missing-user"
    for operation in (
        lambda: repo.get_task_draft(user, tenant),
        lambda: repo.save_task_draft(user, tenant, draft_payload(), 1),
        lambda: repo.clear_task_draft(user, tenant, 1),
    ):
        with pytest.raises(Missing):
            operation()
    with connect() as db:
        assert db.execute(
            "SELECT version FROM web_task_drafts WHERE user_id='alice'"
        ).fetchone() == (1,)
