"""Account-scoped Classroom imports from a user's Apps Script."""

import base64
import binascii
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
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
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


class ClassroomCourse(Input):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)


class ClassroomSnapshot(Input):
    captured_at: int = Field(ge=1, strict=True)
    items: list[ClassroomItem] = Field(max_length=24)
    courses: list[ClassroomCourse] = Field(default_factory=list, max_length=500)


class ClassroomCoursePreference(Input):
    key: str = Field(min_length=1, max_length=140)
    hidden: bool


def _course_id(item):
    if course_id := item.get("course_id"):
        return course_id
    # Existing snapshots contain the encoded numeric Classroom ID in their links.
    path = urlsplit(item.get("url") or "").path.split("/")
    if len(path) > 2 and re.fullmatch(r"[A-Za-z0-9_-]{1,172}", path[2]):
        slug = path[2]
        try:
            decoded = base64.urlsafe_b64decode(slug + "=" * (-len(slug) % 4))
            if decoded.isdigit() and len(decoded) <= 128:
                return decoded.decode("ascii")
        except binascii.Error:
            pass
    return None


def _visible_courses(snapshot, hidden):
    courses = {"id:" + course["id"]: course["name"] for course in snapshot.get("courses", [])}
    items = [(item, _course_id(item)) for item in snapshot["items"]]
    for item, course_id in items:
        if course_id:
            courses.setdefault("id:" + course_id, item["course"])
    keys_by_name = {}
    for key, name in courses.items():
        keys_by_name.setdefault(name, set()).add(key)
    visible = []
    for item, course_id in items:
        name = item["course"]
        if course_id:
            key = "id:" + course_id
        else:
            same_name = keys_by_name.get(name, ())
            key = next(iter(same_name)) if len(same_name) == 1 else "name:" + name
        courses.setdefault(key, name)
        if key not in hidden:
            visible.append(item)
    options = [
        {"key": key, "name": name, "hidden": key in hidden} for key, name in courses.items()
    ]
    return {"captured_at": snapshot["captured_at"], "items": visible}, options


def install_classroom_routes(app, db, require_session, google=None):
    backend = "sqlite" if hasattr(db, "path") else "postgres"
    with db.connect() as connection:
        connection.execute(
            "SELECT user_id,token_hash,captured_at,payload_blob "
            "FROM web_classroom_connections WHERE 1=0"
        )
        connection.execute("SELECT user_id,course_key FROM web_classroom_hidden_courses WHERE 1=0")

    @app.get("/api/classroom")
    def classroom(
        session=Depends(require_session), x_csrf_token: str | None = Header(default=None)
    ):
        # An open tab can outlive the cookie that established its identity.
        if x_csrf_token is not None and not secrets.compare_digest(
            x_csrf_token.encode("utf-8"), session["csrf_token"].encode("ascii")
        ):
            raise HTTPException(403, "The Classroom viewing session has changed")
        return classroom_state(session["user_id"])

    def raw_state(user_id):
        if google and (status := google.status(user_id)) is not None:
            return status
        with db.connect() as connection:
            row = query(
                connection,
                backend,
                "SELECT captured_at,payload_blob FROM web_classroom_connections WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        recent = (
            row is not None
            and row[0] is not None
            and (row[0] >= int(db.clock() * 1000) - _MAX_SNAPSHOT_AGE_MS)
        )
        return {"connected": row is not None, "snapshot": db.unseal(row[1]) if recent else None}

    def classroom_state(user_id):
        status = raw_state(user_id)
        if status["snapshot"] is None:
            return status
        with db.connect() as connection:
            hidden = {
                row[0]
                for row in query(
                    connection,
                    backend,
                    "SELECT course_key FROM web_classroom_hidden_courses WHERE user_id=%s",
                    (user_id,),
                ).fetchall()
            }
        snapshot, courses = _visible_courses(status["snapshot"], hidden)
        return {**status, "snapshot": snapshot, "courses": courses}

    @app.put("/api/classroom/courses/preference")
    def course_preference(body: ClassroomCoursePreference, session=Depends(require_session)):
        user_id = session["user_id"]
        if body.key not in {course["key"] for course in classroom_state(user_id).get("courses", [])}:
            raise HTTPException(404, "Classroom course is unavailable")
        with db.connect() as connection:
            if body.hidden:
                query(
                    connection,
                    backend,
                    """INSERT INTO web_classroom_hidden_courses(user_id,course_key)
                    SELECT id,%s FROM web_users WHERE id=%s AND active=1
                    ON CONFLICT(user_id,course_key) DO NOTHING""",
                    (body.key, user_id),
                )
            else:
                query(
                    connection,
                    backend,
                    "DELETE FROM web_classroom_hidden_courses WHERE user_id=%s AND course_key=%s",
                    (user_id, body.key),
                )
        return classroom_state(user_id)

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
