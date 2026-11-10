import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import uuid4

from cryptography.fernet import Fernet

from .calendar_rules import agenda_result, calendar_result
from .calendar_rules import personal_agenda_result as personal_agenda_result
from .personal_types import (
    PersonalAgenda,
    PersonalTask,
    PreferenceSnapshot,
    PreferenceValues,
    TaskChanges,
    TaskContent,
    TaskPayload,
)
from .schemas import Preferences


class Missing(Exception):
    pass


class PermissionPending(Missing):
    """No mutation was committed because a school permission proof needs renewal."""

    STATES = frozenset(
        {
            "connected",
            "checking",
            "not_connected",
            "source_unavailable",
            "verification_failed",
            "refresh_unavailable",
            "refresh_busy",
            "reauth_required",
            "account_not_allowed",
            "source_not_configured",
        }
    )

    def __init__(
        self, *, state="checking", retryable=True, retain_snapshot=False, snapshot_channels=None
    ):
        self.connection_state = state if state in self.STATES else "verification_failed"
        self.retryable = retryable is True
        self.retain_snapshot = retain_snapshot is True and self.retryable
        self.snapshot_channels = snapshot_channels
        super().__init__()


class Conflict(Exception):
    pass


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Database:
    """Local development adapter. No connection to the legacy Oracle database."""

    def __init__(self, path, data_key, clock=time.time):
        self.path = Path(path)
        self.cipher = Fernet(data_key.encode("ascii"))
        self.clock = clock

    def seal(self, payload):
        return self.cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode(
            "ascii"
        )

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode("ascii")))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute(
                "SELECT name FROM sqlite_master WHERE name='web_schema_version'"
            ).fetchone()
            if exists:
                versions = [r[0] for r in c.execute("SELECT version FROM web_schema_version")]
                if versions != [1]:
                    raise RuntimeError(
                        "Unsupported web database schema; apply an explicit migration"
                    )
            c.executescript(
                "BEGIN IMMEDIATE;\n"
                + Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                + "\nCOMMIT;"
            )

    def save_user(self, tenant_id, object_id, profile):
        user_id = f"{tenant_id}:{object_id}"
        with self.connect() as c:
            c.execute(
                "INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET profile_blob=excluded.profile_blob",
                (user_id, tenant_id, object_id, self.seal(profile)),
            )
        return user_id

    def issue_session(self, user_id, lifetime):
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as c:
            user = c.execute("SELECT active FROM web_users WHERE id=?", (user_id,)).fetchone()
            if not user or not user["active"]:
                raise Missing()
            c.execute("DELETE FROM web_sessions WHERE expires_at<=?", (int(self.clock()),))
            c.execute(
                "INSERT INTO web_sessions VALUES(?,?,?,?)",
                (digest(raw), user_id, csrf, int(self.clock()) + lifetime),
            )
        return raw

    def session(self, raw):
        if not raw or len(raw) > 128:
            return None
        with self.connect() as c:
            row = c.execute(
                "SELECT s.*,u.tenant_id,u.object_id,u.profile_blob FROM web_sessions s JOIN web_users u ON u.id=s.user_id WHERE s.id_hash=? AND s.expires_at>? AND u.active=1",
                (digest(raw), int(self.clock())),
            ).fetchone()
            return dict(row) if row else None

    def revoke_session(self, raw):
        with self.connect() as c:
            c.execute("DELETE FROM web_sessions WHERE id_hash=?", (digest(raw),))

    def store_flow(self, flow):
        raw = secrets.token_urlsafe(32)
        with self.connect() as c:
            c.execute("DELETE FROM web_auth_flows WHERE expires_at<=?", (int(self.clock()),))
            c.execute(
                "INSERT INTO web_auth_flows VALUES(?,?,?)",
                (digest(raw), self.seal(flow), int(self.clock()) + 600),
            )
        return raw

    # 인증 흐름을 조회하면서 즉시 삭제해 같은 로그인 응답이 두 번 사용되지 않게 한다.
    def consume_flow(self, raw):
        if not raw or len(raw) > 128:
            raise Missing()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM web_auth_flows WHERE id_hash=?", (digest(raw),)
            ).fetchone()
            c.execute("DELETE FROM web_auth_flows WHERE id_hash=?", (digest(raw),))
        if not row or row["expires_at"] <= self.clock():
            raise Missing()
        return self.unseal(row["flow_blob"])

    def preferences(self, user_id: str) -> PreferenceSnapshot:
        with self.connect() as c:
            row = c.execute(
                "SELECT payload_blob FROM web_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
            return self.unseal(row[0]) if row else {**Preferences().to_values()}

    def save_preferences(self, user_id: str, values: PreferenceValues) -> PreferenceValues:
        with self.connect() as c:
            c.execute(
                "INSERT INTO web_preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload_blob=excluded.payload_blob",
                (user_id, self.seal(values)),
            )
        return values

    def channels(self, user_id, tenant_id):
        with self.connect() as c:
            rows = c.execute(
                "SELECT ch.*,COALESCE(p.enabled,1) AS enabled FROM web_channels ch JOIN web_channel_access a ON a.channel_id=ch.id LEFT JOIN web_channel_preferences p ON p.channel_id=ch.id AND p.user_id=a.user_id WHERE a.user_id=? AND ch.tenant_id=? AND a.valid_until>? ORDER BY ch.id",
                (user_id, tenant_id, int(self.clock())),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "name": self.unseal(r["label_blob"])["name"],
                    "enabled": bool(r["enabled"]),
                }
                for r in rows
            ]

    def channel_preference(
        self, user_id: str, tenant_id: str, channel_id: str, enabled: bool
    ) -> None:
        if channel_id not in {ch["id"] for ch in self.channels(user_id, tenant_id)}:
            raise Missing()
        with self.connect() as c:
            c.execute(
                "INSERT INTO web_channel_preferences VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET enabled=excluded.enabled",
                (user_id, channel_id, int(enabled)),
            )

    def add_task(self, user_id: str, payload: TaskPayload) -> PersonalTask:
        item_id = str(uuid4())
        with self.connect() as c:
            c.execute(
                "INSERT INTO web_personal_tasks(id,user_id,start_date,end_date,payload_blob) VALUES(?,?,?,?,?)",
                (item_id, user_id, payload["start_date"], payload["end_date"], self.seal(payload)),
            )
        return self.task(user_id, item_id)

    def _task_value(self, row) -> PersonalTask:
        # Keep legacy/extra payload fields rather than validating a response model.
        payload = cast(TaskContent, self.unseal(row["payload_blob"]))
        return {
            **payload,
            "id": row["id"],
            "source": "personal",
            "kind": "personal",
            "completed": bool(row["completed"]),
            "hidden": bool(row["hidden"]),
            "version": row["version"],
            "cancelled": False,
        }

    def task(self, user_id: str, item_id: str) -> PersonalTask:
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM web_personal_tasks WHERE user_id=? AND id=?", (user_id, item_id)
            ).fetchone()
            if not row:
                raise Missing()
            return self._task_value(row)

    def personal_agenda(self, user_id: str, tenant_id: str, day: str) -> PersonalAgenda:
        # This path intentionally reads only personal rows. A pending Teams
        # proof must not hide the signed-in user's own tasks.
        with self.connect() as c:
            rows = c.execute(
                """SELECT t.* FROM web_personal_tasks t
                JOIN web_users u ON u.id=t.user_id
                WHERE t.user_id=? AND u.tenant_id=? AND u.active=1
                AND t.start_date<=? AND t.end_date>=? AND t.hidden=0""",
                (user_id, tenant_id, day, day),
            ).fetchall()
        return personal_agenda_result([self._task_value(row) for row in rows], day)

    # 소유자를 확인하고 전달된 버전이 현재 값과 같을 때만 개인 일정 내용을 수정한다.
    def update_task(
        self, user_id: str, item_id: str, payload: TaskPayload, version: int
    ) -> PersonalTask:
        self.task(user_id, item_id)
        with self.connect() as c:
            cursor = c.execute(
                "UPDATE web_personal_tasks SET start_date=?,end_date=?,payload_blob=?,version=version+1 WHERE user_id=? AND id=? AND version=?",
                (
                    payload["start_date"],
                    payload["end_date"],
                    self.seal(payload),
                    user_id,
                    item_id,
                    version,
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict()
        return self.task(user_id, item_id)

    # 전달된 완료·숨김 값만 반영하며 다른 화면에서 먼저 수정한 경우 버전 충돌을 반환한다.
    def task_state(
        self, user_id: str, item_id: str, values: TaskChanges, version: int
    ) -> PersonalTask:
        self.task(user_id, item_id)
        updates = {
            k: int(v) for k, v in values.items() if k in {"hidden", "completed"} and v is not None
        }
        assignments = ",".join(k + "=?" for k in updates)
        with self.connect() as c:
            cursor = c.execute(
                f"UPDATE web_personal_tasks SET {assignments},version=version+1 WHERE user_id=? AND id=? AND version=?",
                (*updates.values(), user_id, item_id, version),
            )
            if cursor.rowcount != 1:
                raise Conflict()
        return self.task(user_id, item_id)

    def delete_task(self, user_id: str, item_id: str, version: int) -> None:
        self.task(user_id, item_id)
        with self.connect() as c:
            cursor = c.execute(
                "DELETE FROM web_personal_tasks WHERE user_id=? AND id=? AND version=?",
                (user_id, item_id, version),
            )
            if cursor.rowcount != 1:
                raise Conflict()

    # 아직 유효한 사용자별 채널 권한 범위에서만 공용 일정을 읽고 개인 표시 상태를 합친다.
    def _shared(self, user_id, tenant_id):
        with self.connect() as c:
            rows = c.execute(
                "SELECT e.*,ch.label_blob,COALESCE(s.hidden,0) AS hidden,COALESCE(s.completed,0) AS completed,COALESCE(p.enabled,1) AS channel_enabled FROM web_events e JOIN web_channels ch ON ch.id=e.channel_id JOIN web_channel_access a ON a.channel_id=e.channel_id AND a.user_id=? LEFT JOIN web_event_states s ON s.event_id=e.id AND s.user_id=a.user_id LEFT JOIN web_channel_preferences p ON p.channel_id=e.channel_id AND p.user_id=a.user_id WHERE ch.tenant_id=? AND a.valid_until>?",
                (user_id, tenant_id, int(self.clock())),
            ).fetchall()
            return [
                {
                    **self.unseal(r["payload_blob"]),
                    "id": r["id"],
                    "source": "teams",
                    "channel_id": r["channel_id"],
                    "channel": self.unseal(r["label_blob"])["name"],
                    "start_date": r["start_date"],
                    "end_date": r["end_date"],
                    "kind": r["kind"],
                    "cancelled": bool(r["cancelled"]),
                    "hidden": bool(r["hidden"]),
                    "completed": bool(r["completed"]),
                    "revision": r["revision"],
                    "channel_enabled": bool(r["channel_enabled"]),
                }
                for r in rows
            ]

    def event_state(self, user_id, tenant_id, event_id, values):
        if event_id not in {item["id"] for item in self._shared(user_id, tenant_id)}:
            raise Missing()
        updates = {
            k: int(v) for k, v in values.items() if k in {"hidden", "completed"} and v is not None
        }
        with self.connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO web_event_states(user_id,event_id) VALUES(?,?)",
                (user_id, event_id),
            )
            assignments = ",".join(k + "=?" for k in updates)
            c.execute(
                f"UPDATE web_event_states SET {assignments} WHERE user_id=? AND event_id=?",
                (*updates.values(), user_id, event_id),
            )
        return next(item for item in self._shared(user_id, tenant_id) if item["id"] == event_id)

    def calendar(self, user_id, tenant_id, start, end, hidden_only=False):
        prefs = self.preferences(user_id)
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM web_personal_tasks WHERE user_id=? AND start_date<=? AND end_date>=?",
                (user_id, end, start),
            ).fetchall()
            personal = [self._task_value(row) for row in rows]
        return calendar_result(
            personal, self._shared(user_id, tenant_id), prefs, start, end, hidden_only
        )

    def agenda(self, user_id, tenant_id, day):
        return agenda_result(self.calendar(user_id, tenant_id, day, day), day)
