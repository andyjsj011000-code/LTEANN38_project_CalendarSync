from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import uuid4

from cryptography.fernet import Fernet

from .schemas import Preferences


class Missing(Exception):
    pass


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
        return self.cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")

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
            exists = c.execute("SELECT name FROM sqlite_master WHERE name='web_schema_version'").fetchone()
            if exists:
                versions = [r[0] for r in c.execute("SELECT version FROM web_schema_version")]
                if versions != [1]:
                    raise RuntimeError("Unsupported web database schema; apply an explicit migration")
            c.executescript("BEGIN IMMEDIATE;\n" + Path(__file__).with_name("schema.sql").read_text(encoding="utf-8") + "\nCOMMIT;")

    def save_user(self, tenant_id, object_id, profile):
        user_id = f"{tenant_id}:{object_id}"
        with self.connect() as c:
            c.execute("INSERT INTO web_users(id,tenant_id,object_id,profile_blob) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET profile_blob=excluded.profile_blob", (user_id, tenant_id, object_id, self.seal(profile)))
        return user_id

    def issue_session(self, user_id, lifetime):
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as c:
            user = c.execute("SELECT active FROM web_users WHERE id=?", (user_id,)).fetchone()
            if not user or not user["active"]:
                raise Missing()
            c.execute("DELETE FROM web_sessions WHERE expires_at<=?", (int(self.clock()),))
            c.execute("INSERT INTO web_sessions VALUES(?,?,?,?)", (digest(raw), user_id, csrf, int(self.clock()) + lifetime))
        return raw

    def session(self, raw):
        if not raw or len(raw) > 128:
            return None
        with self.connect() as c:
            row = c.execute("SELECT s.*,u.tenant_id,u.object_id,u.profile_blob FROM web_sessions s JOIN web_users u ON u.id=s.user_id WHERE s.id_hash=? AND s.expires_at>? AND u.active=1", (digest(raw), int(self.clock()))).fetchone()
            return dict(row) if row else None

    def revoke_session(self, raw):
        with self.connect() as c:
            c.execute("DELETE FROM web_sessions WHERE id_hash=?", (digest(raw),))

    def store_flow(self, flow):
        raw = secrets.token_urlsafe(32)
        with self.connect() as c:
            c.execute("DELETE FROM web_auth_flows WHERE expires_at<=?", (int(self.clock()),))
            c.execute("INSERT INTO web_auth_flows VALUES(?,?,?)", (digest(raw), self.seal(flow), int(self.clock()) + 600))
        return raw

    def consume_flow(self, raw):
        if not raw or len(raw) > 128:
            raise Missing()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM web_auth_flows WHERE id_hash=?", (digest(raw),)).fetchone()
            c.execute("DELETE FROM web_auth_flows WHERE id_hash=?", (digest(raw),))
        if not row or row["expires_at"] <= self.clock():
            raise Missing()
        return self.unseal(row["flow_blob"])

    def preferences(self, user_id):
        with self.connect() as c:
            row = c.execute("SELECT payload_blob FROM web_preferences WHERE user_id=?", (user_id,)).fetchone()
            return self.unseal(row[0]) if row else Preferences().model_dump()

    def save_preferences(self, user_id, values):
        with self.connect() as c:
            c.execute("INSERT INTO web_preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload_blob=excluded.payload_blob", (user_id, self.seal(values)))
        return values

    def channels(self, user_id, tenant_id):
        with self.connect() as c:
            rows = c.execute("SELECT ch.*,COALESCE(p.enabled,1) AS enabled FROM web_channels ch JOIN web_channel_access a ON a.channel_id=ch.id LEFT JOIN web_channel_preferences p ON p.channel_id=ch.id AND p.user_id=a.user_id WHERE a.user_id=? AND ch.tenant_id=? AND a.valid_until>? ORDER BY ch.id", (user_id, tenant_id, int(self.clock()))).fetchall()
            return [{"id": r["id"], "name": self.unseal(r["label_blob"])["name"], "enabled": bool(r["enabled"])} for r in rows]

    def channel_preference(self, user_id, tenant_id, channel_id, enabled):
        if channel_id not in {ch["id"] for ch in self.channels(user_id, tenant_id)}:
            raise Missing()
        with self.connect() as c:
            c.execute("INSERT INTO web_channel_preferences VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET enabled=excluded.enabled", (user_id, channel_id, int(enabled)))

    def add_task(self, user_id, payload):
        item_id = str(uuid4())
        with self.connect() as c:
            c.execute("INSERT INTO web_personal_tasks(id,user_id,start_date,end_date,payload_blob) VALUES(?,?,?,?,?)", (item_id, user_id, payload["start_date"], payload["end_date"], self.seal(payload)))
        return self.task(user_id, item_id)

    def _task_value(self, row):
        return {**self.unseal(row["payload_blob"]), "id": row["id"], "source": "personal", "kind": "personal", "completed": bool(row["completed"]), "hidden": bool(row["hidden"]), "version": row["version"], "cancelled": False}

    def task(self, user_id, item_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM web_personal_tasks WHERE user_id=? AND id=?", (user_id, item_id)).fetchone()
            if not row:
                raise Missing()
            return self._task_value(row)

    def update_task(self, user_id, item_id, payload, version):
        self.task(user_id, item_id)
        with self.connect() as c:
            cursor = c.execute("UPDATE web_personal_tasks SET start_date=?,end_date=?,payload_blob=?,version=version+1 WHERE user_id=? AND id=? AND version=?", (payload["start_date"], payload["end_date"], self.seal(payload), user_id, item_id, version))
            if cursor.rowcount != 1:
                raise Conflict()
        return self.task(user_id, item_id)

    def task_state(self, user_id, item_id, values, version):
        self.task(user_id, item_id)
        updates = {k: int(v) for k, v in values.items() if k in {"hidden", "completed"} and v is not None}
        assignments = ",".join(k + "=?" for k in updates)
        with self.connect() as c:
            cursor = c.execute(f"UPDATE web_personal_tasks SET {assignments},version=version+1 WHERE user_id=? AND id=? AND version=?", (*updates.values(), user_id, item_id, version))
            if cursor.rowcount != 1:
                raise Conflict()
        return self.task(user_id, item_id)

    def delete_task(self, user_id, item_id, version):
        self.task(user_id, item_id)
        with self.connect() as c:
            cursor = c.execute("DELETE FROM web_personal_tasks WHERE user_id=? AND id=? AND version=?", (user_id, item_id, version))
            if cursor.rowcount != 1:
                raise Conflict()

    def _shared(self, user_id, tenant_id):
        with self.connect() as c:
            rows = c.execute("SELECT e.*,ch.label_blob,COALESCE(s.hidden,0) AS hidden,COALESCE(s.completed,0) AS completed,COALESCE(p.enabled,1) AS channel_enabled FROM web_events e JOIN web_channels ch ON ch.id=e.channel_id JOIN web_channel_access a ON a.channel_id=e.channel_id AND a.user_id=? LEFT JOIN web_event_states s ON s.event_id=e.id AND s.user_id=a.user_id LEFT JOIN web_channel_preferences p ON p.channel_id=e.channel_id AND p.user_id=a.user_id WHERE ch.tenant_id=? AND a.valid_until>?", (user_id, tenant_id, int(self.clock()))).fetchall()
            return [{**self.unseal(r["payload_blob"]), "id": r["id"], "source": "teams", "channel_id": r["channel_id"], "channel": self.unseal(r["label_blob"])["name"], "start_date": r["start_date"], "end_date": r["end_date"], "kind": r["kind"], "cancelled": bool(r["cancelled"]), "hidden": bool(r["hidden"]), "completed": bool(r["completed"]), "revision": r["revision"], "channel_enabled": bool(r["channel_enabled"])} for r in rows]

    def event_state(self, user_id, tenant_id, event_id, values):
        if event_id not in {item["id"] for item in self._shared(user_id, tenant_id)}:
            raise Missing()
        updates = {k: int(v) for k, v in values.items() if k in {"hidden", "completed"} and v is not None}
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO web_event_states(user_id,event_id) VALUES(?,?)", (user_id, event_id))
            assignments = ",".join(k + "=?" for k in updates)
            c.execute(f"UPDATE web_event_states SET {assignments} WHERE user_id=? AND event_id=?", (*updates.values(), user_id, event_id))
        return next(item for item in self._shared(user_id, tenant_id) if item["id"] == event_id)

    def calendar(self, user_id, tenant_id, start, end, hidden_only=False):
        prefs = self.preferences(user_id)
        with self.connect() as c:
            rows = c.execute("SELECT * FROM web_personal_tasks WHERE user_id=? AND start_date<=? AND end_date>=?", (user_id, end, start)).fetchall()
            personal = [self._task_value(row) for row in rows]
        teams = [e for e in self._shared(user_id, tenant_id) if e["start_date"] <= end and e["end_date"] >= start]
        result = []
        for e in personal + teams:
            if hidden_only:
                if not e["hidden"]:
                    continue
            elif e["hidden"] or (e["source"] == "teams" and (not e["channel_enabled"] or e["kind"] in prefs["disabled_kinds"])):
                continue
            result.append({k: v for k, v in e.items() if k != "channel_enabled"})
        return sorted(result, key=lambda e: (e.get("time") or "99:99", e["source"], e["id"]))

    def agenda(self, user_id, tenant_id, day):
        items = self.calendar(user_id, tenant_id, day, day)
        def appears(e):
            if e["source"] == "teams" or e["end_date"] == day:
                return True
            mode = e["display_mode"]
            return mode == "daily" or (mode == "selected" and day in e["selected_dates"])
        return {"date": day, "teams": [e for e in items if e["source"] == "teams" and appears(e)], "personal": [e for e in items if e["source"] == "personal" and appears(e)]}
