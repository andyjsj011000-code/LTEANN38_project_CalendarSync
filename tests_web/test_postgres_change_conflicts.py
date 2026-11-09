"""Stale review identities via the existing SQLite repository shim."""

import pytest

from test_postgres_events import events, source
from web_service.storage import Conflict, Missing


@pytest.mark.parametrize("status", ["accepted", "kept", "superseded"])
@pytest.mark.parametrize("accept", [False, True])
def test_stale_own_review_conflicts_without_deciding_the_replacement(events, status, accept):
    e = events
    e.review()
    e.execute("UPDATE web_changes SET status=%s WHERE id=%s", (status, "change-alice"))
    e.execute(
        """INSERT INTO web_changes
        (id,user_id,event_id,source_hash,payload_blob,created_at)
        VALUES(%s,%s,%s,%s,%s,%s)""",
        (
            "new-change",
            "alice",
            "direct:event",
            "latest-hash",
            e.seal(source(title="Latest revision", source_version="v3")),
            1001,
        ),
    )
    before = e.repo._shared("alice", "tenant")

    with pytest.raises(Conflict):
        e.repo.resolve_change("alice", "tenant", "change-alice", accept)

    assert e.repo._shared("alice", "tenant") == before
    assert e.execute("SELECT id,status FROM web_changes ORDER BY id") == [
        ("change-alice", status),
        ("new-change", "pending"),
    ]
    assert e.execute("SELECT seen_hash FROM web_user_source_versions WHERE user_id='alice'") == [
        ("baseline",)
    ]
    assert e.repo.changes("alice", "tenant")[0]["id"] == "new-change"


@pytest.mark.parametrize(
    "user,tenant,identity",
    [
        ("bob", "tenant", "change-alice"),
        ("alice", "other", "change-alice"),
        ("alice", "tenant", "unknown-change"),
    ],
)
def test_stale_review_conflict_does_not_disclose_other_users_or_unknown_ids(
    events, user, tenant, identity
):
    events.review()
    events.execute("UPDATE web_changes SET status='accepted' WHERE id='change-alice'")
    with pytest.raises(Missing):
        events.repo.resolve_change(user, tenant, identity, True)
    assert events.execute("SELECT status FROM web_changes") == [("accepted",)]


def test_access_is_checked_before_reporting_an_own_stale_review(events):
    events.review()
    events.execute("UPDATE web_changes SET status='superseded' WHERE id='change-alice'")
    events.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    with pytest.raises(Missing):
        events.repo.resolve_change("alice", "tenant", "change-alice", True)
