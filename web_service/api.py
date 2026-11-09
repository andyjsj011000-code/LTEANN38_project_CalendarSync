from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth_routes import install_auth_routes
from .config import Settings
from .identity import MicrosoftIdentity
from .personal_routes import install_personal_routes
from .request_dependencies import create_session_dependency
from .settings_routes import install_settings_routes
from .shared_routes import install_shared_routes
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

    install_auth_routes(
        app, settings, db, provider, current_session, integration, session_cookie, flow_cookie
    )

    install_shared_routes(app, settings, db, current_session, integration)
    install_settings_routes(app, db, current_session, integration)
    install_personal_routes(app, db, current_session)

    return app
