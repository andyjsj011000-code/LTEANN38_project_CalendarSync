"""Experimental PostgreSQL notification inbox; not wired into the server.

The connection factory must return a psycopg 3 connection used by one caller
at a time. Its context commits on success, rolls back on failure and closes it.
The schema is provisioned separately. Never pass an existing production DB to
the optional integration tests.
"""
import hashlib
import hmac
import secrets
import time

from .notification_queue import UnknownSubscription


class PostgresNotificationQueue:
    def __init__(self, connect, clock=time.time, *, track_targets=False):
        self.connect, self.clock = connect, clock
        self.track_targets = track_targets

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode('utf-8')).digest()

    def register(self, subscription_id, client_state, tenant_id, team_id, channel_id):
        values = (subscription_id, client_state, tenant_id, team_id, channel_id)
        if not all(isinstance(x, str) and 0 < len(x) <= 256 for x in values):
            raise ValueError('Invalid subscription')
        with self.connect() as db:
            db.execute('''INSERT INTO graph_subscriptions VALUES(%s,%s,%s,%s,%s,1)
                ON CONFLICT(subscription_id) DO UPDATE SET state_hash=excluded.state_hash,
                tenant_id=excluded.tenant_id,team_id=excluded.team_id,
                channel_id=excluded.channel_id,active=1''',
                (subscription_id, self._hash(client_state), tenant_id, team_id, channel_id))

    def active_subscription(self, tenant_id, team_id, channel_id):
        with self.connect() as db:
            row = db.execute('''SELECT subscription_id FROM graph_subscriptions
                WHERE tenant_id=%s AND team_id=%s AND channel_id=%s AND active=1''',
                (tenant_id, team_id, channel_id)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _batch(notifications):
        if not isinstance(notifications, list) or len(notifications) > 1000:
            raise ValueError('Invalid notification batch')

    @staticmethod
    def _ordered(notifications, lifecycle=False):
        """Lock subscriptions in a stable order, preserving each one's events."""
        valid = []
        for item in notifications:
            if not isinstance(item, dict):
                continue
            sub, state = item.get('subscriptionId'), item.get('clientState')
            if not all(isinstance(x, str) and 0 < len(x) <= 256 for x in (sub, state)):
                continue
            if lifecycle and item.get('lifecycleEvent') not in ('reauthorizationRequired', 'subscriptionRemoved'):
                continue
            valid.append(item)
        # Python's sort is stable, so events for the same subscription retain
        # their original order. Different subscriptions have no shared order.
        return sorted(valid, key=lambda item: item['subscriptionId'])

    def accept(self, notifications, *, defer_unknown=False):
        self._batch(notifications)
        accepted = 0
        with self.connect() as db:
            for item in self._ordered(notifications):
                sub, state = item.get('subscriptionId'), item.get('clientState')
                row = db.execute('SELECT state_hash,active,team_id,channel_id FROM graph_subscriptions WHERE subscription_id=%s FOR UPDATE', (sub,)).fetchone()
                if row is None:
                    if defer_unknown:
                        raise UnknownSubscription('Subscription not yet registered')
                    continue
                if not row[1] or not hmac.compare_digest(bytes(row[0]), self._hash(state)):
                    continue
                revision = db.execute('''INSERT INTO graph_notification_jobs(subscription_id,revision)
                    VALUES(%s,1) ON CONFLICT(subscription_id) DO UPDATE
                    SET revision=graph_notification_jobs.revision+1 RETURNING revision''', (sub,)).fetchone()[0]
                if self.track_targets:
                    from .notification_target import parse_notification_target
                    target = parse_notification_target(item.get('resource'), row[2], row[3])
                    message_id = target['message_id'] if target else ''
                    reply_id = (target.get('reply_id') or '') if target else ''
                    # Empty target IDs durably record unrecognised work. They
                    # must not be acknowledged by a bounded channel scan.
                    db.execute('''INSERT INTO graph_notification_targets
                        (subscription_id,message_id,reply_id,revision) VALUES(%s,%s,%s,%s)
                        ON CONFLICT(subscription_id,message_id,reply_id)
                        DO UPDATE SET revision=excluded.revision''',
                        (sub, message_id, reply_id, revision))
                accepted += 1
        return accepted

    def accept_lifecycle(self, notifications, *, defer_unknown=False):
        self._batch(notifications)
        accepted = 0
        with self.connect() as db:
            for item in self._ordered(notifications, lifecycle=True):
                sub, state, event = item.get('subscriptionId'), item.get('clientState'), item.get('lifecycleEvent')
                row = db.execute('SELECT state_hash FROM graph_subscriptions WHERE subscription_id=%s FOR UPDATE', (sub,)).fetchone()
                if row is None:
                    if defer_unknown:
                        raise UnknownSubscription('Subscription not yet registered')
                    continue
                if not hmac.compare_digest(bytes(row[0]), self._hash(state)):
                    continue
                db.execute('''INSERT INTO graph_subscription_alerts VALUES(%s,%s,%s)
                    ON CONFLICT(subscription_id,event) DO UPDATE SET observed_at=excluded.observed_at''',
                    (sub, event, int(self.clock())))
                if event == 'subscriptionRemoved':
                    db.execute('UPDATE graph_subscriptions SET active=0 WHERE subscription_id=%s', (sub,))
                    db.execute("UPDATE graph_channel_registry SET phase='removed',last_error='subscription_removed',updated_at=%s WHERE subscription_id=%s", (int(self.clock()), sub))
                accepted += 1
        return accepted

    def claim(self, seconds=120):
        if not isinstance(seconds, int) or not 1 <= seconds <= 600:
            raise ValueError('Invalid lease')
        now = int(self.clock())
        with self.connect() as db:
            row = db.execute('''SELECT j.subscription_id,j.revision,s.tenant_id,s.team_id,s.channel_id
                FROM graph_notification_jobs j JOIN graph_subscriptions s USING(subscription_id)
                WHERE j.leased_until<=%s AND s.active=1
                ORDER BY j.leased_until,j.subscription_id
                LIMIT 1 FOR UPDATE OF j SKIP LOCKED''', (now,)).fetchone()
            if not row:
                return None
            lease = secrets.token_urlsafe(24)
            db.execute('UPDATE graph_notification_jobs SET lease=%s,leased_until=%s WHERE subscription_id=%s',
                       (lease, now+seconds, row[0]))
            job = dict(zip(('subscription_id','revision','tenant_id','team_id','channel_id'), row), lease=lease)
            if self.track_targets:
                targets = db.execute('''SELECT message_id,reply_id FROM graph_notification_targets
                    WHERE subscription_id=%s AND revision<=%s''', (row[0], row[1])).fetchall()
                job['requires_reconciliation'] = not targets or any(not target[0] for target in targets)
                # A reply change requires the complete root thread. Preserve
                # reply IDs in storage while selecting each root only once.
                job['selected_messages'] = sorted({target[0] for target in targets if target[0]})
                if len(job['selected_messages']) > 20:
                    job['requires_reconciliation'] = True
            return job

    def finish(self, job, success, retry_delay=0):
        if not isinstance(retry_delay, int) or not 0 <= retry_delay <= 3600:
            raise ValueError('Invalid retry delay')
        with self.connect() as db:
            if success and self.track_targets:
                # Hold the job lock while clearing targets. A stale worker
                # must never clear targets belonging to a newer lease.
                current = db.execute('''SELECT lease FROM graph_notification_jobs
                    WHERE subscription_id=%s FOR UPDATE''', (job['subscription_id'],)).fetchone()
                if current and current[0] == job['lease']:
                    db.execute('''DELETE FROM graph_notification_targets
                        WHERE subscription_id=%s AND revision<=%s''',
                        (job['subscription_id'], job['revision']))
            if success:
                removed = db.execute('''DELETE FROM graph_notification_jobs
                    WHERE subscription_id=%s AND lease=%s AND revision=%s''',
                    (job['subscription_id'], job['lease'], job['revision'])).rowcount
                if removed:
                    return
            db.execute('''UPDATE graph_notification_jobs SET lease=NULL,leased_until=%s
                WHERE subscription_id=%s AND lease=%s''',
                (int(self.clock())+retry_delay if not success else 0, job['subscription_id'], job['lease']))
