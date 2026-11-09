"""Targeted notification staging tests using local SQLite and fake Graph."""

import sqlite3
import pytest

from web_service.notification_queue import NotificationQueue
from web_service.notification_worker import process_claimed


def queue(tmp_path):
    q = NotificationQueue(
        lambda: sqlite3.connect(tmp_path / "queue.sqlite3"), clock=lambda: 1000, track_targets=True
    )
    q.register("sub", "secret", "tenant", "team", "channel")
    return q


def item(message="old-post"):
    return {
        "subscriptionId": "sub",
        "clientState": "secret",
        "resource": "teams/team/channels/channel/messages/" + message,
    }


def test_target_is_preserved_when_same_message_changes_during_processing(tmp_path):
    q = queue(tmp_path)
    q.accept([item()])
    first = q.claim()
    assert first["selected_messages"] == ["old-post"]
    assert not first["requires_reconciliation"]
    q.accept([item()])
    q.finish(first, True)
    second = q.claim()
    assert second["revision"] == 2 and second["selected_messages"] == ["old-post"]
    q.finish(second, True)
    assert q.claim() is None


def test_new_target_is_not_removed_by_old_worker(tmp_path):
    q = queue(tmp_path)
    q.accept([item("first")])
    first = q.claim()
    q.accept([item("second")])
    q.finish(first, True)
    assert q.claim()["selected_messages"] == ["second"]


@pytest.mark.parametrize(
    "resource", ["https://example.invalid/secret", "teams/team/channels/other/messages/old-post"]
)
def test_unknown_or_foreign_path_requires_reconciliation(tmp_path, resource):
    q = queue(tmp_path)
    q.accept([{**item(), "resource": resource}])
    job = q.claim()
    assert job["requires_reconciliation"]
    # Must fail closed before touching credentials or Graph.
    assert process_claimed(job, None, None, None, None, None, 60) == "requires_reconciliation"


def test_reply_and_root_targets_use_one_root_read(tmp_path):
    q = queue(tmp_path)
    q.accept([item(), {**item(), "resource": item()["resource"] + "/replies/reply"}])
    job = q.claim()
    assert not job["requires_reconciliation"]
    assert job["selected_messages"] == ["old-post"]


def test_selected_message_ids_are_passed_after_live_access_verification():
    device = {"id_hash": "d", "tenant_id": "tenant", "user_id": "student", "object_id": "oid"}

    class DB:
        def device(self, *args):
            return device

        def begin_sync(self, *args):
            return 123

        def commit_snapshot(self, *args):
            pass

        def fail_sync(self, *args):
            raise AssertionError("unexpected failure")

    class Tokens:
        def acquire(self, *args):
            return "delegated"

    class Graph:
        def channels(self, *args):
            return [{"id": "channel", "team_id": "team"}]

    class Source:
        def collect(self, token, channels, tenant, db, user, **arguments):
            assert token == "delegated"
            assert arguments["selected_messages"] == ["old-post"]
            assert arguments["selected_channels"] == channels
            return []

    assert (
        process_claimed(
            {
                "tenant_id": "tenant",
                "team_id": "team",
                "channel_id": "channel",
                "selected_messages": ["old-post"],
            },
            device,
            DB(),
            Tokens(),
            Graph(),
            Source(),
            60,
        )
        == "processed"
    )
