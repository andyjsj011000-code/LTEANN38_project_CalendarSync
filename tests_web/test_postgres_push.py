"""SQLite SQL-shim checks; PostgreSQL locking and external sending are not exercised."""

import base64
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from web_service.postgres_devices import PostgresDevices
from web_service.postgres_push import PostgresPush
from web_service.storage import Conflict, Missing, digest


@pytest.fixture
def push(tmp_path):
    path, now = tmp_path / "push.sqlite3", [1000]
    with sqlite3.connect(path) as db:
        db.executescript(
            (Path(__file__).parents[1] / "web_service" / "postgresql_schema_draft.sql").read_text(
                encoding="utf-8"
            )
        )
        db.executemany(
            "INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(?,?,?,?)",
            [("alice", "tenant", "a", "x"), ("bob", "tenant", "b", "x")],
        )
        db.executemany(
            "INSERT INTO web_sessions VALUES(?,?,?,?)",
            [
                (digest("session-a"), "alice", "csrf-a", 9999),
                (digest("session-b"), "bob", "csrf-b", 9999),
            ],
        )

    class Shim:
        def __init__(self, connection):
            self.db = connection

        def execute(self, sql, values=()):
            return self.db.execute(
                re.sub(r" FOR UPDATE(?: OF \w+)?", "", sql).replace("%s", "?"), values
            )

    @contextmanager
    def connect():
        db = sqlite3.connect(path)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield Shim(db)
        finally:
            db.close()

    cipher = Fernet(Fernet.generate_key())

    def seal(value):
        return cipher.encrypt(json.dumps(value).encode()).decode()

    def unseal(value):
        return json.loads(cipher.decrypt(value.encode()))

    devices = PostgresDevices(connect, seal, unseal, clock=lambda: now[0])

    def make_device(session, user, background=True):
        raw, _ = devices.add_connection(
            session, user, "token", 3600, cache="cache" if background else "", background=background
        )
        return devices.device(digest(raw))

    alice = make_device("session-a", "alice")
    bob = make_device("session-b", "bob")
    repo = PostgresPush(connect, seal, unseal, clock=lambda: now[0])

    def execute(sql, values=()):
        with connect() as db:
            return db.execute(sql, values).fetchall()

    return SimpleNamespace(
        repo=repo,
        devices=devices,
        alice=alice,
        bob=bob,
        now=now,
        execute=execute,
        seal=seal,
        unseal=unseal,
    )


def subscription(suffix="one"):
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/" + suffix,
        "keys": {"p256dh": encode(public), "auth": encode(b"private-auth-key")},
    }


def test_subscriptions_are_encrypted_owned_and_updated_without_transferring_endpoints(push):
    p = push
    value = subscription()
    key = p.repo.subscribe(p.alice, value)
    assert p.repo.subscribe(p.alice, value) == key
    rows = p.repo.subscriptions(p.alice["id_hash"])
    assert len(rows) == 1 and rows[0]["id"] == key
    assert rows[0]["subscription"]["endpoint"] == value["endpoint"]
    assert value["endpoint"] not in rows[0]["subscription_blob"]
    with pytest.raises(Conflict):
        p.repo.subscribe(p.bob, value)
    assert p.repo.subscriptions(p.bob["id_hash"]) == []
    with pytest.raises(Missing):
        p.repo.subscribe({**p.alice, "user_id": "bob"}, subscription("forged-owner"))
    with pytest.raises(Missing):
        p.repo.subscribe({**p.alice, "tenant_id": "foreign"}, subscription("forged-tenant"))


def test_per_device_cap_updates_existing_and_unsubscribe_is_owner_scoped(push):
    p = push
    values = [subscription(str(i)) for i in range(4)]
    keys = [p.repo.subscribe(p.alice, value) for value in values[:3]]
    with pytest.raises(Conflict):
        p.repo.subscribe(p.alice, values[3])
    assert p.repo.subscribe(p.alice, values[0]) == keys[0]
    p.repo.unsubscribe_subscription(p.bob["id_hash"], keys[0])
    assert len(p.repo.subscriptions(p.alice["id_hash"])) == 3
    p.repo.unsubscribe_subscription(p.alice["id_hash"], keys[0])
    assert len(p.repo.subscriptions(p.alice["id_hash"])) == 2
    p.repo.unsubscribe(p.alice["id_hash"])
    assert p.repo.subscriptions(p.alice["id_hash"]) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE web_users SET active=0 WHERE id='alice'",
        "UPDATE web_connections SET active=0 WHERE user_id='alice'",
        "UPDATE web_connections SET expires_at=1000 WHERE user_id='alice'",
        "UPDATE web_connections SET background=0 WHERE user_id='alice'",
        "UPDATE web_connections SET state='reauth_required' WHERE user_id='alice'",
    ],
)
def test_inactive_connections_cannot_register_read_claim_or_finish(push, mutation):
    p = push
    sub = p.repo.subscribe(p.alice, subscription())
    lease = p.repo.claim_delivery_lease("delivery", sub)
    p.execute(mutation)
    with pytest.raises(Missing):
        p.repo.subscribe(p.alice, subscription("new"))
    assert p.repo.subscriptions(p.alice["id_hash"]) == []
    assert p.repo.claim_delivery_lease("another", sub) is None
    assert not p.repo.finish_delivery_lease("delivery", lease, True)
    assert p.execute("SELECT state FROM web_deliveries WHERE id='delivery'") == [("pending",)]


def test_late_completion_cannot_override_new_lease_and_sent_is_final(push):
    p = push
    sub = p.repo.subscribe(p.alice, subscription())
    old = p.repo.claim_delivery_lease("delivery", sub)
    assert isinstance(old, str) and len(old) == 64
    assert p.repo.claim_delivery_lease("delivery", sub) is None
    assert not p.repo.finish_delivery_lease("delivery", "wrong-lease", True)
    p.now[0] += 30
    assert not p.repo.finish_delivery_lease("delivery", old, True)
    current = p.repo.claim_delivery_lease("delivery", sub)
    assert current and current != old
    assert not p.repo.finish_delivery_lease("delivery", old, True)
    assert p.repo.finish_delivery_lease("delivery", current, True)
    assert not p.repo.finish_delivery_lease("delivery", current, False)
    assert p.repo.claim_delivery_lease("delivery", sub) is None
    assert p.execute(
        "SELECT state,attempts,lease_token,lease_until FROM web_deliveries WHERE id='delivery'"
    ) == [("sent", 2, None, 0)]


def test_failed_delivery_backoff_and_five_attempt_cap(push):
    p = push
    sub = p.repo.subscribe(p.alice, subscription())
    for attempt in range(5):
        lease = p.repo.claim_delivery_lease("delivery", sub)
        assert lease
        assert p.repo.finish_delivery_lease("delivery", lease, False)
        assert p.repo.claim_delivery_lease("delivery", sub) is None
        p.now[0] += 60
    assert p.repo.claim_delivery_lease("delivery", sub) is None
    assert p.execute("SELECT state,attempts FROM web_deliveries WHERE id='delivery'") == [
        ("pending", 5)
    ]


def test_delivery_identity_cannot_be_rebound_to_another_subscription(push):
    p = push
    first = p.repo.subscribe(p.alice, subscription("first"))
    second = p.repo.subscribe(p.bob, subscription("second"))
    lease = p.repo.claim_delivery_lease("delivery", first)
    assert p.repo.claim_delivery_lease("delivery", second) is None
    assert p.repo.finish_delivery_lease("delivery", lease, True)
    assert p.execute(
        "SELECT subscription_id,attempts,state FROM web_deliveries WHERE id='delivery'"
    ) == [(first, 1, "sent")]


def test_unsubscribe_cascades_delivery_and_legacy_unfenced_api_fails_closed(push):
    p = push
    sub = p.repo.subscribe(p.alice, subscription())
    lease = p.repo.claim_delivery_lease("delivery", sub)
    p.repo.unsubscribe_subscription(p.alice["id_hash"], sub)
    assert p.execute("SELECT * FROM web_deliveries") == []
    assert not p.repo.finish_delivery_lease("delivery", lease, True)
    assert p.repo.claim_delivery_lease("new", sub) is None
    with pytest.raises(RuntimeError, match="lease"):
        p.repo.claim_delivery("unsafe", sub)
    with pytest.raises(RuntimeError, match="lease"):
        p.repo.finish_delivery("unsafe", True)


def test_pending_changes_are_user_scoped_and_ignore_disabled_users(push):
    p = push
    p.execute("INSERT INTO web_channels VALUES('channel','tenant','label')")
    p.execute("""INSERT INTO web_events(id,channel_id,start_date,end_date,kind,payload_blob)
        VALUES('event','channel','2026-11-05','2026-11-05','event','blob')""")
    p.execute("""INSERT INTO web_changes(id,user_id,event_id,source_hash,payload_blob,created_at)
        VALUES('change','alice','event','version','blob',1000)""")
    assert p.repo.has_pending_change("alice", "event")
    assert not p.repo.has_pending_change("bob", "event")
    p.execute("UPDATE web_users SET active=0 WHERE id='alice'")
    assert not p.repo.has_pending_change("alice", "event")
