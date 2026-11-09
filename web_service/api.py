from datetime import date, datetime

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .calendar_rules import KST
from .config import Settings
from .identity import MicrosoftIdentity, Unavailable, validate_identity
from .personal_routes import install_personal_routes
from .request_dependencies import create_session_dependency
from .schemas import EventState
from .settings_routes import install_settings_routes
from .storage import Conflict, Database, Missing, PermissionPending

SESSION_COOKIE = "calendarsync_session"
FLOW_COOKIE = "calendarsync_login"


def create_app(
    settings=None,
    database=None,
    identity=None,
    frontend_directory=None,
    integration=None,
    frontend_entry="index.html",
):
    settings = settings or Settings.from_env()
    session_cookie = (
        "__Host-calendarsync_session" if integration and settings.secure_cookies else SESSION_COOKIE
    )
    flow_cookie = (
        "__Host-calendarsync_login" if integration and settings.secure_cookies else FLOW_COOKIE
    )
    db = database or Database(settings.database_path, settings.data_key)
    db.initialize()
    provider = identity or MicrosoftIdentity(settings)
    app = FastAPI(
        title="CalendarSync",
        version="0.2.0",
        docs_url=None if integration else "/docs",
        redoc_url=None if integration else "/redoc",
        openapi_url=None if integration else "/openapi.json",
    )
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
        return JSONResponse(
            {"detail": "Item changed on another device; reload before editing"}, status_code=409
        )

    @app.exception_handler(PermissionPending)
    async def permission_pending(_request, _error):
        return JSONResponse(
            {
                "detail": "Teams 접근 권한을 확인하고 있어요. 잠시 후 다시 시도해 주세요.",
                "code": "permission_check_pending",
                "connection_state": _error.connection_state,
                "retryable": _error.retryable,
                "teams_access_unverified": True,
                **({"retain_teams_snapshot": True} if _error.retain_snapshot else {}),
                **(
                    {"snapshot_channel_ids": _error.snapshot_channels}
                    if _error.snapshot_channels is not None
                    else {}
                ),
            },
            status_code=503,
            headers={"Retry-After": "2"},
        )

    current_session = create_session_dependency(settings, db, session_cookie, integration)
    app.state.require_session = current_session

    def teams_read(session, operation):
        if integration and hasattr(integration, "read_teams"):
            return integration.read_teams(session, operation)
        return operation()

    def cookie(response, name, value, max_age):
        response.set_cookie(
            name,
            value,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            max_age=max_age,
            path="/",
        )

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
            return FileResponse(frontend_directory / frontend_entry)
        return {
            "service": "CalendarSync Web Foundation",
            "sign_in": "/auth/login",
            "api_docs": "/docs",
            "ui_connected": False,
        }

    if frontend_directory is not None:
        app.mount("/assets", StaticFiles(directory=frontend_directory), name="web-assets")

    @app.get("/auth/login")
    def login(request: Request):
        if integration:
            integration.limit_login(request)
        try:
            flow = (
                integration.begin_login(provider, request)
                if integration and hasattr(integration, "begin_login")
                else provider.begin()
            )
        except Exception:
            # Do not return provider errors, tokens, or credential configuration.
            raise HTTPException(503, "School sign-in is not available yet") from None
        raw = db.store_flow(flow)
        response = RedirectResponse(flow["auth_uri"], status_code=303)
        cookie(response, flow_cookie, raw, 600)
        return response

    @app.get("/auth/callback")
    def callback(request: Request):
        raw = None
        try:
            if integration:
                integration.limit_login(request)
            if len(request.url.query) > 16000:
                raise ValueError("Invalid authorization response")
            query = dict(request.query_params)
            if any(len(request.query_params.getlist(k)) != 1 for k in query):
                raise ValueError("Duplicate authorization fields")
            flow = db.consume_flow(request.cookies.get(flow_cookie))
            claims = provider.finish(flow, query)
            tenant, oid, profile = validate_identity(claims, settings, db.clock())
            if integration:
                integration.validate_sign_in(claims)
            user_id = db.save_user(tenant, oid, profile)
            old_session = request.cookies.get(session_cookie)
            if old_session:
                db.revoke_session(old_session)
            raw = db.issue_session(user_id, settings.session_seconds)
            if integration:
                if hasattr(integration, "complete_sign_in"):
                    integration.complete_sign_in(raw, user_id, claims, request)
                else:
                    integration.bind_sign_in(raw, user_id, claims)
        except Unavailable:
            return JSONResponse({"detail": "School sign-in is not available yet"}, status_code=503)
        except Exception:
            if raw:
                db.revoke_session(raw)
            response = (
                RedirectResponse("/?login=failed", status_code=303)
                if integration
                else JSONResponse({"detail": "Sign-in failed; start again"}, status_code=400)
            )
            response.delete_cookie(
                flow_cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax"
            )
            return response
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            flow_cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax"
        )
        cookie(response, session_cookie, raw, settings.session_seconds)
        if integration and hasattr(integration, "login_response"):
            integration.login_response(response, raw)
        return response

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request):
        if integration and hasattr(integration, "logout_request"):
            # A persistent device must remain revocable after its viewing session expires.
            integration.logout_request(request)
        else:
            session = current_session(request)
            if integration and hasattr(integration, "explicit_logout"):
                integration.explicit_logout(session)
        raw_session = request.cookies.get(session_cookie)
        if raw_session:
            db.revoke_session(raw_session)
        response = Response(status_code=204)
        response.delete_cookie(
            session_cookie, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax"
        )
        if integration and hasattr(integration, "logout_response"):
            integration.logout_response(response)
        return response

    @app.get("/api/me")
    def me(session=Depends(current_session)):
        return {
            "id": session["user_id"],
            **db.unseal(session["profile_blob"]),
            "csrf_token": session["csrf_token"],
        }

    @app.get("/api/channels")
    def channels(session=Depends(current_session)):
        return teams_read(session, lambda: db.channels(session["user_id"], session["tenant_id"]))

    install_settings_routes(app, db, current_session, integration)
    install_personal_routes(app, db, current_session)

    @app.patch("/api/events/{event_id}/state")
    def event_state(event_id: str, body: EventState, session=Depends(current_session)):
        result = db.event_state(
            session["user_id"], session["tenant_id"], event_id, body.model_dump()
        )
        return {k: v for k, v in result.items() if k != "channel_enabled"}

    @app.get("/api/calendar")
    def calendar(start: date, end: date, session=Depends(current_session)):
        lo, hi = window(start, end)
        return teams_read(
            session,
            lambda: {
                "start": lo,
                "end": hi,
                "items": db.calendar(session["user_id"], session["tenant_id"], lo, hi),
            },
        )

    @app.get("/api/hidden")
    def hidden(start: date, end: date, session=Depends(current_session)):
        lo, hi = window(start, end)
        return teams_read(
            session,
            lambda: {
                "items": db.calendar(
                    session["user_id"], session["tenant_id"], lo, hi, hidden_only=True
                )
            },
        )

    @app.get("/api/today")
    def today(day: date | None = None, session=Depends(current_session)):
        selected = day or datetime.fromtimestamp(db.clock(), KST).date()
        return teams_read(
            session,
            lambda: db.agenda(session["user_id"], session["tenant_id"], selected.isoformat()),
        )

    return app
