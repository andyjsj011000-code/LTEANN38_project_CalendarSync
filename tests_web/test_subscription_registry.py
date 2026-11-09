from datetime import datetime, timezone
import sqlite3
from cryptography.fernet import Fernet
from web_service.graph_subscriptions import SubscriptionFailure
from web_service.notification_queue import NotificationQueue
from web_service.subscription_registry import SubscriptionRegistry


def setup(tmp_path):
    path = tmp_path / "subscriptions.sqlite3"
    connect = lambda: sqlite3.connect(path)
    NotificationQueue(connect)
    f = Fernet(Fernet.generate_key())

    def seal(obj):
        return f.encrypt(obj["client_state"].encode()).decode()

    def unseal(blob):
        return {"client_state": f.decrypt(blob.encode()).decode()}

    class Graph:
        calls = 0
        error = None

        def create(self, token, team, channel, state):
            self.calls += 1
            if self.error:
                raise SubscriptionFailure(self.error)
            return {"id": "subscription", "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc)}

        def renew(self, token, sub):
            return {"id": sub, "expires_at": datetime(2026, 12, 2, tzinfo=timezone.utc)}

        def delete(self, token, sub):
            if self.error:
                raise SubscriptionFailure(self.error)

    graph = Graph()
    registry = SubscriptionRegistry(
        connect, seal, unseal, graph, owner_active=lambda db, device: True, clock=lambda: 1000
    )
    device = {"tenant_id": "tenant", "id_hash": "device", "background": 1, "active": 1}
    channel = {"id": "channel", "team_id": "team"}
    return registry, graph, device, channel, connect


def test_shared_channel_only_creates_one_subscription_and_hides_state(tmp_path):
    r, g, d, ch, connect = setup(tmp_path)
    assert r.create("token", d, ch, [ch]) == {"state": "active", "created": True}
    assert r.create("token", {**d, "id_hash": "other"}, ch, [ch]) == {
        "state": "active",
        "created": False,
    }
    assert g.calls == 1
    with connect() as db:
        state = db.execute("SELECT state_blob FROM graph_channel_registry").fetchone()[0]
    assert len(state) > 32 and "token" not in state
    assert r.renew("token", {**d, "id_hash": "other"}, ch, [ch])["state"] == "owner_unavailable"
    assert r.renew("token", d, ch, [ch])["renewed"]


def test_uncertain_create_does_not_blindly_repeat(tmp_path):
    r, g, d, ch, _ = setup(tmp_path)
    g.error = "unavailable"
    assert r.create("token", d, ch, [ch])["state"] == "uncertain"
    assert r.create("token", d, ch, [ch])["state"] == "uncertain"
    assert g.calls == 1


def test_unverified_or_temporary_connection_cannot_create(tmp_path):
    import pytest

    r, g, d, ch, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        r.create("token", d, ch, [])
    with pytest.raises(ValueError):
        r.create("token", {**d, "background": 0}, ch, [ch])
    assert g.calls == 0


def test_logout_during_remote_creation_never_activates_subscription(tmp_path):
    r, g, d, ch, connect = setup(tmp_path)
    calls = iter([True, False])
    r.owner_active = lambda db, device: next(calls)
    assert r.create("token", d, ch, [ch]) == {"state": "owner_revoked", "created": False}
    assert r.get("tenant", "team", "channel")["phase"] == "owner_revoked"
    with connect() as db:
        assert db.execute("SELECT count(*) FROM graph_subscriptions").fetchone()[0] == 0


def test_retirement_blocks_silent_recreation_and_records_possible_gap(tmp_path):
    r, g, d, ch, connect = setup(tmp_path)
    r.create("token", d, ch, [ch])
    assert r.retire("token", {**d, "id_hash": "other"}, ch)["state"] == "owner_unavailable"
    g.error = "unavailable"
    assert not r.retire("token", d, ch)["retired"]
    assert r.get("tenant", "team", "channel")["phase"] == "active"
    g.error = None
    assert r.retire("token", d, ch)["state"] == "retired_needs_reconcile"
    assert (
        r.create("token", {**d, "id_hash": "other"}, ch, [ch])["state"] == "retired_needs_reconcile"
    )
    with connect() as db:
        assert db.execute("SELECT active FROM graph_subscriptions").fetchone()[0] == 0


def test_existing_web_database_context_manager_and_sqlite_rows(tmp_path):
    from web_service.test_storage import TestDatabase

    database = TestDatabase(
        tmp_path / "web.sqlite3", Fernet.generate_key().decode(), clock=lambda: 1000
    )
    inbox = NotificationQueue(database.connect, clock=database.clock)

    class Graph:
        def create(self, *args):
            return {"id": "sub", "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc)}

    registry = SubscriptionRegistry(
        database.connect,
        database.seal,
        database.unseal,
        Graph(),
        owner_active=lambda db, device: True,
        clock=database.clock,
    )
    device = {"tenant_id": "tenant", "id_hash": "device", "background": 1, "active": 1}
    channel = {"id": "channel", "team_id": "team"}
    assert registry.create("token", device, channel, [channel])["state"] == "active"
    assert inbox.active_subscription("tenant", "team", "channel") == "sub"
