"""Unwired PostgreSQL event, calendar and source-review repository.

Factories must provide transactional psycopg 3 context managers with tuple
rows. Mutations lock user -> access proof -> event -> review/state. Callers
must authenticate the user; cached ACLs are short-lived permission proofs.
Known Oracle recovery additionally requires a freshly Graph-verified channel
list and deliberately does not use ACLs cleared by begin_sync. This module
does not initialize a schema, connect to a live DB, or modify the legacy app.
"""

import time
from datetime import datetime, timedelta, timezone

from .calendar_rules import agenda_result, calendar_result
from .postgres_channels import PostgresChannels
from .postgres_personal_tasks import PostgresPersonalTasks
from .schemas import Preferences
from .source_updates import SOURCE_EVENT_FIELDS as _SOURCE_FIELDS
from .source_updates import (
    source_display_fields,
    validate_event_override,
    validate_override_request,
)
from .storage import Missing, PermissionPending


class PostgresEvents(PostgresPersonalTasks, PostgresChannels):
    _EVENT_COLUMNS = """e.id,e.channel_id,e.start_date,e.end_date,e.kind,
        e.payload_blob,e.cancelled,e.revision,ch.label_blob,
        COALESCE(s.hidden,0),COALESCE(s.completed,0),COALESCE(p.enabled,1),s.override_blob"""
    _EVENT_FROM = """FROM web_events e JOIN web_channels ch ON ch.id=e.channel_id
        JOIN web_channel_access a ON a.channel_id=e.channel_id
        JOIN web_users u ON u.id=a.user_id
        LEFT JOIN web_event_states s ON s.event_id=e.id AND s.user_id=a.user_id
        LEFT JOIN web_channel_preferences p ON p.channel_id=e.channel_id AND p.user_id=a.user_id"""

    def __init__(self, connect, seal, unseal, clock=time.time):
        self.connect, self.seal, self.unseal, self.clock = connect, seal, unseal, clock

    def _source_fields(self, blob):
        return source_display_fields(self.unseal(blob))

    def _event_value(self, row):
        fields = self._source_fields(row[5])
        result = {
            **fields,
            "id": row[0],
            "source": "teams",
            "channel_id": row[1],
            "channel": self.unseal(row[8])["name"],
            "start_date": row[2],
            "end_date": row[3],
            "kind": row[4],
            "cancelled": bool(row[6]),
            "revision": row[7],
            "hidden": bool(row[9]),
            "completed": bool(row[10]),
            "channel_enabled": bool(row[11]),
        }
        if row[12] is not None:
            result.update(self._source_fields(row[12]))
        return result

    def _shared_in(self, db, user_id, tenant_id, event_id=None):
        params = [user_id, tenant_id, int(self.clock())]
        extra = ""
        if event_id is not None:
            extra = " AND e.id=%s"
            params.append(event_id)
        rows = db.execute(
            f"""SELECT {self._EVENT_COLUMNS} {self._EVENT_FROM}
            WHERE a.user_id=%s AND u.tenant_id=%s AND ch.tenant_id=u.tenant_id
            AND u.active=1 AND a.valid_until>%s{extra} ORDER BY e.id""",
            tuple(params),
        ).fetchall()
        return [self._event_value(row) for row in rows]

    def _shared(self, user_id, tenant_id):
        with self.connect() as db:
            return self._shared_in(db, user_id, tenant_id)

    @staticmethod
    def _lock_user(db, user_id, tenant_id):
        user = db.execute(
            "SELECT tenant_id,active FROM web_users WHERE id=%s FOR UPDATE", (user_id,)
        ).fetchone()
        if user is None or user[0] != tenant_id or not user[1]:
            raise Missing()

    def _lock_event_access(self, db, user_id, tenant_id, event_id):
        self._lock_user(db, user_id, tenant_id)
        proof = db.execute(
            """SELECT a.channel_id,a.valid_until
            FROM web_channel_access a JOIN web_events e ON e.channel_id=a.channel_id
            JOIN web_channels ch ON ch.id=a.channel_id
            WHERE a.user_id=%s AND ch.tenant_id=%s AND e.id=%s AND a.valid_until>%s
            FOR UPDATE OF a""",
            (user_id, tenant_id, event_id, int(self.clock())),
        ).fetchone()
        if proof is None:
            # An expired proof for this user's own previously accessible item
            # is a renewal condition, not evidence that the item disappeared.
            previous = db.execute(
                """SELECT a.valid_until FROM web_channel_access a
                JOIN web_events e ON e.channel_id=a.channel_id
                JOIN web_channels ch ON ch.id=a.channel_id
                WHERE a.user_id=%s AND ch.tenant_id=%s AND e.id=%s FOR UPDATE OF a""",
                (user_id, tenant_id, event_id),
            ).fetchone()
            if previous is not None and previous[0] <= int(self.clock()):
                raise PermissionPending()
            raise Missing()
        event = db.execute(
            "SELECT channel_id FROM web_events WHERE id=%s FOR UPDATE", (event_id,)
        ).fetchone()
        # Lock waits can outlive the proof. Recheck after acquiring both rows.
        if event is None or event[0] != proof[0]:
            raise Missing()
        if proof[1] <= int(self.clock()):
            raise PermissionPending()
        return proof[1]

    def event_state(self, user_id, tenant_id, event_id, values):
        if any(
            not isinstance(value, bool)
            for key, value in values.items()
            if key in {"hidden", "completed"} and value is not None
        ):
            raise ValueError("Event states must be boolean")
        updates = {
            key: int(values[key])
            for key in ("hidden", "completed")
            if key in values and values[key] is not None
        }
        if not updates:
            raise ValueError("At least one state is required")
        assignments = ",".join(key + "=%s" for key in updates)
        with self.connect() as db:
            expiry = self._lock_event_access(db, user_id, tenant_id, event_id)
            db.execute(
                """INSERT INTO web_event_states(user_id,event_id) VALUES(%s,%s)
                ON CONFLICT(user_id,event_id) DO NOTHING""",
                (user_id, event_id),
            )
            db.execute(
                f"""UPDATE web_event_states SET {assignments}
                WHERE user_id=%s AND event_id=%s""",
                (*updates.values(), user_id, event_id),
            )
            rows = self._shared_in(db, user_id, tenant_id, event_id)
            if not rows:
                if expiry <= int(self.clock()):
                    raise PermissionPending()
                raise Missing()
            return rows[0]

    @staticmethod
    def _validate_override(fields):
        validate_event_override(fields)

    def override_event(self, user_id, tenant_id, event_id, values):
        """Apply allowed per-user field changes; immutable identities are rejected."""
        validate_override_request(values)
        with self.connect() as db:
            expiry = self._lock_event_access(db, user_id, tenant_id, event_id)
            rows = self._shared_in(db, user_id, tenant_id, event_id)
            if not rows:
                if expiry <= int(self.clock()):
                    raise PermissionPending()
                raise Missing()
            (current,) = rows
            fields = {key: value for key, value in current.items() if key in _SOURCE_FIELDS}
            fields.update(values)
            self._validate_override(fields)
            blob = self.seal(fields)
            if expiry <= int(self.clock()):
                raise PermissionPending()
            db.execute(
                """INSERT INTO web_event_states(user_id,event_id,override_blob)
                VALUES(%s,%s,%s) ON CONFLICT(user_id,event_id)
                DO UPDATE SET override_blob=excluded.override_blob""",
                (user_id, event_id, blob),
            )

    def calendar(self, user_id, tenant_id, start, end, hidden_only=False):
        with self.connect() as db:
            # Personal tasks must also disappear for an inactive or wrong-tenant user.
            alive = db.execute(
                "SELECT 1 FROM web_users WHERE id=%s AND tenant_id=%s AND active=1",
                (user_id, tenant_id),
            ).fetchone()
            if alive is None:
                return []
            pref = db.execute(
                "SELECT payload_blob FROM web_preferences WHERE user_id=%s", (user_id,)
            ).fetchone()
            prefs = self.unseal(pref[0]) if pref else Preferences().model_dump()
            columns = ",".join(
                "t." + column for column in PostgresPersonalTasks._TASK_COLUMNS.split(",")
            )
            rows = db.execute(
                f"""SELECT {columns} FROM web_personal_tasks t
                JOIN web_users u ON u.id=t.user_id WHERE t.user_id=%s AND u.tenant_id=%s
                AND u.active=1 AND t.start_date<=%s AND t.end_date>=%s""",
                (user_id, tenant_id, end, start),
            ).fetchall()
            personal = [PostgresPersonalTasks._value(self, row) for row in rows]
            teams = self._shared_in(db, user_id, tenant_id)
        return calendar_result(personal, teams, prefs, start, end, hidden_only)

    def agenda(self, user_id, tenant_id, day):
        return agenda_result(self.calendar(user_id, tenant_id, day, day), day)

    def changes(self, user_id, tenant_id):
        with self.connect() as db:
            accessible = {event["id"]: event for event in self._shared_in(db, user_id, tenant_id)}
            rows = db.execute(
                """SELECT id,event_id,payload_blob,created_at FROM web_changes
                WHERE user_id=%s AND status='pending' ORDER BY created_at DESC,id""",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "event_id": row[1],
                "before": accessible[row[1]],
                "after": self._source_fields(row[2]),
                "created_at": row[3],
            }
            for row in rows
            if row[1] in accessible
        ]

    def resolve_change(self, user_id, tenant_id, change_id, accept):
        from .storage import Conflict

        if not isinstance(accept, bool):
            raise ValueError("A boolean review decision is required")
        with self.connect() as db:
            self._lock_user(db, user_id, tenant_id)
            initial = db.execute(
                """SELECT event_id FROM web_changes
                WHERE id=%s AND user_id=%s""",
                (change_id, user_id),
            ).fetchone()
            if initial is None:
                raise Missing()
            expiry = self._lock_event_access(db, user_id, tenant_id, initial[0])
            row = db.execute(
                """SELECT event_id,payload_blob,source_hash,status FROM web_changes
                WHERE id=%s AND user_id=%s FOR UPDATE""",
                (change_id, user_id),
            ).fetchone()
            if row is None or row[0] != initial[0]:
                raise Missing()
            if expiry <= int(self.clock()):
                raise PermissionPending()
            # Keep the displayed change identity fixed. A newer source or a
            # decision from another window requires reloading the review list;
            # it must never silently apply the replacement change instead.
            if row[3] != "pending":
                raise Conflict()
            if accept:
                fields = self._source_fields(row[1])
                self._validate_override(fields)
                db.execute(
                    """INSERT INTO web_event_states(user_id,event_id,override_blob)
                    VALUES(%s,%s,%s) ON CONFLICT(user_id,event_id)
                    DO UPDATE SET override_blob=excluded.override_blob""",
                    (user_id, row[0], self.seal(fields)),
                )
            changed = db.execute(
                """UPDATE web_user_source_versions SET seen_hash=%s
                WHERE user_id=%s AND event_id=%s""",
                (row[2], user_id, row[0]),
            ).rowcount
            if changed != 1:
                raise Missing()
            db.execute(
                "UPDATE web_changes SET status=%s WHERE id=%s AND user_id=%s",
                ("accepted" if accept else "kept", change_id, user_id),
            )
            if expiry <= int(self.clock()):
                raise PermissionPending()

    def known_oracle_events(self, user_id, tenant_id, channels):
        """Recovery only: channels must come from a fresh delegated Graph check."""
        if any(channel.get("tenant_id", tenant_id) != tenant_id for channel in channels):
            raise Missing()
        allowed = {channel["id"] for channel in channels}
        if not allowed:
            return []
        today = (
            datetime.fromtimestamp(self.clock(), timezone(timedelta(hours=9))).date().isoformat()
        )
        with self.connect() as db:
            rows = db.execute(
                """SELECT e.id,e.channel_id,e.start_date,e.end_date,e.cancelled,
                e.payload_blob,s.override_blob FROM web_events e
                JOIN web_channels ch ON ch.id=e.channel_id
                JOIN web_user_source_versions v ON v.event_id=e.id AND v.user_id=%s
                JOIN web_users u ON u.id=v.user_id
                LEFT JOIN web_event_states s ON s.event_id=e.id AND s.user_id=v.user_id
                WHERE ch.tenant_id=%s AND u.tenant_id=ch.tenant_id AND u.active=1
                AND e.id LIKE 'oracle:%%' ORDER BY e.id""",
                (user_id, tenant_id),
            ).fetchall()
        result = []
        for row in rows:
            if row[1] not in allowed:
                continue
            fields = {
                **self._source_fields(row[5]),
                "start_date": row[2],
                "end_date": row[3],
                "cancelled": bool(row[4]),
            }
            if row[6] is not None:
                fields.update(self._source_fields(row[6]))
            if fields.get("cancelled", False) or fields["end_date"] < today:
                continue
            result.append(
                {
                    **{key: fields.get(key) for key in _SOURCE_FIELDS},
                    "id": row[0][len("oracle:") :],
                    "channel_id": row[1],
                }
            )
        return result
