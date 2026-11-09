"""Logic tests with a SQLite-backed SQL shim; not PostgreSQL validation."""

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from web_service.notification_queue import UnknownSubscription
from web_service.postgres_notification_queue import PostgresNotificationQueue


@pytest.fixture
def queue(tmp_path):
    path = tmp_path / "test.sqlite3"
    schema = (Path(__file__).parents[1] / "web_service" / "postgresql_schema_draft.sql").read_text(
        encoding="utf-8"
    )
    schema = schema.split(
        "-- The notification worker and registry are also experimental and not wired.", 1
    )[1]
    with sqlite3.connect(path) as db:
        db.executescript(schema)

    class SQLShim:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, params=()):
            sql = sql.replace("%s", "?")
            sql = re.sub(r" FOR UPDATE(?: OF j SKIP LOCKED)?", "", sql)
            return self.db.execute(sql, params)

    @contextmanager
    def connect():
        with sqlite3.connect(path) as db:
            yield SQLShim(db)

    return PostgresNotificationQueue(connect, clock=lambda: 1000)


def test_wrong_state_cannot_enqueue_and_new_revision_is_not_lost(queue):
    queue.register("sub", "secret", "tenant", "team", "channel")
    assert queue.accept([{"subscriptionId": "sub", "clientState": "wrong"}]) == 0
    assert queue.claim() is None
    item = {"subscriptionId": "sub", "clientState": "secret"}
    assert queue.accept([item]) == 1
    first = queue.claim()
    queue.accept([item])
    queue.finish(first, True)
    second = queue.claim()
    assert second["revision"] == 2
    queue.finish(second, True)
    assert queue.claim() is None


def test_unknown_subscription_rolls_back_batch(queue):
    queue.register("known", "secret", "tenant", "team", "channel")
    with pytest.raises(UnknownSubscription):
        queue.accept(
            [
                {"subscriptionId": "known", "clientState": "secret"},
                {"subscriptionId": "unknown", "clientState": "secret"},
            ],
            defer_unknown=True,
        )
    assert queue.claim() is None


def test_removed_subscription_stops_work(queue):
    queue.register("sub", "secret", "tenant", "team", "channel")
    item = {"subscriptionId": "sub", "clientState": "secret"}
    queue.accept([item])
    assert queue.accept_lifecycle([{**item, "lifecycleEvent": "subscriptionRemoved"}]) == 1
    assert queue.claim() is None


def test_batch_order_keeps_each_subscriptions_lifecycle_sequence():
    item = lambda sub, event: {
        "subscriptionId": sub,
        "clientState": "secret",
        "lifecycleEvent": event,
    }
    removed = item("b", "subscriptionRemoved")
    reauthorize = item("b", "reauthorizationRequired")
    before = item("a", "reauthorizationRequired")
    batch = [removed, None, before, reauthorize]
    assert PostgresNotificationQueue._ordered(batch, lifecycle=True) == [
        before,
        removed,
        reauthorize,
    ]


def targeted_item(message="older-message"):
    return {
        "subscriptionId": "sub",
        "clientState": "secret",
        "resource": "teams/team/channels/channel/messages/" + message,
    }


def test_target_revision_survives_another_change_during_processing(queue):
    queue.track_targets = True
    queue.register("sub", "secret", "tenant", "team", "channel")
    queue.accept([targeted_item()])
    first = queue.claim()
    assert first["selected_messages"] == ["older-message"]
    assert not first["requires_reconciliation"]
    queue.accept([targeted_item()])
    queue.finish(first, True)
    second = queue.claim()
    assert second["selected_messages"] == ["older-message"]
    assert second["revision"] == 2
    queue.finish(second, True)
    assert queue.claim() is None


def test_old_success_clears_only_completed_targets(queue):
    queue.track_targets = True
    queue.register("sub", "secret", "tenant", "team", "channel")
    queue.accept([targeted_item("first")])
    first = queue.claim()
    queue.accept([targeted_item("second")])
    queue.finish(first, True)
    assert queue.claim()["selected_messages"] == ["second"]


def test_stale_lease_cannot_clear_target_from_new_claim(queue):
    queue.track_targets = True
    now = [1000]
    queue.clock = lambda: now[0]
    queue.register("sub", "secret", "tenant", "team", "channel")
    queue.accept([targeted_item()])
    first = queue.claim(seconds=1)
    now[0] += 2
    current = queue.claim()
    queue.finish(first, True)
    assert queue.claim() is None
    queue.finish(current, False)
    assert queue.claim()["selected_messages"] == ["older-message"]


@pytest.mark.parametrize(
    "resource",
    [None, "https://elsewhere.invalid/", "teams/team/channels/other/messages/older-message"],
)
def test_unknown_targets_are_retained_for_reconciliation(queue, resource):
    queue.track_targets = True
    queue.register("sub", "secret", "tenant", "team", "channel")
    queue.accept([{**targeted_item(), "resource": resource}])
    job = queue.claim()
    assert job["requires_reconciliation"]
    queue.finish(job, False)
    assert queue.claim()["requires_reconciliation"]


def test_reply_target_selects_the_root_thread(queue):
    queue.track_targets = True
    queue.register("sub", "secret", "tenant", "team", "channel")
    queue.accept(
        [
            {
                **targeted_item(),
                "resource": "teams/team/channels/channel/messages/older-message/replies/reply",
            }
        ]
    )
    job = queue.claim()
    assert not job["requires_reconciliation"]
    assert job["selected_messages"] == ["older-message"]


def test_oversized_target_batch_requires_reconciliation(queue):
    queue.track_targets = True
    queue.register("sub", "secret", "tenant", "team", "channel")
    queue.accept([targeted_item(str(index)) for index in range(21)])
    assert queue.claim()["requires_reconciliation"]
