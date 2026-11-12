"""Google authorization and periodic reads for the Classroom card."""

import base64
import hashlib
import re
import secrets
import time
from datetime import UTC, date, datetime
from datetime import time as clock_time
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .classroom_routes import _MAX_SNAPSHOT_AGE_MS, ClassroomSnapshot
from .schema_inspection import query
from .storage import Missing

_COURSEWORK_READONLY = "https://www.googleapis.com/auth/classroom.coursework.me.readonly"
SCOPES = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    _COURSEWORK_READONLY,
)
_SCOPE_ALIASES = {
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly": _COURSEWORK_READONLY,
}
_CALLBACK = "/auth/classroom/google/callback"
_TOKEN = "https://oauth2.googleapis.com/token"
_CLASSROOM = "https://classroom.googleapis.com/v1/"
_INTERVAL = 900
_CLAIM_SECONDS = 120
_STATE = re.compile(r"[A-Za-z0-9_-]{43}")


class GoogleAccessExpired(Exception):
    """The user must authorize Classroom again."""


def _token_reply(response):
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Invalid Google token response")
    if payload.get("error") == "invalid_grant" or response.status_code == 401:
        raise GoogleAccessExpired
    response.raise_for_status()
    if not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
        raise ValueError("Google did not issue an access token")
    if "scope" in payload:
        granted = {_SCOPE_ALIASES.get(scope, scope) for scope in payload["scope"].split()}
        if granted != set(SCOPES):
            raise GoogleAccessExpired
    return payload


def _text(value, limit):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]


def _link(value):
    url = urlsplit(value or "")
    return (
        value
        if (
            url.scheme == "https"
            and url.netloc == "classroom.google.com"
            and url.path.startswith("/c/")
            and len(value) <= 320
            and not any(ord(c) < 33 for c in value)
        )
        else ""
    )


def _due(item):
    due = item.get("dueDate")
    if not due:
        return None
    day = date(due["year"], due["month"], due["day"])
    value = item.get("dueTime")
    if value is None:
        return day
    return datetime.combine(
        day, clock_time(*(value.get(key, 0) for key in ("hours", "minutes", "seconds")), tzinfo=UTC)
    )


def _pages(client, token, path, params, field, deadline):
    seen = set()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Classroom sync deadline reached")
        response = client.get(
            _CLASSROOM + path, params=params, headers={"Authorization": "Bearer " + token}
        )
        if response.status_code == 401:
            raise GoogleAccessExpired
        if response.status_code == 403:
            details = response.json().get("error", {})
            reasons = {x.get("reason") for x in details.get("errors", [])}
            if not reasons & {
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "quotaExceeded",
                "dailyLimitExceeded",
            }:
                raise GoogleAccessExpired
        response.raise_for_status()
        page = response.json()
        yield from page.get(field, [])
        next_page = page.get("nextPageToken")
        if not next_page:
            return
        if next_page in seen:
            raise ValueError("Google repeated a page token")
        seen.add(next_page)
        params = {**params, "pageToken": next_page}


def _snapshot(client, token, captured_at):
    notices, assignments = [], []
    deadline = time.monotonic() + 45
    courses = _pages(
        client,
        token,
        "courses",
        {
            "studentId": "me",
            "courseStates": "ACTIVE",
            "pageSize": 100,
            "fields": "courses(id,name),nextPageToken",
        },
        "courses",
        deadline,
    )
    for course in courses:
        name = _text(course.get("name"), 120) or "Classroom"
        path = f"courses/{quote(course['id'], safe='')}/"
        for segment, field, kind, fields, output in (
            (
                "announcements",
                "announcements",
                "announcement",
                "text,alternateLink,updateTime",
                notices,
            ),
            (
                "courseWork",
                "courseWork",
                "assignment",
                "title,description,alternateLink,updateTime,dueDate,dueTime,workType",
                assignments,
            ),
        ):
            params = {
                "orderBy": "updateTime desc",
                "pageSize": 12,
                "fields": f"{field}({fields}),nextPageToken",
            }
            count = 0
            for item in _pages(client, token, path + segment, params, field, deadline):
                if kind == "assignment" and item.get("workType") != "ASSIGNMENT":
                    continue
                title_key = "text" if kind == "announcement" else "title"
                output.append(
                    (
                        item.get("updateTime", ""),
                        {
                            "course": name,
                            "kind": kind,
                            "title": _text(item.get(title_key), 240)
                            or ("수업 공지" if kind == "announcement" else "과제"),
                            "description": _text(item.get("description"), 160),
                            "url": _link(item.get("alternateLink")),
                            "due": _due(item),
                        },
                    )
                )
                count += 1
                if count == 12:
                    break
    items = [
        item
        for group in (notices, assignments)
        for _, item in sorted(group, key=lambda pair: pair[0], reverse=True)[:12]
    ]
    return ClassroomSnapshot(captured_at=captured_at, items=items).model_dump(mode="json")


class GoogleClassroom:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.backend = "sqlite" if hasattr(db, "path") else "postgres"
        self.client_factory = lambda: httpx.Client(timeout=8, follow_redirects=False)
        self.redirect_uri = settings.origin + _CALLBACK
        with db.connect() as connection:
            connection.execute(
                "SELECT user_id,connection_id,state,credential_blob,captured_at,"
                "snapshot_blob,next_sync_at FROM web_classroom_google WHERE 1=0"
            )

    def _execute(self, sql, *values):
        with self.db.connect() as connection:
            result = query(connection, self.backend, sql, values)
            return result.fetchall() if result.description else result.rowcount

    def begin(self, session):
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        user = session["user_id"]
        if not self._execute(
            """INSERT INTO web_classroom_google(user_id,connection_id,state,next_sync_at)
            SELECT id,%s,'pending',0 FROM web_users WHERE id=%s AND active=1
            ON CONFLICT(user_id) DO UPDATE SET connection_id=excluded.connection_id
            WHERE web_classroom_google.state IN ('pending', 'reauth')""",
            state,
            user,
        ):
            raise HTTPException(409, "Google Classroom is already connected")
        raw = self.db.store_flow(
            {
                "kind": "classroom-google",
                "state": state,
                "verifier": verifier,
                "user": user,
                "session": session["csrf_token"],
            }
        )
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.settings.google_classroom_client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "state": state,
                "code_challenge": challenge.decode().rstrip("="),
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent select_account",
            }
        )
        return raw, url

    def finish(self, session, raw, state, code):
        flow = self.db.consume_flow(raw)
        if (
            flow.get("kind") != "classroom-google"
            or not _STATE.fullmatch(state)
            or not secrets.compare_digest(flow["state"], state)
            or flow["user"] != session["user_id"]
            or flow["session"] != session["csrf_token"]
            or not 0 < len(code) <= 2048
        ):
            raise ValueError("Invalid Classroom authorization response")
        user = session["user_id"]
        if not self._execute(
            "SELECT 1 FROM web_classroom_google WHERE user_id=%s AND connection_id=%s AND state IN ('pending', 'reauth')",
            user,
            state,
        ):
            raise ValueError("Stale Classroom authorization response")
        with self.client_factory() as client:
            payload = self._token(
                client,
                {
                    "code": code,
                    "code_verifier": flow["verifier"],
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        refresh = payload.get("refresh_token")
        if not isinstance(refresh, str) or not refresh or not payload.get("scope"):
            raise ValueError("Google did not grant offline Classroom access")
        if not self._execute(
            """UPDATE web_classroom_google SET state='connected',credential_blob=%s,next_sync_at=0,
            captured_at=NULL,snapshot_blob=NULL WHERE user_id=%s AND connection_id=%s
            AND state IN ('pending', 'reauth') AND user_id IN (SELECT id FROM web_users WHERE active=1)""",
            self.db.seal({"refresh_token": refresh}),
            user,
            state,
        ):
            raise ValueError("Classroom authorization was superseded")
        return payload["access_token"]

    def _token(self, client, data):
        return _token_reply(
            client.post(
                _TOKEN,
                data={
                    **data,
                    "client_id": self.settings.google_classroom_client_id,
                    "client_secret": self.settings.google_classroom_client_secret,
                },
            )
        )

    def status(self, user):
        rows = self._execute(
            """SELECT state,captured_at,snapshot_blob FROM web_classroom_google
            WHERE user_id=%s AND state<>'pending'""",
            user,
        )
        if not rows:
            return None
        state, captured_at, blob = rows[0]
        recent = (
            captured_at is not None
            and captured_at >= int(self.db.clock() * 1000) - _MAX_SNAPSHOT_AGE_MS
        )
        return {
            "connected": True,
            "method": "google",
            "reauth": state == "reauth",
            "snapshot": self.db.unseal(blob) if recent else None,
        }

    def sync(self, user, access=None):
        now = int(self.db.clock())
        lease = now + _CLAIM_SECONDS
        rows = self._execute(
            """UPDATE web_classroom_google SET next_sync_at=%s WHERE user_id=%s AND state='connected'
            AND next_sync_at<=%s AND user_id IN (SELECT id FROM web_users WHERE active=1)
            RETURNING connection_id,credential_blob""",
            lease,
            user,
            now,
        )
        if not rows:
            return
        connection_id, credential_blob = rows[0]
        try:
            refresh = self.db.unseal(credential_blob)["refresh_token"]
            with self.client_factory() as client:
                if not access:
                    tokens = self._token(
                        client, {"grant_type": "refresh_token", "refresh_token": refresh}
                    )
                    access = tokens["access_token"]
                    replacement = tokens.get("refresh_token")
                    if isinstance(replacement, str) and replacement:
                        credential_blob = self.db.seal({"refresh_token": replacement})
                        self._execute(
                            """UPDATE web_classroom_google SET credential_blob=%s
                            WHERE user_id=%s AND connection_id=%s AND next_sync_at=%s""",
                            credential_blob,
                            user,
                            connection_id,
                            lease,
                        )
                snapshot = _snapshot(client, access, int(self.db.clock() * 1000))
            self._execute(
                """UPDATE web_classroom_google SET captured_at=%s,snapshot_blob=%s,next_sync_at=%s
                WHERE user_id=%s AND connection_id=%s AND next_sync_at=%s""",
                snapshot["captured_at"],
                self.db.seal(snapshot),
                now + _INTERVAL,
                user,
                connection_id,
                lease,
            )
        except GoogleAccessExpired:
            self._execute(
                """UPDATE web_classroom_google SET state='reauth',credential_blob=NULL,
                captured_at=NULL,snapshot_blob=NULL WHERE user_id=%s AND connection_id=%s AND next_sync_at=%s""",
                user,
                connection_id,
                lease,
            )
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            # The reservation allows a retry after two minutes without overwriting a good snapshot.
            self.db.audit("classroom_google_sync_failed", "classroom")

    def tick(self):
        for (user,) in self._execute(
            """SELECT g.user_id FROM web_classroom_google g JOIN web_users u ON u.id=g.user_id
            WHERE g.state='connected' AND g.next_sync_at<=%s AND u.active=1
            ORDER BY g.next_sync_at,g.user_id LIMIT 5""",
            int(self.db.clock()),
        ):
            self.sync(user)

    def disconnect(self, user):
        rows = self._execute(
            "DELETE FROM web_classroom_google WHERE user_id=%s RETURNING credential_blob",
            user,
        )
        if rows and rows[0][0]:
            try:
                refresh = self.db.unseal(rows[0][0])["refresh_token"]
                with self.client_factory() as client:
                    client.post(
                        "https://oauth2.googleapis.com/revoke", data={"token": refresh}
                    ).raise_for_status()
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.db.audit("classroom_google_revoke_failed", "classroom")


def install_google_classroom(app, service, require):
    secure = service.settings.secure_cookies
    cookie = ("__Host-" if secure else "") + "calendarsync_classroom_flow"

    @app.post("/api/classroom/google/connect")
    def start(response: Response, session=Depends(require)):
        if not service.db.allow("classroom-google:" + session["user_id"], 5):
            raise HTTPException(429, "Try again later")
        raw, url = service.begin(session)
        response.set_cookie(cookie, raw, max_age=600, secure=secure, httponly=True, samesite="lax")
        return {"authorization_url": url}

    @app.get(_CALLBACK)
    def callback(request: Request, tasks: BackgroundTasks, session=Depends(require)):
        result = "failed"
        try:
            params = request.query_params
            if len(request.url.query) > 8192 or any(
                len(params.getlist(key)) != 1 for key in params
            ):
                raise ValueError("Invalid Classroom authorization response")
            access = service.finish(
                session,
                request.cookies.get(cookie),
                params.get("state", ""),
                params.get("code", ""),
            )
            tasks.add_task(service.sync, session["user_id"], access)
            result = "connected"
        except (
            Missing,
            GoogleAccessExpired,
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            pass
        response = RedirectResponse(f"/?classroom={result}", status_code=303)
        response.delete_cookie(cookie, secure=secure, httponly=True, samesite="lax")
        return response

    @app.delete("/api/classroom/google/connection", status_code=204)
    def disconnect(response: Response, session=Depends(require)):
        service.disconnect(session["user_id"])
        response.delete_cookie(cookie, secure=secure, httponly=True, samesite="lax")
