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

from .calendar_rules import KST
from .classroom_routes import _MAX_SNAPSHOT_AGE_MS, ClassroomSnapshot
from .ports import EventExtractor
from .schema_inspection import query
from .schemas import Input
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
_SUBMISSION_STATES = {
    "NEW": "working",
    "CREATED": "working",
    "TURNED_IN": "submitted",
    "RETURNED": "returned",
    "RECLAIMED_BY_STUDENT": "reclaimed",
}
_MAX_NEW_NOTICE_ANALYSES = 4


class AnnouncementAnalysisPreference(Input):
    enabled: bool


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


def _read_submission_statuses(client, token, assignments, deadline):
    by_course = {}
    for assignment in assignments:
        if work_id := assignment.get("assignment_id"):
            by_course.setdefault(assignment["course_id"], {})[work_id] = assignment
    for course_id, pending in by_course.items():
        try:
            for submission in _pages(
                client,
                token,
                f"courses/{quote(course_id, safe='')}/courseWork/-/studentSubmissions",
                {
                    "userId": "me",
                    "pageSize": 100,
                    "fields": "studentSubmissions(courseWorkId,state),nextPageToken",
                },
                "studentSubmissions",
                deadline,
            ):
                work_id = submission.get("courseWorkId")
                if work_id in pending:
                    pending.pop(work_id)["submission_status"] = _SUBMISSION_STATES.get(
                        submission.get("state")
                    )
                    if not pending:
                        break
        except (GoogleAccessExpired, httpx.HTTPError, TimeoutError, ValueError):
            # A submission lookup failure leaves the course work visible with unknown status.
            continue


def _analyze_announcements(extractor, notices, previous, can_send):
    cached = {(item["course_id"], item["notice_id"]): item for item in previous}
    analysis = []
    attempted = 0
    for item, text, created_at in notices:
        notice_id = item.get("notice_id")
        if not notice_id or not text or not created_at:
            continue
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                continue
        except (ValueError, TypeError):
            continue
        course_id = item["course_id"]
        published = created.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        prompt = f"공지 작성일 (한국 시간): {published}\n공지 본문:\n{text}"
        version = hashlib.sha256(prompt[:16000].encode()).hexdigest()
        old = cached.get((course_id, notice_id))
        if old is not None and old["version"] == version:
            events = old["events"]
        else:
            if attempted >= _MAX_NEW_NOTICE_ANALYSES or not can_send(course_id):
                continue
            attempted += 1
            try:
                events = extractor.extract(prompt, [], [])
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                continue
        analysis.append({
            "course_id": course_id,
            "notice_id": notice_id,
            "course": item["course"],
            "url": item["url"],
            "version": version,
            "events": events,
        })
    return analysis


def _snapshot(client, token, captured_at, analyze=None):
    notices, assignments = [], []
    notice_content = {}
    active_courses = []
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
        course_id = course["id"]
        active_courses.append({"id": course_id, "name": name})
        path = f"courses/{quote(course_id, safe='')}/"
        for segment, field, kind, fields, output in (
            (
                "announcements",
                "announcements",
                "announcement",
                "id,text,alternateLink,creationTime,updateTime"
                if analyze else "text,alternateLink,updateTime",
                notices,
            ),
            (
                "courseWork",
                "courseWork",
                "assignment",
                "id,title,description,alternateLink,updateTime,dueDate,dueTime,workType",
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
                entry = {
                    "course": name,
                    "course_id": course_id,
                    "kind": kind,
                    "assignment_id": item.get("id") if kind == "assignment" else None,
                    "title": _text(item.get(title_key), 240)
                    or ("수업 공지" if kind == "announcement" else "과제"),
                    "description": _text(item.get("description"), 160),
                    "url": _link(item.get("alternateLink")),
                    "due": _due(item),
                }
                if analyze and kind == "announcement" and (notice_id := item.get("id")):
                    entry["notice_id"] = notice_id
                    notice_content[(course_id, notice_id)] = (
                        (item.get("text") or "")[:16000],
                        item.get("creationTime"),
                    )
                output.append((item.get("updateTime", ""), entry))
                count += 1
                if count == 12:
                    break
    latest_notices = [item for _, item in sorted(notices, key=lambda pair: pair[0], reverse=True)[:12]]
    latest_assignments = [
        item for _, item in sorted(assignments, key=lambda pair: pair[0], reverse=True)[:12]
    ]
    _read_submission_statuses(client, token, latest_assignments, deadline)
    analysis = []
    if analyze:
        analysis = analyze([
            (item, *notice_content[(item["course_id"], item["notice_id"])])
            for item in latest_notices
            if item.get("notice_id")
        ])
    return ClassroomSnapshot(
        captured_at=captured_at,
        items=latest_notices + latest_assignments,
        courses=active_courses,
        analysis=analysis,
    ).model_dump(mode="json")


class GoogleClassroom:
    def __init__(self, settings, db, extractor: EventExtractor | None = None):
        self.settings = settings
        self.db = db
        self.extractor = extractor
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
            """SELECT state,captured_at,snapshot_blob,credential_blob FROM web_classroom_google
            WHERE user_id=%s AND state<>'pending'""",
            user,
        )
        if not rows:
            return None
        state, captured_at, blob, credential_blob = rows[0]
        recent = (
            captured_at is not None
            and captured_at >= int(self.db.clock() * 1000) - _MAX_SNAPSHOT_AGE_MS
        )
        snapshot = self.db.unseal(blob) if recent else None
        analysis_available = bool(self.extractor and self.extractor.configured and state == "connected")
        analysis_enabled = analysis_available and (
            self.db.unseal(credential_blob).get("announcement_analysis") is True
        )
        if snapshot and not analysis_enabled:
            snapshot.pop("analysis", None)
        result = {
            "connected": True,
            "method": "google",
            "reauth": state == "reauth",
            "snapshot": snapshot,
        }
        if analysis_available:
            result["announcement_analysis"] = analysis_enabled
        return result

    def set_announcement_analysis(self, user, enabled):
        if not self.extractor or not self.extractor.configured:
            raise HTTPException(409, "Classroom announcement analysis is unavailable")
        for _ in range(3):
            rows = self._execute(
                """SELECT connection_id,credential_blob,snapshot_blob,next_sync_at
                FROM web_classroom_google WHERE user_id=%s AND state='connected'""",
                user,
            )
            if not rows:
                raise HTTPException(409, "Connect Google Classroom first")
            connection_id, blob, snapshot_blob, next_sync_at = rows[0]
            credentials = self.db.unseal(blob)
            if (credentials.get("announcement_analysis") is True) == enabled:
                return False
            credentials["announcement_analysis"] = enabled
            if not enabled and snapshot_blob:
                snapshot = self.db.unseal(snapshot_blob)
                snapshot.pop("analysis", None)
                snapshot_blob = self.db.seal(snapshot)
            if self._execute(
                """UPDATE web_classroom_google SET credential_blob=%s,snapshot_blob=%s,next_sync_at=0
                WHERE user_id=%s AND connection_id=%s AND state='connected'
                AND credential_blob=%s AND next_sync_at=%s
                AND user_id IN (SELECT id FROM web_users WHERE active=1)""",
                self.db.seal(credentials), snapshot_blob, user, connection_id, blob, next_sync_at,
            ):
                return True
        raise HTTPException(409, "Classroom connection changed; try again")

    def sync(self, user, access=None):
        analysis_deadline = time.monotonic() + 80
        now = int(self.db.clock())
        lease = now + _CLAIM_SECONDS
        rows = self._execute(
            """UPDATE web_classroom_google SET next_sync_at=%s WHERE user_id=%s AND state='connected'
            AND next_sync_at<=%s AND user_id IN (SELECT id FROM web_users WHERE active=1)
            RETURNING connection_id,credential_blob,snapshot_blob""",
            lease,
            user,
            now,
        )
        if not rows:
            return
        connection_id, credential_blob, previous_blob = rows[0]
        try:
            credentials = self.db.unseal(credential_blob)
            refresh = credentials["refresh_token"]
            with self.client_factory() as client:
                if not access:
                    tokens = self._token(
                        client, {"grant_type": "refresh_token", "refresh_token": refresh}
                    )
                    access = tokens["access_token"]
                    replacement = tokens.get("refresh_token")
                    if isinstance(replacement, str) and replacement:
                        replacement_blob = self.db.seal({**credentials, "refresh_token": replacement})
                        if not self._execute(
                            """UPDATE web_classroom_google SET credential_blob=%s
                            WHERE user_id=%s AND connection_id=%s AND next_sync_at=%s
                            AND credential_blob=%s""",
                            replacement_blob,
                            user,
                            connection_id,
                            lease,
                            credential_blob,
                        ):
                            return
                        credential_blob = replacement_blob
                analyze = None
                extractor = self.extractor
                if extractor and extractor.configured and credentials.get("announcement_analysis") is True:
                    previous = self.db.unseal(previous_blob).get("analysis", []) if previous_blob else []

                    def can_send(course_id):
                        return time.monotonic() < analysis_deadline and bool(self._execute(
                            """SELECT 1 FROM web_classroom_google g JOIN web_users u ON u.id=g.user_id
                            WHERE g.user_id=%s AND g.connection_id=%s AND g.next_sync_at=%s
                            AND g.credential_blob=%s AND g.state='connected' AND u.active=1
                            AND NOT EXISTS (SELECT 1 FROM web_classroom_hidden_courses h
                                WHERE h.user_id=g.user_id AND h.course_key=%s)""",
                            user, connection_id, lease, credential_blob, "id:" + course_id,
                        ))

                    def analyze(notices):
                        return _analyze_announcements(extractor, notices, previous, can_send)

                snapshot = _snapshot(client, access, int(self.db.clock() * 1000), analyze)
            self._execute(
                """UPDATE web_classroom_google SET captured_at=%s,snapshot_blob=%s,next_sync_at=%s
                WHERE user_id=%s AND connection_id=%s AND next_sync_at=%s AND credential_blob=%s""",
                snapshot["captured_at"],
                self.db.seal(snapshot),
                now + _INTERVAL,
                user,
                connection_id,
                lease,
                credential_blob,
            )
        except GoogleAccessExpired:
            self._execute(
                """UPDATE web_classroom_google SET state='reauth',credential_blob=NULL,
                captured_at=NULL,snapshot_blob=NULL WHERE user_id=%s AND connection_id=%s
                AND next_sync_at=%s AND credential_blob=%s""",
                user,
                connection_id,
                lease,
                credential_blob,
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

    @app.put("/api/classroom/google/announcement-analysis")
    def announcement_analysis(
        body: AnnouncementAnalysisPreference, tasks: BackgroundTasks, session=Depends(require)
    ):
        user = session["user_id"]
        if service.set_announcement_analysis(user, body.enabled) and body.enabled:
            tasks.add_task(service.sync, user)
        return {"enabled": body.enabled}

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
