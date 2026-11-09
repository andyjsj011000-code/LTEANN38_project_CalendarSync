"""Candidate PostgreSQL personal-task repository; not connected to the app.

The injected connection factory must provide a transactional context manager
returning a psycopg 3 connection with tuple rows. Payload encryption is supplied
by the caller. The candidate requires real PostgreSQL integration tests before use.
User-row locks serialize these operations with deactivation and bulk logout.
Logout preserves user.active: requests already authenticated before logout
cannot be rejected solely through this user-level check; session/device proof
must also be revalidated by the authenticated application boundary.
"""

from uuid import uuid4

from .calendar_rules import personal_agenda_result
from .storage import Conflict, Missing


class PostgresPersonalTasks:
    _TASK_COLUMNS = "id,payload_blob,completed,hidden,version"

    def __init__(self, connect, seal, unseal):
        self.connect, self.seal, self.unseal = connect, seal, unseal

    def _value(self, row):
        item_id, blob, completed, hidden, version = row
        return {
            **self.unseal(blob),
            "id": item_id,
            "source": "personal",
            "kind": "personal",
            "completed": bool(completed),
            "hidden": bool(hidden),
            "version": version,
            "cancelled": False,
        }

    @staticmethod
    def _task_lock_user(db, user_id):
        user = db.execute(
            "SELECT active FROM web_users WHERE id=%s FOR UPDATE", (user_id,)
        ).fetchone()
        if not user or not user[0]:
            raise Missing()

    def add_task(self, user_id, payload):
        item_id = str(uuid4())
        with self.connect() as db:
            self._task_lock_user(db, user_id)
            row = db.execute(
                f"""INSERT INTO web_personal_tasks
                (id,user_id,start_date,end_date,payload_blob) VALUES (%s,%s,%s,%s,%s)
                RETURNING {self._TASK_COLUMNS}""",
                (item_id, user_id, payload["start_date"], payload["end_date"], self.seal(payload)),
            ).fetchone()
            return self._value(row)

    def task(self, user_id, item_id):
        with self.connect() as db:
            self._task_lock_user(db, user_id)
            row = db.execute(
                f"""SELECT {self._TASK_COLUMNS} FROM web_personal_tasks
                WHERE user_id=%s AND id=%s""",
                (user_id, item_id),
            ).fetchone()
            if row is None:
                raise Missing()
            return self._value(row)

    def personal_agenda(self, user_id, tenant_id, day):
        columns = ",".join("t." + column for column in self._TASK_COLUMNS.split(","))
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT {columns} FROM web_personal_tasks t
                JOIN web_users u ON u.id=t.user_id
                WHERE t.user_id=%s AND u.tenant_id=%s AND u.active=1
                AND t.start_date<=%s AND t.end_date>=%s AND t.hidden=0""",
                (user_id, tenant_id, day, day),
            ).fetchall()
        return personal_agenda_result([self._value(row) for row in rows], day)

    @staticmethod
    def _missing_or_conflict(db, user_id, item_id):
        row = db.execute(
            "SELECT 1 FROM web_personal_tasks WHERE user_id=%s AND id=%s", (user_id, item_id)
        ).fetchone()
        if row is None:
            raise Missing()
        raise Conflict()

    def update_task(self, user_id, item_id, payload, version):
        with self.connect() as db:
            self._task_lock_user(db, user_id)
            row = db.execute(
                f"""UPDATE web_personal_tasks SET start_date=%s,end_date=%s,
                payload_blob=%s,version=version+1 WHERE user_id=%s AND id=%s AND version=%s
                RETURNING {self._TASK_COLUMNS}""",
                (
                    payload["start_date"],
                    payload["end_date"],
                    self.seal(payload),
                    user_id,
                    item_id,
                    version,
                ),
            ).fetchone()
            if row is None:
                self._missing_or_conflict(db, user_id, item_id)
            return self._value(row)

    def task_state(self, user_id, item_id, values, version):
        updates = {
            key: int(values[key])
            for key in ("hidden", "completed")
            if key in values and values[key] is not None
        }
        if not updates:
            raise ValueError("At least one state is required")
        assignments = ",".join(key + "=%s" for key in updates)
        with self.connect() as db:
            self._task_lock_user(db, user_id)
            row = db.execute(
                f"""UPDATE web_personal_tasks SET {assignments},version=version+1
                WHERE user_id=%s AND id=%s AND version=%s RETURNING {self._TASK_COLUMNS}""",
                (*updates.values(), user_id, item_id, version),
            ).fetchone()
            if row is None:
                self._missing_or_conflict(db, user_id, item_id)
            return self._value(row)

    def delete_task(self, user_id, item_id, version):
        with self.connect() as db:
            self._task_lock_user(db, user_id)
            row = db.execute(
                """DELETE FROM web_personal_tasks
                WHERE user_id=%s AND id=%s AND version=%s RETURNING id""",
                (user_id, item_id, version),
            ).fetchone()
            if row is None:
                self._missing_or_conflict(db, user_id, item_id)
            db.execute(
                """DELETE FROM web_reminder_rules
                WHERE user_id=%s AND item_id=%s AND source='personal' """,
                (user_id, item_id),
            )
