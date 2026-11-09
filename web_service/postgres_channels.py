"""Unwired PostgreSQL candidate for channel and user preferences.

The connection factory supplies a psycopg 3 transactional context manager with
tuple rows. Channel access records are time-limited hints; live Graph access
must still be revalidated by the caller. This is not a complete storage adapter.
"""

import time

from .postgres_preferences import PostgresPreferences
from .storage import Missing, PermissionPending


class PostgresChannels(PostgresPreferences):
    def __init__(self, connect, seal, unseal, clock=time.time):
        super().__init__(connect, seal, unseal)
        self.clock = clock

    def channels(self, user_id, tenant_id):
        with self.connect() as db:
            rows = db.execute(
                """SELECT ch.id,ch.label_blob,COALESCE(p.enabled,1)
                FROM web_channels ch JOIN web_channel_access a ON a.channel_id=ch.id
                JOIN web_users u ON u.id=a.user_id AND u.tenant_id=ch.tenant_id AND u.active=1
                LEFT JOIN web_channel_preferences p ON p.channel_id=ch.id AND p.user_id=a.user_id
                WHERE a.user_id=%s AND ch.tenant_id=%s AND a.valid_until>%s ORDER BY ch.id""",
                (user_id, tenant_id, int(self.clock())),
            ).fetchall()
        return [
            {"id": row[0], "name": self.unseal(row[1])["name"], "enabled": bool(row[2])}
            for row in rows
        ]

    def channel_preference(self, user_id, tenant_id, channel_id, enabled):
        with self.connect() as db:
            user = db.execute(
                "SELECT tenant_id,active FROM web_users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if not user or user[0] != tenant_id or not user[1]:
                raise Missing()
            # Lock the proof until the update commits, so concurrent revocation
            # cannot delete the row between the permission check and the write.
            allowed = db.execute(
                """SELECT a.valid_until FROM web_channel_access a
                JOIN web_channels ch ON ch.id=a.channel_id
                WHERE a.user_id=%s AND ch.tenant_id=%s AND ch.id=%s AND a.valid_until>%s
                FOR UPDATE OF a""",
                (user_id, tenant_id, channel_id, int(self.clock())),
            ).fetchone()
            # A row lock can wait; a proof that expired during the wait is stale.
            if allowed is None:
                previous = db.execute(
                    """SELECT a.valid_until FROM web_channel_access a
                    JOIN web_channels ch ON ch.id=a.channel_id
                    WHERE a.user_id=%s AND ch.tenant_id=%s AND ch.id=%s FOR UPDATE OF a""",
                    (user_id, tenant_id, channel_id),
                ).fetchone()
                if previous is not None and previous[0] <= int(self.clock()):
                    raise PermissionPending()
                raise Missing()
            if allowed[0] <= int(self.clock()):
                raise PermissionPending()
            db.execute(
                """INSERT INTO web_channel_preferences(user_id,channel_id,enabled)
                VALUES(%s,%s,%s) ON CONFLICT(user_id,channel_id)
                DO UPDATE SET enabled=excluded.enabled""",
                (user_id, channel_id, int(enabled)),
            )
