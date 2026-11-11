"""Account-scoped Classroom imports from a user's Apps Script."""

import re
import secrets
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException
from pydantic import AwareDatetime, Field, field_validator

from .schema_inspection import query
from .schemas import Input
from .storage import digest

_TOKEN = re.compile(r"Bearer ([A-Za-z0-9_-]{43})")
_MAX_SNAPSHOT_AGE_MS = 24 * 60 * 60 * 1000


class ClassroomItem(Input):
    kind: Literal["announcement", "assignment"]
    course: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=240)
    url: str = Field(default="", max_length=320)
    due: AwareDatetime | date | None = None

    @field_validator("url")
    @classmethod
    def classroom_url(cls, value):
        if value:
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or url.netloc != "classroom.google.com"
                or not url.path.startswith("/c/")
                or any(ord(character) < 33 for character in value)
            ):
                raise ValueError("Expected a Classroom source link")
        return value


class ClassroomSnapshot(Input):
    captured_at: int = Field(ge=1, strict=True)
    items: list[ClassroomItem] = Field(max_length=24)


def install_classroom_routes(app, db, require_session):
    backend = "sqlite" if hasattr(db, "path") else "postgres"
    with db.connect() as connection:
        connection.execute(
            "SELECT user_id,token_hash,captured_at,payload_blob "
            "FROM web_classroom_connections WHERE 1=0"
        )

    @app.get("/api/classroom")
    def classroom(
        session=Depends(require_session), x_csrf_token: str | None = Header(default=None)
    ):
        # An open tab can outlive the cookie that established its identity.
        if x_csrf_token is not None and not secrets.compare_digest(
            x_csrf_token.encode("utf-8"), session["csrf_token"].encode("ascii")
        ):
            raise HTTPException(403, "The Classroom viewing session has changed")
        with db.connect() as connection:
            row = query(
                connection,
                backend,
                "SELECT captured_at,payload_blob FROM web_classroom_connections WHERE user_id=%s",
                (session["user_id"],),
            ).fetchone()
        recent = (
            row is not None
            and row[0] is not None
            and (row[0] >= int(db.clock() * 1000) - _MAX_SNAPSHOT_AGE_MS)
        )
        return {"connected": row is not None, "snapshot": db.unseal(row[1]) if recent else None}

    @app.post("/api/classroom/connection", status_code=201)
    def connect(session=Depends(require_session)):
        token = secrets.token_urlsafe(32)
        with db.connect() as connection:
            result = query(
                connection,
                backend,
                """INSERT INTO web_classroom_connections(user_id,token_hash)
                SELECT id,%s FROM web_users WHERE id=%s AND active=1
                ON CONFLICT(user_id) DO NOTHING""",
                (digest(token), session["user_id"]),
            )
            if not result.rowcount:
                raise HTTPException(409, "Classroom connection already exists")
        return {"token": token}

    @app.delete("/api/classroom/connection", status_code=204)
    def disconnect(session=Depends(require_session)):
        with db.connect() as connection:
            query(
                connection,
                backend,
                "DELETE FROM web_classroom_connections WHERE user_id=%s",
                (session["user_id"],),
            )

    @app.post("/api/classroom/import", status_code=204)
    def upload(body: ClassroomSnapshot, authorization: str = Header(default="")):
        matched = _TOKEN.fullmatch(authorization)
        if not matched:
            raise HTTPException(401, "Invalid Classroom connection")
        now = int(db.clock() * 1000)
        if not now - _MAX_SNAPSHOT_AGE_MS <= body.captured_at <= now + 300_000:
            raise HTTPException(422, "Invalid Classroom sync time")
        token_hash = digest(matched[1])
        blob = db.seal(body.model_dump(mode="json"))
        with db.connect() as connection:
            # Compare and update atomically. Rotated tokens and older retries cannot win.
            result = query(
                connection,
                backend,
                """UPDATE web_classroom_connections SET captured_at=%s,payload_blob=%s
                WHERE token_hash=%s AND (captured_at IS NULL OR captured_at<%s)
                AND user_id IN (SELECT id FROM web_users WHERE active=1)""",
                (body.captured_at, blob, token_hash, body.captured_at),
            )
            if (
                result.rowcount == 0
                and query(
                    connection,
                    backend,
                    """SELECT 1 FROM web_classroom_connections
                WHERE token_hash=%s AND user_id IN (SELECT id FROM web_users WHERE active=1)""",
                    (token_hash,),
                ).fetchone()
                is None
            ):
                raise HTTPException(401, "Invalid Classroom connection")
