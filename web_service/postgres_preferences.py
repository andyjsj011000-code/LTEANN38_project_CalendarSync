"""Unwired PostgreSQL candidate for encrypted user preference persistence.

The caller must authenticate the user. Reminder writes recheck the target in
the same transaction; the full event facade supplies Teams permission locks.
Connections must be psycopg 3 transactional context managers with tuple rows.
This module does not initialize, migrate, or connect to any database by itself.
The user-row lock serializes writes with deactivation and bulk logout, but
logout keeps the user active. The caller must revalidate the session/device
proof to reject already-started requests that outlive logout.
"""

from .schemas import Preferences
from .storage import Missing, PermissionPending


class PostgresPreferences:
    def __init__(self, connect, seal, unseal):
        self.connect, self.seal, self.unseal = connect, seal, unseal

    @staticmethod
    def _preference_lock_user(db, user_id):
        user = db.execute(
            "SELECT active FROM web_users WHERE id=%s FOR UPDATE", (user_id,)
        ).fetchone()
        if not user or not user[0]:
            raise Missing()

    def preferences(self, user_id):
        with self.connect() as db:
            self._preference_lock_user(db, user_id)
            row = db.execute(
                "SELECT payload_blob FROM web_preferences WHERE user_id=%s", (user_id,)
            ).fetchone()
        return self.unseal(row[0]) if row else Preferences().model_dump()

    def save_preferences(self, user_id, values):
        blob = self.seal(values)
        with self.connect() as db:
            self._preference_lock_user(db, user_id)
            db.execute(
                """INSERT INTO web_preferences(user_id,payload_blob) VALUES(%s,%s)
                ON CONFLICT(user_id) DO UPDATE SET payload_blob=excluded.payload_blob""",
                (user_id, blob),
            )
        return values

    def rules(self, user_id, item_id, source):
        with self.connect() as db:
            self._preference_lock_user(db, user_id)
            row = db.execute(
                """SELECT payload_blob FROM web_reminder_rules
                WHERE user_id=%s AND item_id=%s AND source=%s""",
                (user_id, item_id, source),
            ).fetchone()
        return self.unseal(row[0]) if row else {"offsets": [-1440, -60], "custom_times": []}

    def save_rules(self, user_id, item_id, source, values):
        if source not in {"personal", "teams"}:
            raise Missing()
        blob = self.seal(values)
        with self.connect() as db:
            self._preference_lock_user(db, user_id)
            expiry = None
            if source == "personal":
                # Serialize with deletion of this user's own task. A separate
                # earlier API lookup is not a proof that it still exists.
                item = db.execute(
                    """SELECT id FROM web_personal_tasks
                    WHERE user_id=%s AND id=%s FOR UPDATE""",
                    (user_id, item_id),
                ).fetchone()
                if item is None:
                    raise Missing()
            else:
                lock_access = getattr(self, "_lock_event_access", None)
                tenant = getattr(self, "tenant_id", None)
                if not callable(lock_access) or not tenant:
                    raise ValueError("Teams reminders require an authenticated event repository")
                expiry = lock_access(db, user_id, tenant, item_id)
            db.execute(
                """INSERT INTO web_reminder_rules(user_id,item_id,source,payload_blob)
                VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,item_id,source)
                DO UPDATE SET payload_blob=excluded.payload_blob""",
                (user_id, item_id, source, blob),
            )
            # A database wait can outlive the ACL even though the locked user
            # and item cannot change. Roll the write back rather than save a
            # rule under an expired permission proof.
            if expiry is not None and expiry <= int(self.clock()):
                raise PermissionPending()
