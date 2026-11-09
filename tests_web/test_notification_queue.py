import sqlite3

import pytest

from web_service.notification_queue import NotificationQueue


def queue(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    return NotificationQueue(lambda: sqlite3.connect(path), clock=lambda: 1000)


def register(q):
    q.register("subscription", "secret-state", "tenant", "team", "channel")


def item(state="secret-state", **extra):
    return {"subscriptionId": "subscription", "clientState": state, **extra}


def test_known_subscription_and_client_state_required(tmp_path):
    q = queue(tmp_path)
    register(q)
    assert (
        q.accept([item("wrong"), {"subscriptionId": "other", "clientState": "secret-state"}, None])
        == 0
    )
    assert q.claim() is None
    assert q.accept([item(resource="attacker-value", tenantId="wrong")]) == 1
    job = q.claim()
    assert (job["tenant_id"], job["team_id"], job["channel_id"]) == ("tenant", "team", "channel")


def test_retries_and_notifications_arriving_during_processing_are_not_lost(tmp_path):
    q = queue(tmp_path)
    register(q)
    assert q.accept([item(), item()]) == 2
    first = q.claim()
    assert first["revision"] == 2
    assert q.claim() is None
    q.accept([item()])
    q.finish(first, True)
    second = q.claim()
    assert second["revision"] == 3
    q.finish(second, False)
    third = q.claim()
    q.finish(third, True)
    assert q.claim() is None


def test_late_worker_cannot_clear_new_lease(tmp_path):
    now = [1000]
    path = tmp_path / "inbox.sqlite3"
    q = NotificationQueue(lambda: sqlite3.connect(path), clock=lambda: now[0])
    register(q)
    q.accept([item()])
    first = q.claim(seconds=1)
    now[0] += 2
    second = q.claim()
    q.finish(first, True)
    assert q.claim() is None
    q.finish(second, True)
    assert q.claim() is None


def test_lifecycle_removal_requires_valid_state_and_stops_new_work(tmp_path):
    q = queue(tmp_path)
    register(q)
    assert q.accept_lifecycle([item("wrong", lifecycleEvent="subscriptionRemoved")]) == 0
    assert q.accept([item()]) == 1
    assert q.accept_lifecycle([item(lifecycleEvent="subscriptionRemoved")]) == 1
    assert q.claim() is None
    assert q.accept([item()]) == 0


def test_channel_has_only_one_active_subscription(tmp_path):
    q = queue(tmp_path)
    register(q)
    assert q.active_subscription("tenant", "team", "channel") == "subscription"
    with pytest.raises(sqlite3.IntegrityError):
        q.register("another", "different-state", "tenant", "team", "channel")
    q.accept_lifecycle([item(lifecycleEvent="subscriptionRemoved")])
    q.register("another", "different-state", "tenant", "team", "channel")
    assert q.active_subscription("tenant", "team", "channel") == "another"
