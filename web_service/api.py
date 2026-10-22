from datetime import date, datetime, timedelta, timezone
import hmac

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .identity import MicrosoftIdentity, Unavailable, validate_identity
from .schemas import ChannelPreference, EventState, Preferences, TaskInput, TaskState, TaskUpdate
from .storage import Conflict, Database, Missing

SESSION_COOKIE = "calendarsync_session"
FLOW_COOKIE = "calendarsync_login"
KST = timezone(timedelta(hours=9))


def create_app(settings=None, database=None, identity=None, frontend_directory=None):
    settings = settings or Settings.from_env()
    db = database or Database(settings.database_path, settings.data_key)
    db.initialize()
    provider = identity or MicrosoftIdentity(settings)
    app = FastAPI(title="CalendarSync Web Foundation", version="0.1.0")
    app.state.database = db

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Missing)
    async def missing(_request, _error):
        return JSONResponse({"detail": "Item not found"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_request, _error):
        return JSONResponse({"detail": "Item changed on another device; reload before editing"}, status_code=409)

    def current_session(request: Request):
        session = db.session(request.cookies.get(SESSION_COOKIE))
        if not session or (settings.tenant_id and session["tenant_id"] != settings.tenant_id):
            raise HTTPException(401, "Sign in with your school account")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and origin != settings.origin:
                raise HTTPException(403, "Invalid request origin")
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), session["csrf_token"].encode("ascii")):
                raise HTTPException(403, "A valid CSRF token is required")
        return session

    app.state.require_session = current_session

    def cookie(response, name, value, max_age):
        response.set_cookie(name, value, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=max_age, path="/")

    def window(start, end):
        if end < start or (end - start).days > 366:
            raise HTTPException(422, "Query range must be ordered and no longer than 367 dates")
        return start.isoformat(), end.isoformat()

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "web-foundation"}

    @app.get("/")
    def index():
        if frontend_directory is not None:
            return FileResponse(frontend_directory / "index.html")
        return {"service": "CalendarSync Web Foundation", "sign_in": "/auth/login", "api_docs": "/docs", "ui_connected": False}

    if frontend_directory is not None:
        app.mount("/assets", StaticFiles(directory=frontend_directory), name="web-assets")

    @app.get("/auth/login")
    def login():
        try:
            flow = provider.begin()
        except Exception:
            # Do not return provider errors, tokens, or credential configuration.
            raise HTTPException(503, "School sign-in is not available yet") from None
        raw = db.store_flow(flow)
        response = RedirectResponse(flow["auth_uri"], status_code=303)
        cookie(response, FLOW_COOKIE, raw, 600)
        return response

    @app.get("/auth/callback")
    def callback(request: Request):
        try:
            if len(request.url.query) > 16000:
                raise ValueError("Invalid authorization response")
            query = dict(request.query_params)
            if any(len(request.query_params.getlist(k)) != 1 for k in query):
                raise ValueError("Duplicate authorization fields")
            flow = db.consume_flow(request.cookies.get(FLOW_COOKIE))
            claims = provider.finish(flow, query)
            tenant, oid, profile = validate_identity(claims, settings, db.clock())
            user_id = db.save_user(tenant, oid, profile)
            old_session = request.cookies.get(SESSION_COOKIE)
            if old_session:
                db.revoke_session(old_session)
            raw = db.issue_session(user_id, settings.session_seconds)
        except Unavailable:
            return JSONResponse({"detail": "School sign-in is not available yet"}, status_code=503)
        except Exception:
            response = JSONResponse({"detail": "Sign-in failed; start again"}, status_code=400)
            response.delete_cookie(FLOW_COOKIE, path="/")
            return response
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/")
        cookie(response, SESSION_COOKIE, raw, settings.session_seconds)
        return response

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request, session=Depends(current_session)):
        db.revoke_session(request.cookies[SESSION_COOKIE])
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/me")
    def me(session=Depends(current_session)):
        return {"id": session["user_id"], **db.unseal(session["profile_blob"]), "csrf_token": session["csrf_token"]}

    @app.get("/api/channels")
    def channels(session=Depends(current_session)):
        return db.channels(session["user_id"], session["tenant_id"])

    @app.put("/api/channels/{channel_id}/preference")
    def channel_preference(channel_id: str, body: ChannelPreference, session=Depends(current_session)):
        db.channel_preference(session["user_id"], session["tenant_id"], channel_id, body.enabled)
        return {"id": channel_id, "enabled": body.enabled}

    @app.get("/api/settings")
    def preferences(session=Depends(current_session)):
        return {"preferences": db.preferences(session["user_id"]), "capabilities": {"push_delivery": False, "teams_chat_delivery": False, "calendar_export": False, "live_teams_ingestion": False}}

    @app.put("/api/settings")
    def save_preferences(body: Preferences, session=Depends(current_session)):
        if body.calendar_sync_enabled or body.teams_chat_enabled:
            raise HTTPException(409, "Delivery integrations are not connected in this foundation")
        return db.save_preferences(session["user_id"], body.model_dump())

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskInput, session=Depends(current_session)):
        return db.add_task(session["user_id"], body.model_dump(mode="json"))

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, session=Depends(current_session)):
        return db.task(session["user_id"], task_id)

    @app.put("/api/tasks/{task_id}")
    def update_task(task_id: str, body: TaskUpdate, session=Depends(current_session)):
        payload = body.model_dump(mode="json", exclude={"version"})
        return db.update_task(session["user_id"], task_id, payload, body.version)

    @app.patch("/api/tasks/{task_id}/state")
    def task_state(task_id: str, body: TaskState, session=Depends(current_session)):
        return db.task_state(session["user_id"], task_id, body.model_dump(exclude={"version"}), body.version)

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str, version: int = Query(ge=1), session=Depends(current_session)):
        db.delete_task(session["user_id"], task_id, version)
        return Response(status_code=204)

    @app.patch("/api/events/{event_id}/state")
    def event_state(event_id: str, body: EventState, session=Depends(current_session)):
        result = db.event_state(session["user_id"], session["tenant_id"], event_id, body.model_dump())
        return {k: v for k, v in result.items() if k != "channel_enabled"}

    @app.get("/api/calendar")
    def calendar(start: date, end: date, session=Depends(current_session)):
        lo, hi = window(start, end)
        return {"start": lo, "end": hi, "items": db.calendar(session["user_id"], session["tenant_id"], lo, hi)}

    @app.get("/api/hidden")
    def hidden(start: date, end: date, session=Depends(current_session)):
        lo, hi = window(start, end)
        return {"items": db.calendar(session["user_id"], session["tenant_id"], lo, hi, hidden_only=True)}

    @app.get("/api/today")
    def today(day: date | None = None, session=Depends(current_session)):
        selected = day or datetime.fromtimestamp(db.clock(), KST).date()
        return db.agenda(session["user_id"], session["tenant_id"], selected.isoformat())

    return app
