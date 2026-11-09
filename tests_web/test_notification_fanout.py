import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from web_service.notification_fanout import FanoutQueue, FanoutRuntime
from web_service.storage import digest
from web_service.test_storage import TestDatabase


@pytest.fixture
def fanout(tmp_path):
    path = tmp_path / "fanout.sqlite3"
    now = [1000]
    cipher = Fernet(Fernet.generate_key())
    seal = lambda value: cipher.encrypt(json.dumps(value).encode()).decode()
    unseal = lambda value: json.loads(cipher.decrypt(value.encode()))

    @contextmanager
    def connect():
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection

    with connect() as connection:
        connection.executescript("""CREATE TABLE web_users(id TEXT PRIMARY KEY,tenant_id TEXT,active INTEGER);
            CREATE TABLE web_connections(id_hash TEXT PRIMARY KEY,user_id TEXT,active INTEGER,
                background INTEGER,expires_at INTEGER,state TEXT);""")
        for user in ("alice", "bob"):
            connection.execute("INSERT INTO web_users VALUES(?,?,1)", (user, "tenant"))
            connection.execute(
                "INSERT INTO web_connections VALUES(?,?,1,1,99999,?)", (user, user, "connected")
            )
    queue = FanoutQueue(connect, seal, unseal, lambda: now[0])
    queue.register("sub", "secret", "tenant", "team", "channel")
    devices = {
        user: {"id_hash": user, "user_id": user, "tenant_id": "tenant", "object_id": user}
        for user in ("alice", "bob")
    }

    class DB:
        commits = []
        clock = staticmethod(lambda: now[0])

        def connect(self):
            return connect()

        def device(self, key):
            with connect() as db:
                active = db.execute(
                    "SELECT active FROM web_connections WHERE id_hash=?", (key,)
                ).fetchone()
            return devices[key] if active and active[0] else None

        def begin_sync(self, *args):
            return 123

        def fail_sync(self, *args):
            pass

        def commit_snapshot(self, device, *args):
            self.commits.append(device["user_id"])

    class Tokens:
        def acquire(self, device):
            return device["object_id"]

    class Graph:
        denied = set()

        def channels(self, token, *args):
            return [] if token in self.denied else [{"id": "channel", "team_id": "team"}]

    class Source:
        def collect(self, token, channels, tenant, db, user, **kwargs):
            assert kwargs["selected_messages"] == ["root"]
            assert kwargs["selected_channels"] == channels
            return []

    db = DB()
    graph = Graph()

    def runtime(q=queue):
        result = FanoutRuntime(q, db, Tokens(), graph, Source(), 60)
        result.all_candidates = lambda job: list(devices.values())
        return result

    item = {
        "subscriptionId": "sub",
        "clientState": "secret",
        "resource": "teams/team/channels/channel/messages/root",
    }
    return SimpleNamespace(
        queue=queue,
        connect=connect,
        seal=seal,
        unseal=unseal,
        now=now,
        db=db,
        graph=graph,
        runtime=runtime,
        item=item,
    )


def test_every_user_has_durable_work_before_channel_is_acknowledged(fanout):
    f = fanout
    f.queue.accept([f.item])
    runtime = f.runtime()
    assert runtime.stage() == "staged"
    assert f.queue.claim() is None
    # Simulate process restart: pending per-user work remains in SQLite.
    reopened = FanoutQueue(f.connect, f.seal, f.unseal, lambda: f.now[0])
    worker = f.runtime(reopened)
    assert worker.tick() == "processed"
    assert worker.tick() == "processed"
    assert set(f.db.commits) == {"alice", "bob"}
    assert reopened.claim_user() is None


def test_denied_user_is_not_committed_and_does_not_erase_other_user_work(fanout):
    f = fanout
    f.graph.denied.add("alice")
    f.queue.accept([f.item])
    runtime = f.runtime()
    runtime.tick()
    runtime.tick()
    assert f.db.commits == ["bob"]
    assert f.queue.claim_user() is None
    f.now[0] += 60
    pending = f.queue.claim_user()
    assert pending["user_id"] == "alice"


def test_stale_user_lease_cannot_delete_newer_claim(fanout):
    f = fanout
    f.queue.accept([f.item])
    job = f.queue.claim()
    assert f.queue.handoff(job, ["alice"])
    first = f.queue.claim_user(seconds=1)
    f.now[0] += 2
    second = f.queue.claim_user()
    f.queue.finish_user(first, True)
    assert f.queue.user_job_counts() == {"pending": 1, "blocked": 0}
    assert f.queue.claim_user() is None
    f.queue.finish_user(second, True)
    assert f.queue.user_job_counts() == {"pending": 0, "blocked": 0}


def test_new_notification_after_channel_revision_resets_is_not_deduplicated(fanout):
    f = fanout
    f.queue.accept([f.item])
    f.runtime().stage()
    f.queue.accept([f.item])
    f.runtime().stage()
    with f.connect() as db:
        assert db.execute("SELECT count(*) FROM graph_user_jobs").fetchone()[0] == 4


def test_handoff_keeps_notification_arriving_after_claim(fanout):
    f = fanout
    f.queue.accept([f.item])
    first = f.queue.claim()
    f.queue.accept([f.item])
    assert f.queue.handoff(first, ["alice"])
    next_job = f.queue.claim()
    assert next_job["revision"] == 2 and next_job["selected_messages"] == ["root"]


def test_no_active_recipient_never_acknowledges_channel(fanout):
    f = fanout
    f.queue.accept([f.item])
    job = f.queue.claim()
    with f.connect() as db:
        db.execute("UPDATE web_users SET active=0")
    assert not f.queue.handoff(job, ["alice", "bob"])
    f.queue.finish(job, False, retry_delay=60)
    f.now[0] += 60
    assert f.queue.claim() is not None


def test_minute_tick_does_not_starve_other_user_after_failure(fanout):
    f = fanout
    f.queue.accept([f.item])
    runtime = f.runtime()
    runtime.stage()
    # The first child fails repeatedly; simulate a scheduler that ticks once
    # per minute, exactly when its retry becomes eligible again.
    first = f.queue.claim_user()
    f.graph.denied.add(first["user_id"])
    f.queue.finish_user(first, False)
    f.now[0] += 60
    assert runtime.tick() == "processed"
    assert f.db.commits == [({"alice", "bob"} - {first["user_id"]}).pop()]


@pytest.mark.parametrize("payload", ["damaged-ciphertext", "encrypted-invalid-identifiers"])
def test_bad_targets_are_preserved_without_blocking_other_users(fanout, payload):
    f = fanout
    f.queue.accept([f.item])
    f.runtime().stage()
    with f.connect() as db:
        key = db.execute(
            "SELECT id FROM graph_user_jobs ORDER BY created_at,id LIMIT 1"
        ).fetchone()[0]
        blob = (
            f.seal({"selected_messages": ["root/another-path"]})
            if payload == "encrypted-invalid-identifiers"
            else payload
        )
        db.execute("UPDATE graph_user_jobs SET targets_blob=? WHERE id=?", (blob, key))
    assert f.runtime().tick() == "processed"
    assert f.queue.user_job_counts() == {"pending": 0, "blocked": 1}
    assert f.queue.claim_user() is None


def test_handoff_transaction_rolls_back_all_children_and_preserves_channel(fanout):
    f = fanout
    f.queue.accept([f.item])
    job = f.queue.claim()
    with f.connect() as db:
        db.execute("""CREATE TRIGGER simulate_write_failure BEFORE INSERT ON graph_user_jobs
            WHEN NEW.user_id='bob' BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        f.queue.handoff(job, ["alice", "bob"])
    assert f.queue.user_job_counts() == {"pending": 0, "blocked": 0}
    f.queue.finish(job, False, retry_delay=60)
    f.now[0] += 60
    assert f.queue.claim()["selected_messages"] == ["root"]


def test_stage_exception_does_not_stop_already_persisted_children(fanout):
    f = fanout
    f.queue.accept([f.item])
    runtime = f.runtime()
    runtime.stage()

    def unavailable():
        raise RuntimeError("simulated staging failure")

    runtime.stage = unavailable
    assert runtime.tick() == "processed"
    assert len(f.db.commits) == 1


def test_wrong_tenant_recipient_is_excluded_before_handoff(fanout):
    f = fanout
    with f.connect() as db:
        db.execute("INSERT INTO web_users VALUES('outsider','different-tenant',1)")
    f.queue.accept([f.item])
    job = f.queue.claim()
    assert f.queue.handoff(job, ["alice", "outsider"])
    child = f.queue.claim_user()
    assert child["user_id"] == "alice"
    f.queue.finish_user(child, True)
    assert f.queue.claim_user() is None


def test_old_experimental_schema_requires_explicit_migration(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE graph_user_jobs(id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="explicit migration"):
        FanoutQueue(lambda: sqlite3.connect(path), lambda x: x, lambda x: x, lambda: 1000)
    with sqlite3.connect(path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(graph_user_jobs)")] == ["id"]


def test_real_storage_fanout_saves_each_user_and_isolates_revoked_access(tmp_path):
    now = [1000]
    db = TestDatabase(
        tmp_path / "real.sqlite3", Fernet.generate_key().decode(), clock=lambda: now[0]
    )
    db.initialize()
    channels = [{"id": "channel", "team_id": "team", "name": "Class"}]
    users = {}
    for name in ("alice", "bob"):
        user = db.save_user("tenant", name, {"name": name})
        session = db.issue_session(user, 3600)
        raw, _ = db.add_connection(session, user, "token-" + name, 3600, "cache", background=True)
        device = db.device(digest(raw))
        lease = db.begin_sync(user, 0)
        db.commit_snapshot(device, channels, [], 60, lease)
        users[name] = user
    q = FanoutQueue(db.connect, db.seal, db.unseal, db.clock)
    q.register("sub", "secret", "tenant", "team", "channel")

    class Tokens:
        def acquire(self, device):
            return device["object_id"]

    class Graph:
        denied = set()

        def channels(self, token, *args):
            return [] if token in self.denied else channels

    class Source:
        version = "first"
        calls = []

        def collect(self, token, allowed, tenant, storage, user, **kwargs):
            assert tenant == "tenant" and allowed == channels
            assert kwargs == {"selected_channels": channels, "selected_messages": ["root"]}
            self.calls.append(user)
            return [
                {
                    "id": "direct:shared",
                    "channel_id": "channel",
                    "title": self.version,
                    "start_date": "2026-11-06",
                    "end_date": "2026-11-06",
                    "kind": "event",
                    "notes": "",
                    "time": None,
                    "source_url": "https://teams.microsoft.com/",
                }
            ]

    source = Source()
    graph = Graph()
    runtime = FanoutRuntime(q, db, Tokens(), graph, source, 60)
    item = {
        "subscriptionId": "sub",
        "clientState": "secret",
        "resource": "teams/team/channels/channel/messages/root",
    }
    q.accept([item])
    assert runtime.tick() == "processed"
    assert runtime.tick() == "processed"
    with db.connect() as c:
        assert {row[0] for row in c.execute("SELECT user_id FROM web_user_source_versions")} == set(
            users.values()
        )
    assert set(source.calls) == set(users.values())

    # Both users had historical access; a new delegated check now denies Bob.
    # Alice can still get a source update, while Bob gains neither ACL nor change.
    source.calls = []
    source.version = "second"
    graph.denied.add("bob")
    q.accept([item])
    assert {runtime.tick(), runtime.tick()} == {"processed", "channel_not_authorized"}
    assert source.calls == [users["alice"]]
    with db.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM web_channel_access WHERE user_id=?", (users["bob"],)
            ).fetchone()[0]
            == 0
        )
        assert {
            row[0] for row in c.execute("SELECT user_id FROM web_changes WHERE status='pending'")
        } == {users["alice"]}
    assert q.user_job_counts() == {"pending": 1, "blocked": 0}
