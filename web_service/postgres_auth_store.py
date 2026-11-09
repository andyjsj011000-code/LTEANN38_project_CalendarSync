"""Unwired PostgreSQL candidate for login flow and session persistence.

The factory must return a psycopg 3 transactional context manager with tuple
rows. Encryption and a stable key are supplied by the caller. This module does
not initialize or migrate a database and is not ready for deployment.
"""

import secrets
import time

from .storage import Missing, digest


class PostgresAuthStore:
    def __init__(self, connect, seal, unseal, clock=time.time):
        self.connect, self.seal, self.unseal, self.clock = connect, seal, unseal, clock

    def save_user(self, tenant_id, object_id, profile):
        user_id = f"{tenant_id}:{object_id}"
        with self.connect() as db:
            db.execute(
                """INSERT INTO web_users(id,tenant_id,object_id,profile_blob)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET profile_blob=excluded.profile_blob""",
                (user_id, tenant_id, object_id, self.seal(profile)),
            )
        return user_id

    def store_flow(self, flow):
        raw = secrets.token_urlsafe(32)
        now = int(self.clock())
        with self.connect() as db:
            db.execute("DELETE FROM web_auth_flows WHERE expires_at<=%s", (now,))
            db.execute(
                "INSERT INTO web_auth_flows VALUES(%s,%s,%s)",
                (digest(raw), self.seal(flow), now + 600),
            )
        return raw

    def consume_flow(self, raw):
        if not isinstance(raw, str) or not 0 < len(raw) <= 128:
            raise Missing()
        # DELETE RETURNING gives exactly one winner when two callbacks race.
        # Commit even when the flow has expired, so it cannot be replayed.
        with self.connect() as db:
            row = db.execute(
                """DELETE FROM web_auth_flows WHERE id_hash=%s
                RETURNING flow_blob,expires_at""",
                (digest(raw),),
            ).fetchone()
        if row is None or row[1] <= self.clock():
            raise Missing()
        return self.unseal(row[0])

    def issue_session(self, user_id, lifetime):
        if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
            raise ValueError("Session lifetime must be a positive number of seconds")
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(self.clock())
        with self.connect() as db:
            # User deactivation must lock this same row; device/logout revocation
            # requires its own shared locking protocol, not yet implemented here.
            user = db.execute(
                "SELECT active FROM web_users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if not user or not user[0]:
                raise Missing()
            db.execute("DELETE FROM web_sessions WHERE expires_at<=%s", (now,))
            db.execute(
                "INSERT INTO web_sessions VALUES(%s,%s,%s,%s)",
                (digest(raw), user_id, csrf, now + lifetime),
            )
        return raw

    def session(self, raw):
        if not isinstance(raw, str) or not 0 < len(raw) <= 128:
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT s.id_hash,s.user_id,s.csrf_token,s.expires_at,
                    u.tenant_id,u.object_id,u.profile_blob
                FROM web_sessions s JOIN web_users u ON u.id=s.user_id
                WHERE s.id_hash=%s AND s.expires_at>%s AND u.active=1""",
                (digest(raw), int(self.clock())),
            ).fetchone()
        keys = (
            "id_hash",
            "user_id",
            "csrf_token",
            "expires_at",
            "tenant_id",
            "object_id",
            "profile_blob",
        )
        return dict(zip(keys, row)) if row else None

    def revoke_session(self, raw):
        if not isinstance(raw, str) or not 0 < len(raw) <= 128:
            return
        with self.connect() as db:
            db.execute("DELETE FROM web_sessions WHERE id_hash=%s", (digest(raw),))
