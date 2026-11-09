"""Reminder target access and writes share one real PostgreSQL transaction."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event

import pytest

from test_postgres_database_integration import CHANNELS, postgres_database, source
from web_service.planner_trash import TRASH_SCHEMA, TRASH_SECONDS
from web_service.storage import Conflict, Missing, PermissionPending


RULES = {"offsets": [-30], "custom_times": []}


def publish(p):
    event = source("Shared deadline")
    lease = p.repo.begin_sync(p.users["alice"], 0)
    p.repo.commit_snapshot(p.devices["alice"], CHANNELS, [event], 60, lease)
    return event


def personal(p, user="alice"):
    return p.repo.add_task(
        p.users[user],
        {
            "title": "Personal task",
            "notes": "",
            "time": None,
            "start_date": "2026-11-05",
            "end_date": "2026-11-06",
            "display_mode": "daily",
            "selected_dates": [],
        },
    )


def stored(p, item, kind):
    with p.connect() as db:
        row = db.execute(
            """SELECT payload_blob FROM web_reminder_rules
            WHERE user_id=%s AND item_id=%s AND source=%s""",
            (p.users["alice"], item, kind),
        ).fetchone()
    return p.repo.unseal(row[0]) if row else None


def test_reminder_writes_require_current_target_and_owner(postgres_database):
    p = postgres_database
    event, task = publish(p), personal(p)
    p.repo.save_rules(p.users["alice"], event["id"], "teams", RULES)
    p.repo.save_rules(p.users["alice"], task["id"], "personal", RULES)
    assert stored(p, event["id"], "teams") == stored(p, task["id"], "personal") == RULES
    for identity, kind in ((event["id"], "teams"), (task["id"], "personal")):
        with pytest.raises(Missing):
            p.repo.save_rules(p.users["bob"], identity, kind, RULES)
    with pytest.raises(Missing):
        p.repo.save_rules(p.users["alice"], event["id"], "other", RULES)


def test_task_deleted_after_api_lookup_cannot_get_an_orphan_rule(postgres_database):
    p = postgres_database
    task = personal(p)
    assert p.repo.task(p.users["alice"], task["id"])
    p.repo.delete_task(p.users["alice"], task["id"], task["version"])
    with pytest.raises(Missing):
        p.repo.save_rules(p.users["alice"], task["id"], "personal", RULES)
    assert stored(p, task["id"], "personal") is None


def test_permission_revoked_after_api_lookup_cannot_get_a_rule(postgres_database):
    p = postgres_database
    event = publish(p)
    assert p.repo._shared(p.users["alice"], "tenant")
    p.repo.revoke_device(p.devices["alice"]["id_hash"])
    with pytest.raises(Missing):
        p.repo.save_rules(p.users["alice"], event["id"], "teams", RULES)
    assert stored(p, event["id"], "teams") is None


def test_expired_proof_preserves_the_previous_rule(postgres_database):
    p = postgres_database
    event = publish(p)
    p.repo.save_rules(p.users["alice"], event["id"], "teams", RULES)
    p.now[0] += 61
    with pytest.raises(PermissionPending):
        p.repo.save_rules(
            p.users["alice"], event["id"], "teams", {"offsets": [], "custom_times": []}
        )
    assert stored(p, event["id"], "teams") == RULES


def test_proof_expiring_while_waiting_for_event_lock_saves_nothing(postgres_database, monkeypatch):
    p = postgres_database
    event = publish(p)
    waiting = Event()
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:

            class Connection:
                def execute(self, query, values=()):
                    if query.startswith("SELECT channel_id FROM web_events WHERE id="):
                        waiting.set()
                    return db.execute(query, values)

            yield Connection()

    monkeypatch.setattr(p.repo, "connect", intercepted)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with p.connect() as held:
            held.execute("SELECT id FROM web_events WHERE id=%s FOR UPDATE", (event["id"],))
            result = pool.submit(p.repo.save_rules, p.users["alice"], event["id"], "teams", RULES)
            assert waiting.wait(2), "The writer must reach the event lock"
            p.now[0] += 61
        with pytest.raises(PermissionPending):
            result.result(timeout=5)
    assert stored(p, event["id"], "teams") is None


def test_expiry_after_rule_write_rolls_back_the_transaction(postgres_database, monkeypatch):
    p = postgres_database
    event = publish(p)
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:

            class Connection:
                def execute(self, query, values=()):
                    result = db.execute(query, values)
                    if query.startswith("INSERT INTO web_reminder_rules"):
                        p.now[0] += 61
                    return result

            yield Connection()

    monkeypatch.setattr(p.repo, "connect", intercepted)
    with pytest.raises(PermissionPending):
        p.repo.save_rules(p.users["alice"], event["id"], "teams", RULES)
    assert stored(p, event["id"], "teams") is None


def enable_trash(p):
    with p.connect() as db:
        for statement in TRASH_SCHEMA:
            db.execute(statement)
    p.repo.planner_enhancements_enabled = True


def test_personal_trash_roundtrip_preserves_rules_and_sent_deliveries(postgres_database):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    task = p.repo.task_state(
        p.users["alice"], task["id"], {"hidden": True, "completed": True}, task["version"]
    )
    p.repo.save_rules(p.users["alice"], task["id"], "personal", RULES)
    with p.connect() as db:
        db.execute(
            """INSERT INTO web_push_subscriptions
            (id,device_hash,endpoint_hash,subscription_blob,created_at)
            VALUES(%s,%s,%s,%s,%s)""",
            (
                "fixture-subscription",
                p.devices["alice"]["id_hash"],
                "fixture-endpoint-hash",
                p.repo.seal({"endpoint": "https://push.example.test/fixture"}),
                p.now[0],
            ),
        )
        db.execute(
            """INSERT INTO web_deliveries(id,subscription_id,state,created_at)
            VALUES(%s,%s,'sent',%s)""",
            ("already-sent", "fixture-subscription", p.now[0]),
        )
        delivery = db.execute("SELECT * FROM web_deliveries").fetchone()
    archived = p.repo.trash_task(p.users["alice"], task["id"], task["version"])
    assert archived["expires_at"] - archived["deleted_at"] == TRASH_SECONDS
    with pytest.raises(Missing):
        p.repo.task(p.users["alice"], task["id"])
    assert stored(p, task["id"], "personal") is None
    assert p.repo.list_task_trash(p.users["bob"], "tenant")["items"] == []
    restored = p.repo.restore_task_trash(p.users["alice"], "tenant", archived["id"])
    assert restored == {**task, "version": task["version"] + 1}
    assert stored(p, task["id"], "personal") == RULES
    with p.connect() as db:
        assert db.execute("SELECT * FROM web_deliveries").fetchone() == delivery
        assert db.execute("SELECT count(*) FROM web_task_trash").fetchone()[0] == 0
    assert p.repo.claim_delivery_lease("already-sent", "fixture-subscription") is None
    with pytest.raises(Conflict):
        p.repo.update_task(
            p.users["alice"], task["id"], {**task, "title": "Stale"}, task["version"]
        )
    with pytest.raises(Conflict):
        p.repo.delete_task(p.users["alice"], task["id"], task["version"])


def test_trash_and_restore_each_have_one_winner_under_concurrent_requests(postgres_database):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    gate = Barrier(2)

    def archive(_):
        gate.wait(timeout=3)
        try:
            return p.repo.trash_task(p.users["alice"], task["id"], task["version"])
        except Missing:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(archive, range(2)))
    winners = [value for value in results if value]
    assert len(winners) == 1
    gate = Barrier(2)

    def restore(_):
        gate.wait(timeout=3)
        try:
            return p.repo.restore_task_trash(p.users["alice"], "tenant", winners[0]["id"])
        except Missing:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovered = [value for value in pool.map(restore, range(2)) if value]
    assert len(recovered) == 1 and recovered[0]["version"] == task["version"] + 1


@pytest.mark.parametrize("rule", [None, {"offsets": [], "custom_times": []}])
def test_trash_restores_absent_and_disabled_rules_without_changing_default_policy(
    postgres_database, rule
):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    if rule is not None:
        p.repo.save_rules(p.users["alice"], task["id"], "personal", rule)
    archived = p.repo.trash_task(p.users["alice"], task["id"], task["version"])
    p.repo.restore_task_trash(p.users["alice"], "tenant", archived["id"])
    assert stored(p, task["id"], "personal") == rule
    assert p.repo.rules(p.users["alice"], task["id"], "personal") == (
        rule if rule is not None else {"offsets": [-1440, -60], "custom_times": []}
    )


def test_trash_restore_does_not_overwrite_existing_identity(postgres_database):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    archived = p.repo.trash_task(p.users["alice"], task["id"], task["version"])
    with p.connect() as db:
        db.execute(
            """INSERT INTO web_personal_tasks(id,user_id,start_date,end_date,payload_blob)
            VALUES(%s,%s,%s,%s,%s)""",
            (
                task["id"],
                p.users["bob"],
                task["start_date"],
                task["end_date"],
                p.repo.seal(
                    {
                        "title": "Bob fixture task",
                        "start_date": task["start_date"],
                        "end_date": task["end_date"],
                        "display_mode": "daily",
                        "selected_dates": [],
                    }
                ),
            ),
        )
    with pytest.raises(Conflict):
        p.repo.restore_task_trash(p.users["alice"], "tenant", archived["id"])
    assert p.repo.task(p.users["bob"], task["id"])["title"] == "Bob fixture task"
    assert len(p.repo.list_task_trash(p.users["alice"], "tenant")["items"]) == 1


def test_trash_rule_cleanup_failure_rolls_back_original_and_archive(postgres_database, monkeypatch):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    p.repo.save_rules(p.users["alice"], task["id"], "personal", RULES)
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:

            class Connection:
                def execute(self, query, values=()):
                    if "DELETE FROM web_reminder_rules" in query:
                        raise RuntimeError("Fixture cleanup failure")
                    return db.execute(query, values)

            yield Connection()

    monkeypatch.setattr(p.repo, "connect", intercepted)
    with pytest.raises(RuntimeError, match="Fixture cleanup failure"):
        p.repo.trash_task(p.users["alice"], task["id"], task["version"])
    assert p.repo.task(p.users["alice"], task["id"]) == task
    assert stored(p, task["id"], "personal") == RULES
    with p.connect() as db:
        assert db.execute("SELECT count(*) FROM web_task_trash").fetchone()[0] == 0


def test_trash_rule_restore_failure_is_atomic(postgres_database, monkeypatch):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    p.repo.save_rules(p.users["alice"], task["id"], "personal", RULES)
    archived = p.repo.trash_task(p.users["alice"], task["id"], task["version"])
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:

            class Connection:
                def execute(self, query, values=()):
                    if "INSERT INTO web_reminder_rules" in query:
                        raise RuntimeError("Fixture restore failure")
                    return db.execute(query, values)

            yield Connection()

    monkeypatch.setattr(p.repo, "connect", intercepted)
    with pytest.raises(RuntimeError, match="Fixture restore failure"):
        p.repo.restore_task_trash(p.users["alice"], "tenant", archived["id"])
    with pytest.raises(Missing):
        p.repo.task(p.users["alice"], task["id"])
    assert stored(p, task["id"], "personal") is None
    with p.connect() as db:
        assert db.execute("SELECT count(*) FROM web_task_trash").fetchone()[0] == 1


def test_trash_expiry_while_waiting_for_archive_lock_prevents_restore(
    postgres_database, monkeypatch
):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    archived = p.repo.trash_task(p.users["alice"], task["id"], task["version"])
    waiting = Event()
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:

            class Connection:
                def execute(self, query, values=()):
                    if "SELECT task_id,payload_blob,expires_at" in query:
                        waiting.set()
                    return db.execute(query, values)

            yield Connection()

    monkeypatch.setattr(p.repo, "connect", intercepted)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with p.connect() as held:
            held.execute("SELECT id FROM web_task_trash WHERE id=%s FOR UPDATE", (archived["id"],))
            result = pool.submit(
                p.repo.restore_task_trash, p.users["alice"], "tenant", archived["id"]
            )
            assert waiting.wait(2), "The restore must reach the archive lock"
            p.now[0] = archived["expires_at"]
        with pytest.raises(Missing):
            result.result(timeout=5)
    with pytest.raises(Missing):
        p.repo.task(p.users["alice"], task["id"])
    assert p.repo.prune_task_trash() == 1


def test_trash_expiry_after_rule_write_rolls_back_native_restore(postgres_database, monkeypatch):
    p = postgres_database
    enable_trash(p)
    task = personal(p)
    p.repo.save_rules(p.users["alice"], task["id"], "personal", RULES)
    archived = p.repo.trash_task(p.users["alice"], task["id"], task["version"])
    p.now[0] = archived["expires_at"] - 1
    original = p.repo.connect

    @contextmanager
    def intercepted():
        with original() as db:

            class Connection:
                def execute(self, query, values=()):
                    value = db.execute(query, values)
                    if "INSERT INTO web_reminder_rules" in query:
                        p.now[0] = archived["expires_at"]
                    return value

            yield Connection()

    monkeypatch.setattr(p.repo, "connect", intercepted)
    with pytest.raises(Missing):
        p.repo.restore_task_trash(p.users["alice"], "tenant", archived["id"])
    with pytest.raises(Missing):
        p.repo.task(p.users["alice"], task["id"])
    assert stored(p, task["id"], "personal") is None
    with p.connect() as db:
        assert db.execute("SELECT count(*) FROM web_task_trash").fetchone()[0] == 1
