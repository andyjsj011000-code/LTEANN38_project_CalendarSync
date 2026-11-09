"""Durable subscription ownership; Graph tokens are never stored here.

An authorised caller must verify the owner's access before calling create.
Uncertain create results require explicit reconciliation; no blind retry occurs.
"""

import secrets
import time

from .graph_subscriptions import SubscriptionFailure
from .notification_queue import NotificationQueue, db_connection


class SubscriptionRegistry:
    def __init__(self, connect, seal, unseal, graph, owner_active, clock=time.time):
        self.connect, self.seal, self.unseal, self.graph, self.clock = (
            connect,
            seal,
            unseal,
            graph,
            clock,
        )
        self.owner_active = owner_active
        with db_connection(connect) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS graph_channel_registry (
                tenant TEXT NOT NULL,team TEXT NOT NULL,channel TEXT NOT NULL,
                owner_device TEXT NOT NULL,state_blob TEXT NOT NULL,phase TEXT NOT NULL,
                subscription_id TEXT,expires_at INTEGER,last_error TEXT,updated_at INTEGER NOT NULL,
                PRIMARY KEY(tenant,team,channel))""")

    def get(self, tenant, team, channel):
        with db_connection(self.connect) as db:
            row = db.execute(
                """SELECT owner_device,phase,subscription_id,expires_at,last_error
                FROM graph_channel_registry WHERE tenant=? AND team=? AND channel=?""",
                (tenant, team, channel),
            ).fetchone()
        if not row:
            return None
        return dict(
            zip(("owner_device", "phase", "subscription_id", "expires_at", "last_error"), row)
        )

    def create(self, token, device, channel, allowed_channels):
        """Reserve exactly once; caller supplies a recently verified channel list."""
        tenant, owner = device["tenant_id"], device["id_hash"]
        team, ch = channel["team_id"], channel["id"]
        if (
            not device.get("background")
            or not device.get("active")
            or not any(c["id"] == ch and c["team_id"] == team for c in allowed_channels)
        ):
            raise ValueError("Active authorised background connection required")
        state = secrets.token_urlsafe(32)
        now = int(self.clock())
        with db_connection(self.connect) as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.owner_active(db, device):
                raise ValueError("Connection is no longer active")
            old = db.execute(
                "SELECT phase FROM graph_channel_registry WHERE tenant=? AND team=? AND channel=?",
                (tenant, team, ch),
            ).fetchone()
            if old:
                return {"state": old[0], "created": False}
            db.execute(
                "INSERT INTO graph_channel_registry VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    team,
                    ch,
                    owner,
                    self.seal({"client_state": state}),
                    "creating",
                    None,
                    None,
                    None,
                    now,
                ),
            )
        try:
            result = self.graph.create(token, team, ch, state)
        except SubscriptionFailure as error:
            # A timeout/5xx or conflict can hide a successful remote create.
            phase = (
                "uncertain"
                if error.reason in {"unavailable", "conflict", "invalid_response"}
                else "failed"
            )
            with db_connection(self.connect) as db:
                db.execute(
                    "UPDATE graph_channel_registry SET phase=?,last_error=?,updated_at=? WHERE tenant=? AND team=? AND channel=?",
                    (phase, error.reason, int(self.clock()), tenant, team, ch),
                )
            return {"state": phase, "created": False}
        try:
            with db_connection(self.connect) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT phase,owner_device FROM graph_channel_registry WHERE tenant=? AND team=? AND channel=?",
                    (tenant, team, ch),
                ).fetchone()
                if not row or (row[0], row[1]) != ("creating", owner):
                    raise RuntimeError("Reservation lost")
                if not self.owner_active(db, device):
                    db.execute(
                        "UPDATE graph_channel_registry SET phase='owner_revoked',subscription_id=?,expires_at=?,last_error='owner_revoked',updated_at=? WHERE tenant=? AND team=? AND channel=?",
                        (
                            result["id"],
                            int(result["expires_at"].timestamp()),
                            int(self.clock()),
                            tenant,
                            team,
                            ch,
                        ),
                    )
                    return {"state": "owner_revoked", "created": False}
                db.execute(
                    "INSERT INTO graph_subscriptions VALUES(?,?,?,?,?,1)",
                    (result["id"], NotificationQueue._hash(state), tenant, team, ch),
                )
                db.execute(
                    """UPDATE graph_channel_registry SET phase='active',subscription_id=?,expires_at=?,updated_at=?
                    WHERE tenant=? AND team=? AND channel=?""",
                    (
                        result["id"],
                        int(result["expires_at"].timestamp()),
                        int(self.clock()),
                        tenant,
                        team,
                        ch,
                    ),
                )
        except Exception:
            # Keep the durable creating state. Never issue another create automatically.
            raise
        return {"state": "active", "created": True}

    def renew(self, token, device, channel, allowed_channels):
        tenant, owner = device["tenant_id"], device["id_hash"]
        team, ch = channel["team_id"], channel["id"]
        if (
            not device.get("background")
            or not device.get("active")
            or not any(c["id"] == ch and c["team_id"] == team for c in allowed_channels)
        ):
            raise ValueError("Active authorised background connection required")
        current = self.get(tenant, team, ch)
        if not current or current["phase"] != "active" or current["owner_device"] != owner:
            return {"state": "owner_unavailable", "renewed": False}
        with db_connection(self.connect) as db:
            if not self.owner_active(db, device):
                return {"state": "owner_unavailable", "renewed": False}
        try:
            result = self.graph.renew(token, current["subscription_id"])
        except SubscriptionFailure as error:
            with db_connection(self.connect) as db:
                db.execute(
                    "UPDATE graph_channel_registry SET last_error=?,updated_at=? WHERE tenant=? AND team=? AND channel=?",
                    (error.reason, int(self.clock()), tenant, team, ch),
                )
            return {"state": error.reason, "renewed": False}
        if result["id"] != current["subscription_id"]:
            return {"state": "invalid_response", "renewed": False}
        with db_connection(self.connect) as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.owner_active(db, device):
                return {"state": "owner_unavailable", "renewed": False}
            changed = db.execute(
                """UPDATE graph_channel_registry SET expires_at=?,last_error=NULL,updated_at=?
                WHERE tenant=? AND team=? AND channel=? AND phase='active' AND owner_device=? AND subscription_id=?""",
                (
                    int(result["expires_at"].timestamp()),
                    int(self.clock()),
                    tenant,
                    team,
                    ch,
                    owner,
                    result["id"],
                ),
            ).rowcount
        return {"state": "active" if changed else "changed", "renewed": bool(changed)}

    def retire(self, token, device, channel):
        """Delete with the original owner; never recreate without gap recovery."""
        tenant, owner = device["tenant_id"], device["id_hash"]
        team, ch = channel["team_id"], channel["id"]
        current = self.get(tenant, team, ch)
        if not current or current["phase"] != "active" or current["owner_device"] != owner:
            return {"state": "owner_unavailable", "retired": False}
        with db_connection(self.connect) as db:
            if not self.owner_active(db, device):
                return {"state": "owner_unavailable", "retired": False}
        try:
            self.graph.delete(token, current["subscription_id"])
        except SubscriptionFailure as error:
            # An unavailable response is ambiguous; leave the subscription registered
            # and do not let another owner create a duplicate.
            return {"state": error.reason, "retired": False}
        with db_connection(self.connect) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE graph_channel_registry SET phase='retired_needs_reconcile',
                last_error='gap_possible',updated_at=? WHERE tenant=? AND team=? AND channel=?
                AND phase='active' AND owner_device=? AND subscription_id=?""",
                (int(self.clock()), tenant, team, ch, owner, current["subscription_id"]),
            ).rowcount
            if changed:
                db.execute(
                    "UPDATE graph_subscriptions SET active=0 WHERE subscription_id=?",
                    (current["subscription_id"],),
                )
        return {
            "state": "retired_needs_reconcile" if changed else "changed",
            "retired": bool(changed),
        }
