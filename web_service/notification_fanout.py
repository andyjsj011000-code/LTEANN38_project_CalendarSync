"""Opt-in SQLite per-user notification work; never enabled by the entry point.

A channel notification hands off all known user work atomically. Each child
then performs its own live delegated-access verification and exact-root read.
This avoids sharing one user's catalogue or credentials with another user.
PostgreSQL parity, reconciliation and cleanup policies are still outstanding.
"""

import secrets
import uuid

from .notification_queue import NotificationQueue, db_connection
from .notification_runtime import NotificationRuntime
from .notification_target import parse_notification_target
from .notification_worker import process_claimed
from .storage import digest


class FanoutQueue(NotificationQueue):
    def __init__(self, connect, seal, unseal, clock):
        super().__init__(connect, clock, track_targets=True)
        self.seal, self.unseal = seal, unseal
        with db_connection(connect) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS graph_user_jobs (
                id TEXT PRIMARY KEY,subscription_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,team_id TEXT NOT NULL,channel_id TEXT NOT NULL,
                targets_blob TEXT NOT NULL,lease TEXT,leased_until INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,blocked_reason TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(graph_user_jobs)")}
            if "blocked_reason" not in columns:
                raise RuntimeError("Fanout schema requires an explicit migration before reuse")

    def handoff(self, job, user_ids):
        """Persist every child before acknowledging this claimed channel revision."""
        users = sorted(set(user_ids))
        if (
            job.get("requires_reconciliation")
            or not users
            or len(users) > 1000
            or not job.get("selected_messages")
        ):
            return False
        payload = self.seal({"selected_messages": job["selected_messages"]})
        with db_connection(self.connect) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT j.lease,j.revision,s.active FROM graph_notification_jobs j
                JOIN graph_subscriptions s USING(subscription_id) WHERE j.subscription_id=?""",
                (job["subscription_id"],),
            ).fetchone()
            if (
                not current
                or current[0] != job["lease"]
                or not current[2]
                or current[1] < job["revision"]
            ):
                return False
            # A new generation is created inside this one transaction. The
            # channel row is acknowledged in the same commit; a crash cannot
            # leave acknowledged work without children. Queue revisions alone
            # are not permanent IDs because they restart after row deletion.
            generation = uuid.uuid4().hex
            inserted = 0
            for user in users:
                active = db.execute(
                    "SELECT 1 FROM web_users WHERE id=? AND tenant_id=? AND active=1",
                    (user, job["tenant_id"]),
                ).fetchone()
                if not active:
                    continue
                db.execute(
                    """INSERT INTO graph_user_jobs
                    (id,subscription_id,user_id,tenant_id,team_id,channel_id,targets_blob,created_at,leased_until)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        digest(generation + "|" + user),
                        job["subscription_id"],
                        user,
                        job["tenant_id"],
                        job["team_id"],
                        job["channel_id"],
                        payload,
                        int(self.clock()),
                        int(self.clock()),
                    ),
                )
                inserted += 1
            if not inserted:
                return False
            # Use the existing revision/lease-aware completion logic inline,
            # so new targets received during an earlier claim are preserved.
            db.execute(
                "DELETE FROM graph_notification_targets WHERE subscription_id=? AND revision<=?",
                (job["subscription_id"], job["revision"]),
            )
            removed = db.execute(
                "DELETE FROM graph_notification_jobs WHERE subscription_id=? AND lease=? AND revision=?",
                (job["subscription_id"], job["lease"], job["revision"]),
            ).rowcount
            if not removed:
                db.execute(
                    "UPDATE graph_notification_jobs SET lease=NULL,leased_until=0 WHERE subscription_id=? AND lease=?",
                    (job["subscription_id"], job["lease"]),
                )
        return True

    def claim_user(self, seconds=300):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 600:
            raise ValueError("Invalid lease")
        with db_connection(self.connect) as db:
            db.execute("BEGIN IMMEDIATE")
            while True:
                row = db.execute(
                    """SELECT id,subscription_id,user_id,tenant_id,team_id,channel_id,targets_blob
                    FROM graph_user_jobs WHERE leased_until<=? AND blocked_reason IS NULL
                    ORDER BY leased_until,created_at,id LIMIT 1""",
                    (int(self.clock()),),
                ).fetchone()
                if not row:
                    return None
                try:
                    payload = self.unseal(row[6])
                    roots = payload["selected_messages"]
                    if not isinstance(roots, list) or not 1 <= len(roots) <= 20:
                        raise ValueError("Invalid targets")
                    for root in roots:
                        if not isinstance(root, str) or parse_notification_target(
                            f"teams/{row[4]}/channels/{row[5]}/messages/{root}", row[4], row[5]
                        ) != {"message_id": root, "reply_id": None}:
                            raise ValueError("Invalid target identifier")
                except Exception:
                    # Preserve damaged work for explicit repair, and let other
                    # users proceed. Never log the credential or payload text.
                    db.execute(
                        "UPDATE graph_user_jobs SET blocked_reason='invalid_targets' WHERE id=?",
                        (row[0],),
                    )
                    continue
                lease = secrets.token_urlsafe(24)
                db.execute(
                    "UPDATE graph_user_jobs SET lease=?,leased_until=? WHERE id=?",
                    (lease, int(self.clock()) + seconds, row[0]),
                )
                return dict(
                    zip(
                        ("id", "subscription_id", "user_id", "tenant_id", "team_id", "channel_id"),
                        row[:6],
                    ),
                    selected_messages=roots,
                    lease=lease,
                )

    def user_job_counts(self):
        """Return content-free diagnostics; blocked rows need operator repair."""
        with db_connection(self.connect) as db:
            rows = db.execute(
                "SELECT blocked_reason,count(*) FROM graph_user_jobs GROUP BY blocked_reason"
            ).fetchall()
        return {
            "pending": sum(count for reason, count in rows if reason is None),
            "blocked": sum(count for reason, count in rows if reason is not None),
        }

    def finish_user(self, job, success):
        with db_connection(self.connect) as db:
            if success:
                db.execute(
                    "DELETE FROM graph_user_jobs WHERE id=? AND lease=?", (job["id"], job["lease"])
                )
            else:
                db.execute(
                    "UPDATE graph_user_jobs SET lease=NULL,leased_until=? WHERE id=? AND lease=?",
                    (int(self.clock()) + 60, job["id"], job["lease"]),
                )


class FanoutRuntime(NotificationRuntime):
    def stage(self):
        job = self.inbox.claim(seconds=300)
        if not job:
            return "empty"
        staged = False
        try:
            users = [device["user_id"] for device in self.all_candidates(job)]
            staged = self.inbox.handoff(job, users)
            return "staged" if staged else "pending"
        finally:
            if not staged:
                self.inbox.finish(job, False, retry_delay=60)

    def user_devices(self, job):
        with self.db.connect() as db:
            rows = db.execute(
                """SELECT d.id_hash FROM web_connections d JOIN web_users u ON u.id=d.user_id
                WHERE d.user_id=? AND u.tenant_id=? AND u.active=1 AND d.active=1
                AND d.background=1 AND d.expires_at>? AND d.state<>'reauth_required' ORDER BY d.id_hash""",
                (job["user_id"], job["tenant_id"], int(self.db.clock())),
            ).fetchall()
        return [device for row in rows if (device := self.db.device(row[0]))]

    def tick(self):
        try:
            stage_result = self.stage()
        except Exception:
            # Channel handoff errors must not prevent previously committed
            # child work from progressing. The channel lease will be retried.
            stage_result = "staging_failed"
        job = self.inbox.claim_user()
        if not job:
            return stage_result if stage_result != "staged" else "empty"
        result = "no_authorized_connection"
        try:
            devices = self.user_devices(job)
            if devices:
                # One delegated attempt per child lease. Repeated retries may
                # rotate devices, while every attempt rechecks Graph access.
                device = devices[(int(self.db.clock()) // 60) % len(devices)]
                result = process_claimed(
                    job, device, self.db, self.tokens, self.graph, self.source, self.access_seconds
                )
            return result
        finally:
            self.inbox.finish_user(job, result == "processed")
