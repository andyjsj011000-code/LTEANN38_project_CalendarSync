"""Unwired PostgreSQL candidate for encrypted device credentials.

Factories supply psycopg 3 transactional context managers with tuple rows.
All mutations lock the user row before the device row. Future deactivation,
sync commit and bulk logout paths must share that protocol before integration.
This module neither initializes a schema nor connects to any live database.
"""

import secrets
import time

from .storage import Missing, digest


class PostgresDevices:
    _DEVICE_FIELDS = (
        "id_hash",
        "user_id",
        "credential_blob",
        "token_expires",
        "expires_at",
        "background",
        "active",
        "version",
        "refresh_lease",
        "refresh_until",
        "next_sync",
        "ingest_at",
        "state",
    )
    _DEVICE_COLUMNS = ",".join("d." + field for field in _DEVICE_FIELDS)

    def __init__(self, connect, seal, unseal, clock=time.time):
        self.connect, self.seal, self.unseal, self.clock = connect, seal, unseal, clock

    @staticmethod
    def _lifetime(value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("Credential lifetime must be positive seconds")

    def _lock_device(self, db, device_hash, user_id=None):
        # Device ownership is immutable. Locking users first also serializes
        # with session issuance and future user-deactivation implementations.
        owner = db.execute(
            "SELECT user_id FROM web_connections WHERE id_hash=%s", (device_hash,)
        ).fetchone()
        if owner is None or (user_id is not None and owner[0] != user_id):
            return None
        user = db.execute(
            "SELECT active FROM web_users WHERE id=%s FOR UPDATE", (owner[0],)
        ).fetchone()
        if user is None:
            return None
        row = db.execute(
            f"""SELECT {self._DEVICE_COLUMNS} FROM web_connections d
            WHERE d.id_hash=%s AND d.user_id=%s FOR UPDATE OF d""",
            (device_hash, owner[0]),
        ).fetchone()
        return (
            {**dict(zip(self._DEVICE_FIELDS, row)), "user_active": bool(user[0])} if row else None
        )

    def add_connection(
        self,
        raw_session,
        user_id,
        token,
        lifetime,
        cache="",
        background=False,
        *,
        assignment_access=False,
    ):
        self._lifetime(lifetime)
        raw = secrets.token_urlsafe(32)
        with self.connect() as db:
            user = db.execute(
                "SELECT active FROM web_users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            session = db.execute(
                "SELECT user_id,expires_at FROM web_sessions WHERE id_hash=%s FOR UPDATE",
                (digest(raw_session),),
            ).fetchone()
            now = int(self.clock())
            if not user or not user[0] or not session or session[0] != user_id or session[1] <= now:
                raise Missing()
            persistent = bool(background and cache)
            expires = now + (30 * 86400 if persistent else min(lifetime, 3600))
            db.execute(
                """INSERT INTO web_connections
                (id_hash,user_id,credential_blob,token_expires,expires_at,background)
                VALUES(%s,%s,%s,%s,%s,%s)""",
                (
                    digest(raw),
                    user_id,
                    self.seal(
                        {
                            "token": token,
                            "cache": cache,
                            **({"assignment_access": True} if assignment_access else {}),
                        }
                    ),
                    now + lifetime,
                    expires,
                    int(persistent),
                ),
            )
            db.execute(
                "INSERT INTO web_session_links VALUES(%s,%s,%s)",
                (digest(raw_session), digest(raw), now),
            )
        return raw, expires - now

    def device(self, device_hash):
        with self.connect() as db:
            row = db.execute(
                f"""SELECT {self._DEVICE_COLUMNS},u.tenant_id,u.object_id
                FROM web_connections d JOIN web_users u ON u.id=d.user_id
                WHERE d.id_hash=%s AND d.active=1 AND d.expires_at>%s AND u.active=1""",
                (device_hash, int(self.clock())),
            ).fetchone()
        return dict(zip(self._DEVICE_FIELDS + ("tenant_id", "object_id"), row)) if row else None

    def session_device(self, session):
        with self.connect() as db:
            row = db.execute(
                f"""SELECT {self._DEVICE_COLUMNS},u.tenant_id,u.object_id
                FROM web_session_links l JOIN web_sessions s ON s.id_hash=l.session_hash
                JOIN web_connections d ON d.id_hash=l.device_hash JOIN web_users u ON u.id=d.user_id
                WHERE s.id_hash=%s AND s.user_id=%s AND d.user_id=s.user_id
                AND s.expires_at>%s AND d.active=1 AND d.expires_at>%s AND u.active=1""",
                (session["id_hash"], session["user_id"], int(self.clock()), int(self.clock())),
            ).fetchone()
        if row is None:
            raise Missing()
        return dict(zip(self._DEVICE_FIELDS + ("tenant_id", "object_id"), row))

    def school_session(self, raw, idle_seconds):
        """Revalidate and touch a viewing session without extending its device.

        The initial lookup identifies lock targets only. Every mutable proof is
        reread under user -> device -> session -> link locks, and the clock is
        sampled afterwards so a wait cannot turn an expired proof into access.
        Session expiry deletes only the viewing session; background credentials
        remain associated with the device until explicit revocation or expiry.
        """
        self._lifetime(idle_seconds)
        if not isinstance(raw, str) or not 0 < len(raw) <= 128:
            return None
        session_hash = digest(raw)
        result = None
        with self.connect() as db:
            identity = db.execute(
                """SELECT s.user_id,l.device_hash FROM web_sessions s
                LEFT JOIN web_session_links l ON l.session_hash=s.id_hash
                WHERE s.id_hash=%s""",
                (session_hash,),
            ).fetchone()
            if identity is None:
                return None
            user_id, device_hash = identity
            user = db.execute(
                """SELECT active,tenant_id,object_id,profile_blob FROM web_users
                WHERE id=%s FOR UPDATE""",
                (user_id,),
            ).fetchone()
            device = (
                db.execute(
                    """SELECT user_id,active,expires_at,state FROM web_connections
                WHERE id_hash=%s AND user_id=%s FOR UPDATE""",
                    (device_hash, user_id),
                ).fetchone()
                if device_hash is not None
                else None
            )
            session = db.execute(
                """SELECT user_id,csrf_token,expires_at FROM web_sessions
                WHERE id_hash=%s FOR UPDATE""",
                (session_hash,),
            ).fetchone()
            link = db.execute(
                """SELECT device_hash,last_seen FROM web_session_links
                WHERE session_hash=%s FOR UPDATE""",
                (session_hash,),
            ).fetchone()
            now = int(self.clock())
            if (
                not user
                or not user[0]
                or not device
                or device[0] != user_id
                or not device[1]
                or device[2] <= now
                or device[3] == "reauth_required"
                or not session
                or session[0] != user_id
                or session[2] <= now
                or not link
                or link[0] != device_hash
                or link[1] + idle_seconds <= now
            ):
                # Keep this inside the transaction and return normally so the
                # deletion commits instead of being undone by an exception.
                db.execute(
                    "DELETE FROM web_sessions WHERE id_hash=%s AND user_id=%s",
                    (session_hash, user_id),
                )
            else:
                db.execute(
                    "UPDATE web_session_links SET last_seen=%s WHERE session_hash=%s",
                    (now, session_hash),
                )
                result = dict(
                    zip(
                        (
                            "id_hash",
                            "user_id",
                            "csrf_token",
                            "expires_at",
                            "tenant_id",
                            "object_id",
                            "profile_blob",
                        ),
                        (session_hash, user_id, session[1], session[2], *user[1:]),
                    )
                )
        return result

    def lock_refresh(self, device_hash):
        with self.connect() as db:
            device = self._lock_device(db, device_hash)
            now = int(self.clock())
            if (
                not device
                or not device["user_active"]
                or not device["active"]
                or device["expires_at"] <= now
                or device["refresh_until"] > now
                or device["state"] == "reauth_required"
            ):
                return None
            lease = secrets.token_hex(16)
            db.execute(
                "UPDATE web_connections SET refresh_lease=%s,refresh_until=%s WHERE id_hash=%s",
                (lease, now + 30, device_hash),
            )
        return lease

    def save_refresh(self, device_hash, lease, token, lifetime, cache):
        self._lifetime(lifetime)
        with self.connect() as db:
            device = self._lock_device(db, device_hash)
            now = int(self.clock())
            if (
                not device
                or not device["user_active"]
                or not device["active"]
                or device["expires_at"] <= now
                or device["refresh_lease"] != lease
                or device["refresh_until"] <= now
                or device["state"] == "reauth_required"
            ):
                return False
            db.execute(
                """UPDATE web_connections SET credential_blob=%s,token_expires=%s,
                refresh_lease=NULL,refresh_until=0,version=version+1,state='connected'
                WHERE id_hash=%s""",
                (
                    self.seal(
                        {
                            "token": token,
                            "cache": cache,
                            **(
                                {"assignment_access": True}
                                if self.unseal(device["credential_blob"]).get("assignment_access")
                                else {}
                            ),
                        }
                    ),
                    now + lifetime,
                    device_hash,
                ),
            )
        return True

    def fail_connection(self, device_hash, lease=None):
        with self.connect() as db:
            device = self._lock_device(db, device_hash)
            if not device or (
                lease is not None
                and (
                    device["refresh_lease"] != lease or device["refresh_until"] <= int(self.clock())
                )
            ):
                return
            db.execute(
                """UPDATE web_connections SET credential_blob=%s,token_expires=0,
                refresh_lease=NULL,refresh_until=0,version=version+1,state='reauth_required' WHERE id_hash=%s""",
                (self.seal({}), device_hash),
            )
            self._invalidate_device_access(db, device["user_id"])
            db.execute(
                """DELETE FROM web_sessions WHERE id_hash IN
                (SELECT session_hash FROM web_session_links WHERE device_hash=%s)""",
                (device_hash,),
            )

    def revoke_device(self, device_hash, user_id=None):
        with self.connect() as db:
            device = self._lock_device(db, device_hash, user_id)
            if device is None:
                return
            db.execute("DELETE FROM web_push_subscriptions WHERE device_hash=%s", (device_hash,))
            db.execute(
                """DELETE FROM web_sessions WHERE id_hash IN
                (SELECT session_hash FROM web_session_links WHERE device_hash=%s)""",
                (device_hash,),
            )
            db.execute(
                """UPDATE web_connections SET active=0,credential_blob=%s,token_expires=0,
                refresh_lease=NULL,refresh_until=0,version=version+1 WHERE id_hash=%s""",
                (self.seal({}), device_hash),
            )
            self._invalidate_device_access(db, device["user_id"])

    @staticmethod
    def _invalidate_device_access(db, user_id):
        db.execute("DELETE FROM web_channel_access WHERE user_id=%s", (user_id,))
        # Other devices must renew the removed proof immediately. Preserve the
        # current lease: it may belong to another still-authorized connection.
        db.execute("UPDATE web_school_sync SET checked_at=0 WHERE user_id=%s", (user_id,))

    def revoke_all(self, user_id):
        """Revoke existing connections atomically, serialized with session issue.

        The user remains active: a fresh login ordered after this transaction
        may create a new connection. Existing sessions cannot create a device
        after revocation because add_connection locks this same user first.
        """
        empty_credentials = self.seal({})
        with self.connect() as db:
            user = db.execute(
                "SELECT id FROM web_users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if user is None:
                return
            # Lock every device in deterministic order before deleting any
            # linked sessions/subscriptions or clearing credential versions.
            db.execute(
                """SELECT id_hash FROM web_connections WHERE user_id=%s
                ORDER BY id_hash FOR UPDATE""",
                (user_id,),
            ).fetchall()
            db.execute(
                """DELETE FROM web_push_subscriptions WHERE device_hash IN
                (SELECT id_hash FROM web_connections WHERE user_id=%s)""",
                (user_id,),
            )
            db.execute("DELETE FROM web_sessions WHERE user_id=%s", (user_id,))
            db.execute(
                """UPDATE web_connections SET active=0,credential_blob=%s,
                token_expires=0,refresh_lease=NULL,refresh_until=0,version=version+1
                WHERE user_id=%s""",
                (empty_credentials, user_id),
            )
            db.execute("DELETE FROM web_channel_access WHERE user_id=%s", (user_id,))
            db.execute("DELETE FROM web_school_sync WHERE user_id=%s", (user_id,))
