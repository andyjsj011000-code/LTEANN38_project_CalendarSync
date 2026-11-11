"""Server capabilities, static resources and connection endpoints."""

from pathlib import Path

from fastapi import BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import Field

from .schemas import Input


class Feedback(Input):
    message: str = Field(min_length=1, max_length=4000)


def install_server_routes(app, settings, db, integration, require, static, preview):
    @app.get("/api/assignments")
    def assignments(refresh: bool = False, session=Depends(require)):
        if not integration.assignments_enabled:
            raise HTTPException(404, "Assignments unavailable")
        if refresh and not db.allow("assignment-sync:" + session["user_id"], 2):
            raise HTTPException(429, "Wait before syncing again")
        return app.state.assignments.read(session, force=refresh)

    @app.get("/api/service")
    def info():
        return {
            "mode": "preview" if preview else "test-server",
            "school_login": not preview,
            "synthetic_data_only": preview,
            "idle_seconds": settings.idle_seconds,
            "shared_channel_ingestion": integration.shared_ingestion_enabled,
            "personal_agenda": True,
            "assignments_enabled": integration.assignments_enabled,
            **({"classroom_enabled": True} if settings.classroom_enabled and not preview else {}),
            **(
                {"classroom_google_enabled": True}
                if settings.google_classroom_configured and not preview
                else {}
            ),
            "planner_enhancements": bool(getattr(db, "planner_enhancements_enabled", False)),
        }

    @app.get("/service-worker.js")
    def service_worker():
        return FileResponse(
            static / "service-worker.js",
            media_type="text/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
        )

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(static / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/privacy")
    def privacy():
        return FileResponse(
            Path(__file__).parent.parent / "docs" / "TEST_SERVER_PRIVACY.md",
            media_type="text/plain; charset=utf-8",
        )

    @app.post("/api/teams/sync")
    def sync(background_tasks: BackgroundTasks, session=Depends(require)):
        if not db.allow("manual-sync:" + session["user_id"], 2):
            raise HTTPException(429, "Wait before syncing again")
        return integration.sync(session, background_tasks)

    @app.get("/api/teams/ingestion-status")
    def ingestion_status(session=Depends(require)):
        if not getattr(db, "planner_enhancements_enabled", False):
            raise HTTPException(404, "Not available")
        return db.ingestion_status(db.session_device(session))

    @app.post("/auth/logout-all", status_code=204)
    def logout_all(session=Depends(require)):
        db.revoke_all(session["user_id"])
        response = Response(status_code=204)
        response.delete_cookie(
            integration.session_cookie,
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )
        integration.logout_response(response)
        return response

    @app.post("/api/feedback", status_code=201)
    def feedback(body: Feedback, session=Depends(require)):
        if not db.allow("feedback:" + session["user_id"], 5):
            raise HTTPException(429, "Try again later")
        return {"id": db.save_feedback(session["user_id"], body.message)}
