"""Synthetic-data beta. Run explicitly; production create_app has no beta login."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .api import SESSION_COOKIE, create_app
from .config import Settings
from .identity import Unavailable
from .schemas import Input, TaskInput
from .storage import Database, Missing, digest

BETA_TENANT = "00000000-0000-4000-8000-000000000001"
FORMAT = "calendarsync-synthetic-beta-v1"
KST = timezone(timedelta(hours=9))
BETA_SQL = """
CREATE TABLE IF NOT EXISTS beta_environment (format TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS beta_credentials (
  user_id TEXT PRIMARY KEY REFERENCES web_users(id),
  code_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS beta_login_limits (
  fingerprint TEXT NOT NULL, minute INTEGER NOT NULL, attempts INTEGER NOT NULL,
  PRIMARY KEY(fingerprint, minute)
);
CREATE TABLE IF NOT EXISTS beta_feedback (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
  body_blob TEXT NOT NULL, created_at INTEGER NOT NULL
);
"""


class BetaDatabase(Database):
    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            # An ordinary web DB or legacy data must never be relabeled as beta.
            with self.connect() as c:
                table = c.execute("SELECT name FROM sqlite_master WHERE name='beta_environment'").fetchone()
                if not table or [r[0] for r in c.execute("SELECT format FROM beta_environment")] != [FORMAT]:
                    raise RuntimeError("Refusing to open a non-beta database")
                if c.execute("SELECT 1 FROM web_users WHERE tenant_id<>? LIMIT 1", (BETA_TENANT,)).fetchone():
                    raise RuntimeError("Refusing beta access to real-account data")
            super().initialize()
            return
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as c:
            c.executescript("BEGIN IMMEDIATE;\n" + schema + BETA_SQL + "\nINSERT INTO beta_environment VALUES('" + FORMAT + "');\nCOMMIT;")

    def add_participant(self, label):
        label = label.strip()
        if not 1 <= len(label) <= 100:
            raise ValueError("Participant label must be 1-100 characters")
        code = secrets.token_urlsafe(24)
        with self.connect() as c:
            row = c.execute("SELECT user_id FROM beta_credentials WHERE label=?", (label,)).fetchone()
        if row:
            user_id = row["user_id"]
            with self.connect() as c:
                c.execute("UPDATE beta_credentials SET code_hash=? WHERE user_id=?", (digest(code), user_id))
                c.execute("DELETE FROM web_sessions WHERE user_id=?", (user_id,))
        else:
            user_id = self.save_user(BETA_TENANT, str(uuid4()), {"name": label, "email": ""})
            with self.connect() as c:
                c.execute("INSERT INTO beta_credentials VALUES(?,?,?)", (user_id, digest(code), label))
            day = datetime.fromtimestamp(self.clock(), KST).date()
            self.add_task(user_id, TaskInput(title="이번 주 개발 완료하기", notes="직접 적어 보는 개인 할 일의 예시예요. 수정하거나 지워도 됩니다.", start_date=day - timedelta(days=1), end_date=day + timedelta(days=2), display_mode="selected", selected_dates=[day]).model_dump(mode="json"))
        self.seed_examples(user_id)
        return code

    def seed_examples(self, user_id):
        day = datetime.fromtimestamp(self.clock(), KST).date()
        samples = [
            ("beta-homework", "beta-class", "물리학 과제 제출", day, day, "18:00", "assignment", False),
            ("beta-application", "beta-common", "동아리 신청 마감", day + timedelta(days=1), day + timedelta(days=1), "17:00", "application", False),
            ("beta-project", "beta-class", "탐구 보고서 준비 기간", day - timedelta(days=2), day + timedelta(days=4), None, "assignment", False),
            ("beta-meeting", "beta-common", "진로 특강", day + timedelta(days=2), day + timedelta(days=2), "15:00", "event", False),
            ("beta-cancelled", "beta-common", "설명회 (취소 예시)", day + timedelta(days=1), day + timedelta(days=1), "13:00", "event", True),
        ]
        with self.connect() as c:
            for channel_id, name in (("beta-class", "수업 공지 · 예시"), ("beta-common", "공통 공지 · 예시")):
                c.execute("INSERT OR IGNORE INTO web_channels VALUES(?,?,?)", (channel_id, BETA_TENANT, self.seal({"name": name})))
                c.execute("INSERT INTO web_channel_access VALUES(?,?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET confirmed_at=excluded.confirmed_at,valid_until=excluded.valid_until", (user_id, channel_id, int(self.clock()), int(self.clock()) + 365 * 86400))
            for event_id, channel_id, title, start, end, at, kind, cancelled in samples:
                payload = self.seal({"title": title, "notes": "베타 테스트용으로 만든 예시 일정입니다. 실제 학교 공지가 아닙니다.", "time": at})
                c.execute("INSERT OR IGNORE INTO web_events(id,channel_id,start_date,end_date,kind,payload_blob,cancelled) VALUES(?,?,?,?,?,?,?)", (event_id, channel_id, start.isoformat(), end.isoformat(), kind, payload, int(cancelled)))

    def redeem(self, code, fingerprint):
        minute = int(self.clock()) // 60
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM beta_login_limits WHERE minute<?", (minute - 2,))
            c.execute("INSERT INTO beta_login_limits VALUES(?,?,1) ON CONFLICT(fingerprint,minute) DO UPDATE SET attempts=attempts+1", (fingerprint, minute))
            attempts = c.execute("SELECT attempts FROM beta_login_limits WHERE fingerprint=? AND minute=?", (fingerprint, minute)).fetchone()[0]
            row = c.execute("SELECT b.user_id FROM beta_credentials b JOIN web_users u ON u.id=b.user_id WHERE b.code_hash=? AND u.active=1 AND u.tenant_id=?", (digest(code), BETA_TENANT)).fetchone()
        if attempts > 20:
            raise HTTPException(429, "시도가 많아요. 잠시 후 다시 입력해 주세요.")
        if not row:
            raise HTTPException(401, "초대 코드를 확인해 주세요.")
        self.seed_examples(row["user_id"])
        return row["user_id"]


class BetaLogin(Input):
    code: str = Field(min_length=16, max_length=128)


class BetaFeedback(Input):
    message: str = Field(min_length=1, max_length=4000)


class BetaDisplay(Input):
    channels: dict[str, bool] = Field(max_length=1000)
    disabled_kinds: list[str] = Field(max_length=50)


class NoSchoolIdentity:
    def begin(self):
        raise Unavailable("This beta uses invitation codes")
    def finish(self, *args):
        raise Unavailable("School identity is disabled for the synthetic beta")


def read_beta_config(path):
    path = Path(path).resolve()
    values = json.loads(path.read_text(encoding="utf-8"))
    if values.get("format") != FORMAT:
        raise ValueError("Not a CalendarSync beta configuration")
    database = (path.parent / values["database_file"]).resolve()
    if not database.is_relative_to(path.parent) or database == path:
        raise ValueError("Beta database must stay inside the beta data directory")
    return Settings(database_path=database, data_key=values["data_key"], origin=values["origin"])


def prepare_beta(data_dir, origin, labels=("테스터 1", "테스터 2", "테스터 3")):
    directory = Path(data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.json"
    if not config_path.exists():
        key = Fernet.generate_key().decode()
        Settings(database_path=directory / "beta.sqlite3", data_key=key, origin=origin)
        # Exclusive creation avoids replacing a key used by an existing database.
        with config_path.open("x", encoding="utf-8") as f:
            json.dump({"format": FORMAT, "data_key": key, "database_file": "beta.sqlite3", "origin": origin}, f, indent=2)
        config_path.chmod(0o600)
    settings = read_beta_config(config_path)
    if settings.origin != origin:
        raise ValueError("Existing beta origin differs; reuse its original address or create a new data directory")
    db = BetaDatabase(settings.database_path, settings.data_key)
    db.initialize()
    invites_path = directory / "invitations.json"
    if not invites_path.exists():
        invitations = [{"name": label, "code": db.add_participant(label)} for label in labels]
        with invites_path.open("x", encoding="utf-8") as f:
            json.dump(invitations, f, ensure_ascii=False, indent=2)
        invites_path.chmod(0o600)
    else:
        invitations = json.loads(invites_path.read_text(encoding="utf-8"))
    return config_path, invitations


def create_beta_app(settings=None, database=None):
    if settings is None:
        settings = read_beta_config(os.environ.get("CALENDARSYNC_BETA_CONFIG", "beta/var/data/config.json"))
    if settings.tenant_id or settings.client_id or settings.client_secret:
        raise ValueError("School credentials must not be configured in the synthetic beta")
    db = database or BetaDatabase(settings.database_path, settings.data_key)
    if not isinstance(db, BetaDatabase):
        raise ValueError("Beta requires an isolated synthetic-data database")
    frontend = Path(__file__).with_name("static")
    app = create_app(settings, db, NoSchoolIdentity(), frontend_directory=frontend)
    app.title = "CalendarSync Beta"
    require_session = app.state.require_session

    @app.get("/api/beta/info")
    def beta_info():
        return {"version": "0.1.0-beta.1", "synthetic_data_only": True, "school_login": False, "message": "Teams 일정은 예시입니다. 직접 작성한 할 일은 테스트 계정별로 저장됩니다."}

    @app.put("/api/beta/display-settings")
    def display_settings(body: BetaDisplay, session=Depends(require_session)):
        allowed = {ch["id"] for ch in db.channels(session["user_id"], session["tenant_id"])}
        if not body.channels.keys() <= allowed:
            raise Missing()
        if not set(body.disabled_kinds) <= {"assignment", "application", "event"}:
            raise HTTPException(422, "Unknown beta category")
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT payload_blob FROM web_preferences WHERE user_id=?", (session["user_id"],)).fetchone()
            prefs = db.unseal(row[0]) if row else db.preferences(session["user_id"])
            prefs["disabled_kinds"] = sorted(set(body.disabled_kinds))
            c.execute("INSERT INTO web_preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload_blob=excluded.payload_blob", (session["user_id"], db.seal(prefs)))
            for channel_id, enabled in body.channels.items():
                c.execute("INSERT INTO web_channel_preferences VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET enabled=excluded.enabled", (session["user_id"], channel_id, int(enabled)))
        return {"saved": True}

    @app.post("/auth/beta")
    def beta_login(body: BetaLogin, request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin != settings.origin:
            raise HTTPException(403, "Invalid request origin")
        address = request.client.host if request.client else "unknown"
        fingerprint = hmac.new(settings.data_key.encode(), address.encode(), hashlib.sha256).hexdigest()
        user_id = db.redeem(body.code, fingerprint)
        previous = request.cookies.get(SESSION_COOKIE)
        if previous:
            db.revoke_session(previous)
        raw = db.issue_session(user_id, settings.session_seconds)
        response = JSONResponse({"ok": True})
        response.set_cookie(SESSION_COOKIE, raw, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=settings.session_seconds, path="/")
        return response

    @app.get("/api/beta/hidden")
    def hidden(session=Depends(require_session)):
        return {"items": db.calendar(session["user_id"], session["tenant_id"], "0001-01-01", "9999-12-31", hidden_only=True)}

    @app.post("/api/beta/feedback", status_code=201)
    def save_feedback(body: BetaFeedback, session=Depends(require_session)):
        feedback_id = str(uuid4())
        with db.connect() as c:
            c.execute("INSERT INTO beta_feedback VALUES(?,?,?,?)", (feedback_id, session["user_id"], db.seal({"message": body.message}), int(db.clock())))
        return {"id": feedback_id, "saved": True}

    return app
