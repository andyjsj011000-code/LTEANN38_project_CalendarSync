"""Opt-in retention tests on an explicitly disposable PostgreSQL database."""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from test_postgres_database_integration import postgres_database
from web_service.direct_source import DirectSource
from web_service.postgres_connection import PostgresConnectionFactory
from web_service.postgres_retention import PostgresRetention, decode_cache_payload
from web_service.postgres_shared_schema import enable_shared_channel_schema
from web_service.storage import digest


CHANNELS = [{"id": "class", "team_id": "team", "name": "Class"}]


def event(identity, end="2026-11-06"):
    return {
        "id": "direct:" + identity,
        "channel_id": "class",
        "key": identity,
        "title": "Private title " + identity,
        "notes": "Private source notes",
        "start_date": "2026-11-01",
        "end_date": end,
        "kind": "assignment",
        "time": None,
        "cancelled": False,
        "source_version": "v1",
        "source_url": "https://teams.microsoft.com/l/message/class/post",
    }


def set_day(p, day):
    p.now[0] = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    # The disposable fixture started at a synthetic epoch; keep its test-only
    # devices usable after moving the clock to the school-calendar date.
    with p.connect() as db:
        db.execute("UPDATE web_connections SET expires_at=%s", (p.now[0] + 86400,))


def publish(p, events, *, users=("alice", "bob")):
    for name in users:
        lease = p.repo.begin_sync(p.users[name], 0)
        p.repo.commit_snapshot(p.devices[name], CHANNELS, events, 60, lease)


def seed(p, events, message="post"):
    p.repo.cache_analysis(digest("class|" + message), "class", message, "v1", events)
    publish(p, events)


def prune(p, **kwargs):
    if not getattr(p, "retention_schema_ready", False):
        dsn = os.environ["CALENDARSYNC_DISPOSABLE_POSTGRES_DSN"]
        mode = "disable" if urlsplit(dsn).hostname in {"127.0.0.1", "localhost", "::1"} else None
        factory = PostgresConnectionFactory(
            dsn, p.schema, sslmode=mode, statement_timeout_ms=5000, lock_timeout_ms=4000
        )
        enable_shared_channel_schema(factory, p.repo.tenant_id)
        p.retention_schema_ready = True
    p.repo.shared_ingestion_enabled = True
    return PostgresRetention.prune_expired_team_events(p.repo, **kwargs)


def cache_payload(p, message="post"):
    with p.connect() as db:
        row = db.execute(
            "SELECT payload_blob FROM web_source_cache WHERE cache_key=%s",
            (digest("class|" + message),),
        ).fetchone()
    return decode_cache_payload(p.repo.unseal(row[0]), "class")


def event_ids(p):
    with p.connect() as db:
        return [row[0] for row in db.execute("SELECT id FROM web_events ORDER BY id").fetchall()]


def test_seven_full_days_mixed_post_and_personal_tasks_are_preserved(postgres_database):
    p = postgres_database
    set_day(p, "2026-11-13")
    old, boundary, future = (
        event("expired"),
        event("boundary", "2026-11-07"),
        event("future", "2026-12-01"),
    )
    seed(p, [old, boundary, future])
    task = p.repo.add_task(
        p.users["alice"],
        {
            "title": "Old personal task remains",
            "notes": "",
            "time": None,
            "start_date": "2026-11-01",
            "end_date": "2026-11-02",
            "display_mode": "daily",
            "selected_dates": [],
        },
    )
    with p.connect() as db:
        for identity, source in ((old["id"], "teams"), (task["id"], "personal")):
            db.execute(
                "INSERT INTO web_reminder_rules VALUES(%s,%s,%s,%s)",
                (p.users["alice"], identity, source, p.repo.seal({"offsets": []})),
            )

    assert prune(p)["events_deleted"] == 0  # Nov 6 + seven full days is retained.
    set_day(p, "2026-11-14")
    result = prune(p)

    assert result["events_deleted"] == result["cached_events_retired"] == 1
    assert event_ids(p) == [boundary["id"], future["id"]]
    payload = cache_payload(p)
    assert payload["events"] == [boundary, future]
    assert payload["retired"][0]["id"] == old["id"]
    assert payload["retired"][0]["key"] == "expired"
    assert "Private" not in str(payload["retired"])
    assert p.repo.task(p.users["alice"], task["id"])["title"] == "Old personal task remains"
    with p.connect() as db:
        assert db.execute("SELECT version FROM web_source_cache").fetchone()[0] == "v1"
        for table in (
            "web_event_states",
            "web_user_source_versions",
            "web_changes",
            "web_event_sources",
        ):
            assert (
                db.execute("SELECT 1 FROM " + table + " WHERE event_id=%s", (old["id"],)).fetchone()
                is None
            )
        assert db.execute("SELECT item_id,source FROM web_reminder_rules").fetchall() == [
            (task["id"], "personal")
        ]


def test_future_override_and_every_pending_decision_protect_the_shared_event(postgres_database):
    p = postgres_database
    set_day(p, "2026-11-14")
    extended, reviewing, expired = event("extended"), event("reviewing"), event("expired")
    seed(p, [extended, reviewing, expired])
    p.repo.override_event(p.users["alice"], "tenant", extended["id"], {"end_date": "2026-12-01"})
    # Even an entirely old pending change is a decision the user must not lose.
    with p.connect() as db:
        db.execute(
            """INSERT INTO web_changes
            (id,user_id,event_id,source_hash,payload_blob,created_at,status)
            VALUES(%s,%s,%s,%s,%s,%s,'pending')""",
            (
                "review",
                p.users["bob"],
                reviewing["id"],
                "pending-hash",
                p.repo.seal(reviewing),
                p.now[0],
            ),
        )

    assert prune(p)["events_deleted"] == 1
    assert event_ids(p) == [extended["id"], reviewing["id"]]
    assert [row["id"] for row in cache_payload(p)["events"]] == [extended["id"], reviewing["id"]]

    p.repo.override_event(p.users["alice"], "tenant", extended["id"], {"end_date": "2026-11-06"})
    p.repo.resolve_change(p.users["bob"], "tenant", "review", False)
    assert prune(p)["events_deleted"] == 2
    assert event_ids(p) == []
    assert len(cache_payload(p)["retired"]) == 3


def test_unchanged_post_and_stale_snapshot_cannot_resurrect_retired_events(postgres_database):
    p = postgres_database
    set_day(p, "2026-11-14")

    class Graph:
        version = "v1"

        def messages(self, *args):
            return [
                {
                    "id": "post",
                    "body": {"content": self.version},
                    "replies": [],
                    "createdDateTime": "2026-11-01T00:00:00Z",
                }
            ]

        def message(self, *args, **kwargs):
            return self.messages()[0]

    class Extractor:
        configured = True
        calls = 0
        previous = None

        def extract(self, text, images, previous):
            self.calls += 1
            self.previous = previous
            result = event("stable", "2026-11-06" if self.calls == 1 else "2026-12-01")
            return [
                {
                    key: value
                    for key, value in result.items()
                    if key not in {"id", "channel_id", "source_version", "source_url"}
                }
            ]

    graph, extractor = Graph(), Extractor()
    source = DirectSource(graph, extractor, images=False)
    original = source.collect("test-token", CHANNELS, "tenant", p.repo, p.users["alice"])
    assert len(original) == 1
    publish(p, original)
    stable_id = original[0]["id"]

    assert prune(p)["events_deleted"] == 1
    unchanged = source.collect("test-token", CHANNELS, "tenant", p.repo, p.users["alice"])
    assert unchanged == [] and extractor.calls == 1
    publish(p, original)  # An old in-memory catalog, read before maintenance.
    assert event_ids(p) == []

    p.now[0] += 301  # The unchanged pass made this shared channel fresh for five minutes.
    graph.version = "v2"
    changed = source.collect("test-token", CHANNELS, "tenant", p.repo, p.users["alice"])
    assert extractor.calls == 2 and any(item.get("key") == "stable" for item in extractor.previous)
    assert len(changed) == 1 and changed[0]["id"] == stable_id
    assert changed[0]["end_date"] == "2026-12-01"
    assert cache_payload(p)["retired"] == []
    publish(p, changed)
    assert event_ids(p) == [stable_id]


def test_locked_event_is_not_pruned_while_another_writer_can_reschedule_it(postgres_database):
    p = postgres_database
    set_day(p, "2026-11-14")
    old = event("locked")
    seed(p, [old])
    with p.connect() as held:
        held.execute("SELECT id FROM web_events WHERE id=%s FOR UPDATE", (old["id"],))
        result = prune(p)
        assert result["events_deleted"] == result["cached_events_retired"] == 0
        assert result["events_preserved"] == 1
    assert cache_payload(p)["events"] == [old]
    assert prune(p)["events_deleted"] == 1


def test_encryption_failure_rolls_back_cache_markers_and_event_deletion(
    postgres_database, monkeypatch
):
    p = postgres_database
    set_day(p, "2026-11-14")
    first, second = event("first"), event("second")
    seed(p, [first], "first-post")
    seed(p, [second], "second-post")
    original_seal = p.repo.seal
    calls = []

    def fail_second_envelope(payload):
        if isinstance(payload, dict) and "retired" in payload:
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("synthetic encryption failure")
        return original_seal(payload)

    with monkeypatch.context() as patch:
        patch.setattr(p.repo, "seal", fail_second_envelope)
        with pytest.raises(ValueError, match="synthetic encryption"):
            prune(p)
    assert len(calls) == 2
    assert event_ids(p) == [first["id"], second["id"]]
    assert cache_payload(p, "first-post") == {"events": [first], "retired": []}
    assert cache_payload(p, "second-post") == {"events": [second], "retired": []}
    assert prune(p)["events_deleted"] == 2


def test_explicit_reminders_protect_future_and_exact_delivery_window(postgres_database):
    p = postgres_database
    set_day(p, "2026-11-14")
    now = datetime.fromtimestamp(p.now[0], timezone.utc)
    offsets = {"future": 86400, "due_now": 0, "inside": -599, "boundary": -600, "older": -601}
    items = [event(name) for name in offsets]
    seed(p, items)
    with p.connect() as db:
        for item in items:
            value = {
                "offsets": [],
                "custom_times": [(now + timedelta(seconds=offsets[item["key"]])).isoformat()],
            }
            db.execute(
                "INSERT INTO web_reminder_rules VALUES(%s,%s,%s,%s)",
                (p.users["alice"], item["id"], "teams", p.repo.seal(value)),
            )
    result = prune(p)
    assert result["events_deleted"] == 2
    assert event_ids(p) == ["direct:due_now", "direct:future", "direct:inside"]
    assert {row["id"] for row in cache_payload(p)["events"]} == set(event_ids(p))
    with p.connect() as db:
        assert sorted(
            row[0] for row in db.execute("SELECT item_id FROM web_reminder_rules").fetchall()
        ) == event_ids(p)
    # Once the latest explicit reminder's existing delivery window has ended,
    # normal date retention applies again without extending the notification.
    p.now[0] += 86400 + 600
    assert prune(p)["events_deleted"] == 3
    assert event_ids(p) == []


def test_invalid_explicit_rules_are_preserved_conservatively(postgres_database):
    p = postgres_database
    set_day(p, "2026-11-14")
    items = [event("invalid" + str(index)) for index in range(4)] + [event("ordinary")]
    seed(p, items)
    invalid = [
        [],
        {"custom_times": "not-a-list"},
        {"custom_times": ["not-a-date"]},
        {"custom_times": ["2026-11-15T12:00:00"]},
    ]
    with p.connect() as db:
        for item, value in zip(items, invalid):
            db.execute(
                "INSERT INTO web_reminder_rules VALUES(%s,%s,%s,%s)",
                (p.users["alice"], item["id"], "teams", p.repo.seal(value)),
            )
    assert prune(p)["events_deleted"] == 1
    assert event_ids(p) == [item["id"] for item in items[:-1]]
