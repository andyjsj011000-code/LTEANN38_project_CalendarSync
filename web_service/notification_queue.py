"""Durable, channel-level inbox for Graph notifications.

This is an isolated building block, not a webhook endpoint or a subscription
manager.  A trusted subscription manager must populate it, and consumers must
recheck channel access with the user's delegated credential before reading data.
"""
import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager


@contextmanager
def db_connection(factory):
    """Accept either sqlite3 connections or the existing Database.connect manager."""
    resource = factory()
    if isinstance(resource, sqlite3.Connection):
        try:
            with resource:
                yield resource
        finally:
            resource.close()
    else:
        with resource as connection:
            yield connection


class UnknownSubscription(Exception):
    """An early delivery may precede the subscription create response."""


class NotificationQueue:
    def __init__(self, connect, clock=time.time, *, track_targets=False):
        self.connect, self.clock = connect, clock
        self.track_targets = track_targets
        with db_connection(self.connect) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS graph_subscriptions (
                subscription_id TEXT PRIMARY KEY, state_hash BLOB NOT NULL,
                tenant_id TEXT NOT NULL, team_id TEXT NOT NULL,
                channel_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)''')
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS graph_one_active_per_channel
                ON graph_subscriptions(tenant_id,team_id,channel_id) WHERE active=1''')
            db.execute('''CREATE TABLE IF NOT EXISTS graph_notification_jobs (
                subscription_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                lease TEXT, leased_until INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(subscription_id) REFERENCES graph_subscriptions(subscription_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS graph_subscription_alerts (
                subscription_id TEXT NOT NULL, event TEXT NOT NULL, observed_at INTEGER NOT NULL,
                PRIMARY KEY(subscription_id,event))''')
            if track_targets:
                db.execute('''CREATE TABLE IF NOT EXISTS graph_notification_targets (
                    subscription_id TEXT NOT NULL REFERENCES graph_subscriptions(subscription_id),
                    message_id TEXT NOT NULL,reply_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    PRIMARY KEY(subscription_id,message_id,reply_id))''')

    @staticmethod
    def _hash(state):
        return hashlib.sha256(state.encode('utf-8')).digest()

    def register(self, subscription_id, client_state, tenant_id, team_id, channel_id):
        if not all(isinstance(x, str) and 0 < len(x) <= 256 for x in
                   (subscription_id, client_state, tenant_id, team_id, channel_id)):
            raise ValueError('Invalid subscription')
        with db_connection(self.connect) as db:
            db.execute('''INSERT INTO graph_subscriptions VALUES(?,?,?,?,?,1)
                ON CONFLICT(subscription_id) DO UPDATE SET state_hash=excluded.state_hash,
                tenant_id=excluded.tenant_id,team_id=excluded.team_id,
                channel_id=excluded.channel_id,active=1''',
                (subscription_id, self._hash(client_state), tenant_id, team_id, channel_id))

    def active_subscription(self, tenant_id, team_id, channel_id):
        with db_connection(self.connect) as db:
            row = db.execute('''SELECT subscription_id FROM graph_subscriptions
                WHERE tenant_id=? AND team_id=? AND channel_id=? AND active=1''',
                (tenant_id, team_id, channel_id)).fetchone()
            return row[0] if row else None

    def accept(self, notifications, *, defer_unknown=False):
        """Queue only known, active subscriptions with the correct clientState.

        The payload's resource and tenant fields are deliberately not trusted.
        Returns the count accepted; callers must not log notification bodies.
        """
        if not isinstance(notifications, list) or len(notifications) > 1000:
            raise ValueError('Invalid notification batch')
        accepted = 0
        with db_connection(self.connect) as db:
            for item in notifications:
                if not isinstance(item, dict):
                    continue
                sub, state = item.get('subscriptionId'), item.get('clientState')
                if not all(isinstance(x, str) and 0 < len(x) <= 256 for x in (sub, state)):
                    continue
                row = db.execute('SELECT state_hash,active FROM graph_subscriptions WHERE subscription_id=?', (sub,)).fetchone()
                if not row:
                    if defer_unknown:
                        raise UnknownSubscription('Subscription not yet registered')
                    continue
                if not row[1] or not hmac.compare_digest(row[0], self._hash(state)):
                    continue
                db.execute('''INSERT INTO graph_notification_jobs(subscription_id,revision)
                    VALUES(?,1) ON CONFLICT(subscription_id) DO UPDATE SET revision=revision+1''', (sub,))
                if self.track_targets:
                    from .notification_target import parse_notification_target
                    trusted=db.execute('SELECT team_id,channel_id FROM graph_subscriptions WHERE subscription_id=?', (sub,)).fetchone()
                    target=parse_notification_target(item.get('resource'),trusted[0],trusted[1])
                    # An unrecognised target is durable reconciliation work, never
                    # silently replaced with a bounded recent-channel scan.
                    mid=target['message_id'] if target else ''
                    reply=(target.get('reply_id') or '') if target else ''
                    revision=db.execute('SELECT revision FROM graph_notification_jobs WHERE subscription_id=?', (sub,)).fetchone()[0]
                    db.execute('''INSERT INTO graph_notification_targets VALUES(?,?,?,?)
                        ON CONFLICT(subscription_id,message_id,reply_id) DO UPDATE SET revision=excluded.revision''',
                        (sub,mid,reply,revision))
                accepted += 1
        return accepted

    def accept_lifecycle(self, notifications, *, defer_unknown=False):
        """Store supported lifecycle events; removal never silently resumes a feed."""
        if not isinstance(notifications, list) or len(notifications) > 1000:
            raise ValueError('Invalid notification batch')
        accepted = 0
        with db_connection(self.connect) as db:
            for item in notifications:
                if not isinstance(item, dict):
                    continue
                sub, state, event = item.get('subscriptionId'), item.get('clientState'), item.get('lifecycleEvent')
                if (not all(isinstance(x, str) and 0 < len(x) <= 256 for x in (sub, state)) or
                        event not in ('reauthorizationRequired', 'subscriptionRemoved')):
                    continue
                row = db.execute('SELECT state_hash FROM graph_subscriptions WHERE subscription_id=?', (sub,)).fetchone()
                if not row:
                    if defer_unknown:
                        raise UnknownSubscription('Subscription not yet registered')
                    continue
                if not hmac.compare_digest(row[0], self._hash(state)):
                    continue
                db.execute('''INSERT INTO graph_subscription_alerts VALUES(?,?,?)
                    ON CONFLICT(subscription_id,event) DO UPDATE SET observed_at=excluded.observed_at''',
                    (sub, event, int(self.clock())))
                if event == 'subscriptionRemoved':
                    db.execute('UPDATE graph_subscriptions SET active=0 WHERE subscription_id=?', (sub,))
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_channel_registry'").fetchone():
                        db.execute("UPDATE graph_channel_registry SET phase='removed',last_error='subscription_removed',updated_at=? WHERE subscription_id=?", (int(self.clock()),sub))
                accepted += 1
        return accepted

    def claim(self, seconds=120):
        if not isinstance(seconds, int) or not 1 <= seconds <= 600:
            raise ValueError('Invalid lease')
        now = int(self.clock())
        with db_connection(self.connect) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT j.subscription_id,j.revision,s.tenant_id,s.team_id,s.channel_id
                FROM graph_notification_jobs j JOIN graph_subscriptions s USING(subscription_id)
                WHERE j.leased_until<=? AND s.active=1 ORDER BY j.rowid LIMIT 1''', (now,)).fetchone()
            if not row:
                return None
            lease = secrets.token_urlsafe(24)
            db.execute('UPDATE graph_notification_jobs SET lease=?,leased_until=? WHERE subscription_id=?',
                       (lease, now+seconds, row[0]))
            job={'subscription_id': row[0], 'revision': row[1], 'tenant_id': row[2],
                 'team_id': row[3], 'channel_id': row[4], 'lease': lease}
            if self.track_targets:
                targets=db.execute('SELECT message_id,reply_id FROM graph_notification_targets WHERE subscription_id=? AND revision<=?', (row[0],row[1])).fetchall()
                job['requires_reconciliation']=not targets or any(not target[0] for target in targets)
                # Reply changes require re-reading their root with the full
                # bounded reply context in the targeted source path.
                job['selected_messages']=sorted({target[0] for target in targets if target[0]})
                if len(job['selected_messages'])>20:
                    job['requires_reconciliation']=True
            return job

    def finish(self, job, success, retry_delay=0):
        """Delete only the claimed revision; preserve a notification arriving during work."""
        if not isinstance(retry_delay,int) or not 0<=retry_delay<=3600:
            raise ValueError('Invalid retry delay')
        with db_connection(self.connect) as db:
            db.execute('BEGIN IMMEDIATE')
            if success and self.track_targets:
                current=db.execute('SELECT lease FROM graph_notification_jobs WHERE subscription_id=?', (job['subscription_id'],)).fetchone()
                if current and current[0]==job['lease']:
                    db.execute('DELETE FROM graph_notification_targets WHERE subscription_id=? AND revision<=?', (job['subscription_id'],job['revision']))
            if success:
                removed = db.execute('DELETE FROM graph_notification_jobs WHERE subscription_id=? AND lease=? AND revision=?',
                                     (job['subscription_id'], job['lease'], job['revision'])).rowcount
                if removed:
                    return
            db.execute('UPDATE graph_notification_jobs SET lease=NULL,leased_until=? WHERE subscription_id=? AND lease=?',
                       (int(self.clock())+retry_delay if not success else 0,job['subscription_id'], job['lease']))
