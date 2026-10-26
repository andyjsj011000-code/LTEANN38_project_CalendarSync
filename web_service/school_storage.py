"""Server-only credentials and short-lived channel permission proofs."""
import hashlib
import hmac
from .storage import Database, Missing, digest

SCHOOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_school_tokens (
 session_hash TEXT PRIMARY KEY REFERENCES web_sessions(id_hash) ON DELETE CASCADE,
 token_blob TEXT NOT NULL, expires_at INTEGER NOT NULL, last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_school_sync (
 user_id TEXT PRIMARY KEY REFERENCES web_users(id), checked_at INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'not_connected', lease_until INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_security_limits (
 key_hash TEXT NOT NULL, bucket INTEGER NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(key_hash,bucket));
CREATE TABLE IF NOT EXISTS web_security_audit (at INTEGER NOT NULL, event TEXT NOT NULL, principal_hash TEXT NOT NULL);
"""

class SchoolDatabase(Database):
    def __init__(self, path, data_key, **kwargs):
        super().__init__(path, data_key, **kwargs)
        self._audit_key = hashlib.sha256(("school-audit:" + data_key).encode()).digest()

    def initialize(self):
        if self.path.exists():
            with self.connect() as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='beta_environment'").fetchone():
                    raise ValueError("Use a separate database for school accounts")
        super().initialize()
        with self.connect() as c:
            c.executescript(SCHOOL_SCHEMA)

    def fingerprint(self, value):
        return hmac.new(self._audit_key, value.encode(), hashlib.sha256).hexdigest()

    def audit(self, event, principal):
        with self.connect() as c:
            c.execute("DELETE FROM web_security_audit WHERE at<?", (int(self.clock()) - 7*86400,))
            c.execute("INSERT INTO web_security_audit VALUES(?,?,?)", (int(self.clock()), event, self.fingerprint(principal)))

    def allow(self, key, limit):
        bucket = int(self.clock()) // 60
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM web_security_limits WHERE bucket<?", (bucket-2,))
            hashed = self.fingerprint(key)
            c.execute("INSERT INTO web_security_limits VALUES(?,?,1) ON CONFLICT(key_hash,bucket) DO UPDATE SET attempts=attempts+1", (hashed,bucket))
            return c.execute("SELECT attempts FROM web_security_limits WHERE key_hash=? AND bucket=?", (hashed,bucket)).fetchone()[0] <= limit

    def bind_token(self, raw, token, expiry):
        with self.connect() as c:
            c.execute("INSERT INTO web_school_tokens VALUES(?,?,?,?)", (digest(raw),self.seal({"access_token":token}),expiry,int(self.clock())))

    def school_session(self, raw, idle_seconds):
        session = super().session(raw)
        if not session:
            return None
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM web_school_tokens WHERE session_hash=?", (session["id_hash"],)).fetchone()
            if not row or row["expires_at"] <= self.clock() or row["last_seen"] + idle_seconds <= self.clock():
                c.execute("DELETE FROM web_sessions WHERE id_hash=?", (session["id_hash"],))
                return None
            c.execute("UPDATE web_school_tokens SET last_seen=? WHERE session_hash=?", (int(self.clock()),session["id_hash"]))
        return session

    def token(self, session):
        with self.connect() as c:
            row = c.execute("SELECT token_blob,expires_at FROM web_school_tokens WHERE session_hash=?", (session["id_hash"],)).fetchone()
        if not row or row["expires_at"] <= self.clock():
            raise Missing()
        return self.unseal(row["token_blob"])["access_token"]

    def sync_status(self, user_id):
        with self.connect() as c:
            row = c.execute("SELECT checked_at,state,lease_until FROM web_school_sync WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {"checked_at":0,"state":"not_connected","lease_until":0}

    def begin_sync(self, user_id, ttl):
        now = int(self.clock())
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT OR IGNORE INTO web_school_sync(user_id) VALUES(?)", (user_id,))
            row = c.execute("SELECT * FROM web_school_sync WHERE user_id=?", (user_id,)).fetchone()
            if row["lease_until"] > now or row["checked_at"] + ttl > now:
                return False
            c.execute("UPDATE web_school_sync SET lease_until=?,state='checking' WHERE user_id=?", (now+180,user_id))
            c.execute("DELETE FROM web_channel_access WHERE user_id=?", (user_id,))
        return now+180

    def fail_sync(self, user_id, state, lease):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            changed = c.execute("UPDATE web_school_sync SET checked_at=?,state=?,lease_until=0 WHERE user_id=? AND lease_until=?", (int(self.clock()),state,user_id,lease)).rowcount
            if changed:
                c.execute("DELETE FROM web_channel_access WHERE user_id=?", (user_id,))

    def apply_snapshot(self, session, channels, events, ttl, lease=None):
        now = int(self.clock()); user_id,tenant = session["user_id"],session["tenant_id"]
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            alive = c.execute("SELECT 1 FROM web_sessions s JOIN web_users u ON u.id=s.user_id JOIN web_school_tokens t ON t.session_hash=s.id_hash WHERE s.id_hash=? AND s.expires_at>? AND t.expires_at>? AND u.active=1", (session["id_hash"],now,now)).fetchone()
            current = c.execute("SELECT lease_until FROM web_school_sync WHERE user_id=?", (user_id,)).fetchone()
            if not alive or not current or current[0] != lease or lease <= now:
                raise Missing()
            allowed = {ch["id"] for ch in channels}; seen = set()
            c.execute("DELETE FROM web_channel_access WHERE user_id=?", (user_id,))
            for ch in channels:
                old = c.execute("SELECT tenant_id FROM web_channels WHERE id=?", (ch["id"],)).fetchone()
                if old and old[0] != tenant:
                    raise ValueError("Channel tenant mismatch")
                c.execute("INSERT INTO web_channels VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET label_blob=excluded.label_blob", (ch["id"],tenant,self.seal({"name":ch["name"]})))
                c.execute("INSERT INTO web_channel_access VALUES(?,?,?,?)", (user_id,ch["id"],now,now+ttl))
            for e in events:
                if e["channel_id"] not in allowed:
                    raise ValueError("Unauthorized source channel")
                event_id = "oracle:"+e["id"]; seen.add(event_id)
                old = c.execute("SELECT channel_id FROM web_events WHERE id=?", (event_id,)).fetchone()
                if old and old[0] != e["channel_id"]:
                    raise ValueError("Event channel changed")
                blob = self.seal({k:e.get(k) for k in ("title","notes","time","source_url")})
                c.execute("INSERT INTO web_events(id,channel_id,start_date,end_date,kind,payload_blob) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET start_date=excluded.start_date,end_date=excluded.end_date,kind=excluded.kind,payload_blob=excluded.payload_blob,cancelled=0,revision=web_events.revision+1", (event_id,e["channel_id"],e["start_date"],e["end_date"],e["kind"],blob))
            for channel_id in allowed:
                for row in c.execute("SELECT id FROM web_events WHERE channel_id=? AND id LIKE 'oracle:%'", (channel_id,)).fetchall():
                    if row[0] not in seen:
                        c.execute("UPDATE web_events SET cancelled=1 WHERE id=?", (row[0],))
            c.execute("UPDATE web_school_sync SET checked_at=?,state='connected',lease_until=0 WHERE user_id=?", (now,user_id))

    def revoke_all(self, user_id):
        with self.connect() as c:
            c.execute("DELETE FROM web_sessions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM web_channel_access WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM web_school_sync WHERE user_id=?", (user_id,))
