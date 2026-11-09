"""Personal-task recovery contracts, with no external notification sends."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.planner_trash import (
    TRASH_SCHEMA,
    TRASH_SECONDS,
    PostgresTaskTrash,
    SQLiteTaskTrash,
)
from web_service.push import Notifications
from web_service.storage import Conflict, Missing, digest
from web_service.test_storage import TestDatabase

PAYLOAD = {
    "title": "Fixture personal deadline",
    "notes": "Private fixture notes",
    "start_date": "2026-11-08",
    "end_date": "2026-11-09",
    "time": "10:00",
    "display_mode": "selected",
    "selected_dates": ["2026-11-09"],
}
RULE = {"offsets": [-30], "custom_times": ["2026-11-09T00:30:00+00:00"]}


@pytest.fixture(params=["sqlite", "postgres_sql_shim"])
def trash_store(tmp_path, request):
    now = [1000]
    db = TestDatabase(
        tmp_path / "trash.sqlite3", Fernet.generate_key().decode(), clock=lambda: now[0]
    )
    db.initialize()
    alice = db.save_user("school", "alice", {"name": "Fixture Alice"})
    bob = db.save_user("school", "bob", {"name": "Fixture Bob"})
    with db.connect() as connection:
        for statement in TRASH_SCHEMA:
            connection.execute(statement)

    class Shim:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, values=()):
            return self.connection.execute(
                query.replace(" FOR UPDATE", "").replace("%s", "?"), values
            )

    @contextmanager
    def connect():
        with db.connect() as connection:
            # PostgreSQL itself starts a transaction before its row locks.
            connection.execute("BEGIN IMMEDIATE")
            yield Shim(connection)

    class Store(PostgresTaskTrash if request.param == "postgres_sql_shim" else SQLiteTaskTrash):
        pass

    store = Store()
    store.connect = connect if request.param == "postgres_sql_shim" else db.connect
    store.seal, store.unseal, store.clock = db.seal, db.unseal, db.clock
    store.planner_enhancements_enabled = True
    item = db.add_task(alice, PAYLOAD)
    return SimpleNamespace(store=store, db=db, now=now, alice=alice, bob=bob, item=item)


def save_rule(p, user=None, item=None, rule=RULE, source="personal"):
    with p.db.connect() as connection:
        connection.execute(
            "INSERT INTO web_reminder_rules VALUES(?,?,?,?)",
            (user or p.alice, item or p.item["id"], source, p.db.seal(rule)),
        )


def rule(p, user=None, item=None, source="personal"):
    with p.db.connect() as connection:
        row = connection.execute(
            """SELECT payload_blob FROM web_reminder_rules
            WHERE user_id=? AND item_id=? AND source=?""",
            (user or p.alice, item or p.item["id"], source),
        ).fetchone()
    return p.db.unseal(row[0]) if row else None


def archives(p):
    with p.db.connect() as connection:
        return [
            tuple(row) for row in connection.execute("SELECT * FROM web_task_trash ORDER BY id")
        ]


def test_delete_archives_only_the_owned_task_and_explicit_personal_rule(trash_store):
    p = trash_store
    other = p.db.add_task(p.bob, PAYLOAD)
    save_rule(p)
    save_rule(p, p.bob, other["id"])
    save_rule(p, source="teams")
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    assert archived["task_id"] == p.item["id"] and archived["id"] != p.item["id"]
    assert archived["expires_at"] - archived["deleted_at"] == TRASH_SECONDS
    with pytest.raises(Missing):
        p.db.task(p.alice, p.item["id"])
    assert rule(p) is None
    assert rule(p, p.bob, other["id"]) == RULE
    assert rule(p, source="teams") == RULE
    values = p.store.list_task_trash(p.alice, "school")
    assert values["retention_days"] == 7
    assert len(values["items"]) == 1
    assert values["items"][0]["title"] == PAYLOAD["title"]
    assert "notes" not in values["items"][0] and "reminder_rule" not in values["items"][0]
    assert p.store.list_task_trash(p.bob, "school")["items"] == []
    blob = archives(p)[0][3]
    assert PAYLOAD["title"] not in blob and PAYLOAD["notes"] not in blob


def test_restore_preserves_data_state_and_blocks_stale_versions(trash_store):
    p = trash_store
    save_rule(p)
    changed = p.db.task_state(
        p.alice, p.item["id"], {"completed": True, "hidden": True}, p.item["version"]
    )
    archived = p.store.trash_task(p.alice, p.item["id"], changed["version"])
    restored = p.store.restore_task_trash(p.alice, "school", archived["id"])
    assert restored["id"] == p.item["id"]
    assert restored["version"] == changed["version"] + 1
    assert restored["hidden"] and restored["completed"]
    for field, value in PAYLOAD.items():
        assert restored[field] == value
    assert p.db.task(p.alice, p.item["id"]) == restored
    assert rule(p) == RULE
    assert archives(p) == []
    with pytest.raises(Conflict):
        p.db.update_task(
            p.alice, p.item["id"], {**PAYLOAD, "title": "Stale write"}, changed["version"]
        )
    with pytest.raises(Conflict):
        p.db.delete_task(p.alice, p.item["id"], changed["version"])
    with pytest.raises(Missing):
        p.store.restore_task_trash(p.alice, "school", archived["id"])


@pytest.mark.parametrize("stored_rule", [None, {"offsets": [], "custom_times": []}])
def test_absent_default_rule_and_explicit_disabled_rule_stay_distinct(trash_store, stored_rule):
    p = trash_store
    if stored_rule is not None:
        save_rule(p, rule=stored_rule)
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    p.store.restore_task_trash(p.alice, "school", archived["id"])
    assert rule(p) == stored_rule
    expected = {"offsets": [-1440, -60], "custom_times": []} if stored_rule is None else stored_rule
    assert p.db.rules(p.alice, p.item["id"], "personal") == expected


def test_owner_tenant_and_inactive_user_cannot_read_or_restore(trash_store):
    p = trash_store
    with pytest.raises(Missing):
        p.store.trash_task(p.bob, p.item["id"], p.item["version"])
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    with pytest.raises(Missing):
        p.store.restore_task_trash(p.bob, "school", archived["id"])
    with pytest.raises(Missing):
        p.store.restore_task_trash(p.alice, "another-school", archived["id"])
    with pytest.raises(Missing):
        p.store.list_task_trash(p.alice, "another-school")
    with p.db.connect() as connection:
        connection.execute("UPDATE web_users SET active=0 WHERE id=?", (p.alice,))
    for operation in (
        lambda: p.store.list_task_trash(p.alice, "school"),
        lambda: p.store.restore_task_trash(p.alice, "school", archived["id"]),
    ):
        with pytest.raises(Missing):
            operation()
    assert len(archives(p)) == 1


def test_stale_delete_and_duplicate_archive_do_not_remove_or_replace_data(trash_store):
    p = trash_store
    save_rule(p)
    changed = p.db.task_state(p.alice, p.item["id"], {"completed": True}, p.item["version"])
    with pytest.raises(Conflict):
        p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    assert archives(p) == [] and rule(p) == RULE
    archived = p.store.trash_task(p.alice, p.item["id"], changed["version"])
    before = archives(p)
    with pytest.raises(Missing):
        p.store.trash_task(p.alice, p.item["id"], changed["version"])
    assert archives(p) == before
    restored = p.store.restore_task_trash(p.alice, "school", archived["id"])
    again = p.store.trash_task(p.alice, p.item["id"], restored["version"])
    assert again["id"] != archived["id"]
    with pytest.raises(Missing):
        p.store.restore_task_trash(p.alice, "school", archived["id"])


@pytest.mark.parametrize(
    "age,allowed", [(TRASH_SECONDS - 1, True), (TRASH_SECONDS, False), (TRASH_SECONDS + 1, False)]
)
def test_expiry_boundary_is_exact(trash_store, age, allowed):
    p = trash_store
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    p.now[0] += age
    assert bool(p.store.list_task_trash(p.alice, "school")["items"]) is allowed
    if allowed:
        assert p.store.restore_task_trash(p.alice, "school", archived["id"])["id"] == p.item["id"]
    else:
        with pytest.raises(Missing):
            p.store.restore_task_trash(p.alice, "school", archived["id"])
        with pytest.raises(Missing):
            p.db.task(p.alice, p.item["id"])


@pytest.mark.parametrize("collision_owner", ["alice", "bob"])
def test_existing_identity_collision_cannot_overwrite_a_task(trash_store, collision_owner):
    p = trash_store
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    user = getattr(p, collision_owner)
    with p.db.connect() as connection:
        connection.execute(
            """INSERT INTO web_personal_tasks
            (id,user_id,start_date,end_date,payload_blob) VALUES(?,?,?,?,?)""",
            (
                p.item["id"],
                user,
                PAYLOAD["start_date"],
                PAYLOAD["end_date"],
                p.db.seal({**PAYLOAD, "title": "Existing task"}),
            ),
        )
    with pytest.raises(Conflict):
        p.store.restore_task_trash(p.alice, "school", archived["id"])
    assert p.db.task(user, p.item["id"])["title"] == "Existing task"
    assert len(archives(p)) == 1


def fail_query(p, needle, *, after=None):
    original = p.store.connect

    @contextmanager
    def connect():
        with original() as db:

            class Failing:
                def execute(self, query, values=()):
                    if needle in query:
                        if after is None:
                            raise RuntimeError("Fixture transaction failure")
                        result = db.execute(query, values)
                        after()
                        return result
                    return db.execute(query, values)

            yield Failing()

    p.store.connect = connect


def test_rule_cleanup_failure_rolls_back_archive_and_task_delete(trash_store):
    p = trash_store
    save_rule(p)
    fail_query(p, "DELETE FROM web_reminder_rules")
    with pytest.raises(RuntimeError, match="Fixture transaction failure"):
        p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    assert p.db.task(p.alice, p.item["id"]) == p.item
    assert rule(p) == RULE and archives(p) == []


def test_rule_restore_failure_rolls_back_task_insert_and_preserves_archive(trash_store):
    p = trash_store
    save_rule(p)
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    before = archives(p)
    fail_query(p, "INSERT INTO web_reminder_rules")
    with pytest.raises(RuntimeError, match="Fixture transaction failure"):
        p.store.restore_task_trash(p.alice, "school", archived["id"])
    with pytest.raises(Missing):
        p.db.task(p.alice, p.item["id"])
    assert rule(p) is None and archives(p) == before


def test_expiry_during_restore_rolls_back_task_and_rules(trash_store):
    p = trash_store
    save_rule(p)
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    p.now[0] = archived["expires_at"] - 1
    fail_query(
        p,
        "INSERT INTO web_reminder_rules",
        after=lambda: p.now.__setitem__(0, archived["expires_at"]),
    )
    with pytest.raises(Missing):
        p.store.restore_task_trash(p.alice, "school", archived["id"])
    with pytest.raises(Missing):
        p.db.task(p.alice, p.item["id"])
    assert rule(p) is None and len(archives(p)) == 1


def test_cleanup_removes_only_expired_archives_and_preserves_delivery_records(trash_store):
    p = trash_store
    session = p.db.issue_session(p.alice, 3600)
    raw, _ = p.db.add_connection(
        session, p.alice, "fixture-token", 3600, "fixture-cache", background=True
    )
    device = p.db.device(digest(raw))
    subscription = p.db.subscribe(device, {"endpoint": "https://push.example.test/fixture"})
    assert p.db.claim_delivery("already-sent", subscription)
    p.db.finish_delivery("already-sent", True)
    with p.db.connect() as connection:
        delivery = tuple(connection.execute("SELECT * FROM web_deliveries").fetchone())
    first = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    p.now[0] += 1
    another = p.db.add_task(p.alice, {**PAYLOAD, "title": "Another task"})
    second = p.store.trash_task(p.alice, another["id"], another["version"])
    p.now[0] = first["expires_at"]
    assert p.store.prune_task_trash() == 1
    assert p.store.list_task_trash(p.alice, "school")["items"][0]["id"] == second["id"]
    restored = p.store.restore_task_trash(p.alice, "school", second["id"])
    assert restored["title"] == "Another task"
    with p.db.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM web_deliveries").fetchone()) == delivery
    assert not p.db.claim_delivery("already-sent", subscription)


def test_archived_task_is_not_a_notification_candidate(trash_store):
    from datetime import datetime, timezone

    p = trash_store
    p.now[0] = int(datetime(2026, 11, 9, 0, 30, tzinfo=timezone.utc).timestamp())
    save_rule(p)
    p.db.save_preferences(
        p.alice,
        {
            "morning_enabled": False,
            "morning_time": "07:00",
            "disabled_kinds": [],
            "calendar_sync_enabled": False,
            "teams_chat_enabled": False,
        },
    )
    runtime = Notifications(p.db, SimpleNamespace(configured=False))
    assert runtime.due(p.alice, "school")
    archived = p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    assert runtime.due(p.alice, "school") == []
    p.store.restore_task_trash(p.alice, "school", archived["id"])
    assert runtime.due(p.alice, "school")


def test_deletion_after_delivery_claim_prevents_dispatch(trash_store, monkeypatch):
    from datetime import datetime, timezone

    p = trash_store
    p.now[0] = int(datetime(2026, 11, 9, 0, 30, tzinfo=timezone.utc).timestamp())
    save_rule(p)
    p.db.save_preferences(
        p.alice,
        {
            "morning_enabled": False,
            "morning_time": "07:00",
            "disabled_kinds": [],
            "calendar_sync_enabled": False,
            "teams_chat_enabled": False,
        },
    )
    session = p.db.issue_session(p.alice, 3600)
    raw, _ = p.db.add_connection(
        session, p.alice, "fixture-token", 3600, "fixture-cache", background=True
    )
    device = p.db.device(digest(raw))
    p.db.subscribe(device, {"endpoint": "https://push.example.test/fixture"})
    sent, deleted = [], []
    sender = SimpleNamespace(configured=True, send=lambda *values: sent.append(values) or "sent")
    claim = p.db.claim_delivery

    def claim_and_delete(*values):
        lease = claim(*values)
        if lease:
            deleted.append(p.store.trash_task(p.alice, p.item["id"], p.item["version"]))
        return lease

    monkeypatch.setattr(p.db, "claim_delivery", claim_and_delete)
    assert Notifications(p.db, sender).dispatch(device) == 0
    assert sent == [] and len(deleted) == 1
    assert len(archives(p)) == 1


def test_feature_is_opt_in_and_rejects_invalid_versions(trash_store):
    p = trash_store
    for value in (0, -1, True, 1.5, "1"):
        with pytest.raises(ValueError):
            p.store.trash_task(p.alice, p.item["id"], value)
    p.store.planner_enhancements_enabled = False
    with pytest.raises(Missing):
        p.store.trash_task(p.alice, p.item["id"], p.item["version"])
    with pytest.raises(Missing):
        p.store.list_task_trash(p.alice, "school")
