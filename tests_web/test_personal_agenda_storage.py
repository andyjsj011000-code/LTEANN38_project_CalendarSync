"""Personal Today reads stay independent of Teams; PostgreSQL uses a SQL shim."""

from contextlib import contextmanager
import re
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.postgres_personal_tasks import PostgresPersonalTasks
from web_service.storage import Database


DAY = "2026-11-07"


@pytest.fixture(params=["sqlite", "postgres-shim"])
def personal_store(request, tmp_path, monkeypatch):
    base = Database(tmp_path / "personal.sqlite3", Fernet.generate_key().decode())
    base.initialize()
    alice = base.save_user("school", "alice", {"name": "Alice"})
    bob = base.save_user("school", "bob", {"name": "Bob"})
    outsider = base.save_user("another-school", "other", {"name": "Other"})
    queries = []
    connect = base.connect

    @contextmanager
    def tracked_connection():
        with connect() as db:
            db.set_trace_callback(queries.append)
            yield db

    monkeypatch.setattr(base, "connect", tracked_connection)
    if request.param == "postgres-shim":

        class Shim:
            def __init__(self, db):
                self.db = db

            def execute(self, query, values=()):
                return self.db.execute(re.sub(r" FOR UPDATE", "", query).replace("%s", "?"), values)

        @contextmanager
        def postgres_connection():
            with tracked_connection() as db:
                yield Shim(db)

        repo = PostgresPersonalTasks(postgres_connection, base.seal, base.unseal)
    else:
        repo = base
    return SimpleNamespace(
        repo=repo, base=base, alice=alice, bob=bob, outsider=outsider, queries=queries
    )


def payload(**values):
    return {
        "title": "Personal task",
        "notes": "Private notes",
        "time": None,
        "start_date": DAY,
        "end_date": DAY,
        "display_mode": "daily",
        "selected_dates": [],
        **values,
    }


def test_returns_complete_items_for_only_the_requested_owner(personal_store):
    p = personal_store
    own = p.repo.add_task(p.alice, payload(title="Alice task", time="12:30"))
    other = p.repo.add_task(p.bob, payload(title="Bob task"))
    p.repo.add_task(p.outsider, payload(title="Other school task"))

    assert p.repo.personal_agenda(p.alice, "school", DAY) == {"date": DAY, "personal": [own]}
    assert p.repo.personal_agenda(p.bob, "school", DAY) == {"date": DAY, "personal": [other]}


@pytest.mark.parametrize("invalid", ["tenant", "missing-user", "inactive-user"])
def test_invalid_identity_cannot_read_personal_items(personal_store, invalid):
    p = personal_store
    p.repo.add_task(p.alice, payload())
    user, tenant = p.alice, "school"
    if invalid == "tenant":
        tenant = "another-school"
    elif invalid == "missing-user":
        user = "unknown-user"
    else:
        with p.base.connect() as db:
            db.execute("UPDATE web_users SET active=0 WHERE id=?", (p.alice,))

    assert p.repo.personal_agenda(user, tenant, DAY) == {"date": DAY, "personal": []}


def test_daily_selected_and_final_rules_respect_the_task_period(personal_store):
    p = personal_store
    items = {}
    for mode in ("daily", "selected", "final"):
        items[mode] = p.repo.add_task(
            p.alice,
            payload(
                title=mode,
                start_date="2026-11-05",
                end_date="2026-11-09",
                display_mode=mode,
                selected_dates=["2026-11-06"] if mode == "selected" else [],
            ),
        )
    expected = {
        "2026-11-04": [],
        "2026-11-05": ["daily"],
        "2026-11-06": ["daily", "selected"],
        "2026-11-07": ["daily"],
        "2026-11-08": ["daily"],
        # The final date must appear even when not explicitly in selected_dates.
        "2026-11-09": ["daily", "selected", "final"],
        "2026-11-10": [],
    }
    for day, modes in expected.items():
        result = p.repo.personal_agenda(p.alice, "school", day)
        assert result["date"] == day
        assert {item["id"] for item in result["personal"]} == {
            items[mode]["id"] for mode in modes
        }, day


def test_hidden_items_are_excluded_but_completed_items_keep_their_version(personal_store):
    p = personal_store
    visible = p.repo.add_task(p.alice, payload(title="Visible"))
    completed = p.repo.add_task(p.alice, payload(title="Completed"))
    hidden = p.repo.add_task(p.alice, payload(title="Hidden"))
    completed = p.repo.task_state(
        p.alice, completed["id"], {"completed": True}, completed["version"]
    )
    p.repo.task_state(p.alice, hidden["id"], {"hidden": True}, hidden["version"])

    items = {
        item["id"]: item for item in p.repo.personal_agenda(p.alice, "school", DAY)["personal"]
    }
    assert set(items) == {visible["id"], completed["id"]}
    assert items[completed["id"]] == completed
    assert items[visible["id"]] == visible


def test_time_then_id_order_is_stable_and_all_day_items_come_last(personal_store):
    p = personal_store
    records = [
        ("all-day-z", None),
        ("same-time-z", "09:00"),
        ("earlier-z", "08:00"),
        ("all-day-a", None),
        ("same-time-a", "09:00"),
    ]
    with p.base.connect() as db:
        for identity, when in records:
            value = payload(title=identity, time=when)
            db.execute(
                """INSERT INTO web_personal_tasks
                (id,user_id,start_date,end_date,payload_blob,completed)
                VALUES(?,?,?,?,?,?)""",
                (identity, p.alice, DAY, DAY, p.base.seal(value), int(identity == "same-time-a")),
            )
    expected = ["earlier-z", "same-time-a", "same-time-z", "all-day-a", "all-day-z"]
    for _ in range(2):
        result = p.repo.personal_agenda(p.alice, "school", DAY)
        assert [item["id"] for item in result["personal"]] == expected


def test_personal_agenda_does_not_call_or_query_teams_paths(personal_store, monkeypatch):
    p = personal_store
    own = p.repo.add_task(p.alice, payload())

    def forbidden(*args, **kwargs):
        pytest.fail("Personal Today must not wait for a Teams-dependent read")

    for method in ("_shared", "calendar", "agenda"):
        monkeypatch.setattr(p.repo, method, forbidden, raising=False)
    p.queries.clear()
    assert p.repo.personal_agenda(p.alice, "school", DAY) == {"date": DAY, "personal": [own]}
    shared_tables = r"\bweb_(?:channels|channel_access|channel_preferences|events|event_states|changes|source_cache)\b"
    assert not any(re.search(shared_tables, query, re.IGNORECASE) for query in p.queries)
