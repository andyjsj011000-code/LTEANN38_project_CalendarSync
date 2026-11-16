"""Server capabilities, static resources and connection endpoints."""

from pathlib import Path

from fastapi import BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import Field

from .schemas import Input


class Feedback(Input):
    message: str = Field(min_length=1, max_length=4000)


def install_server_routes(app, settings, db, integration, require, static, preview):
    if preview:
        notice_processing = "sample"
        notice_description = "예시 모드에서는 실제 학교 공지를 읽거나 외부로 보내지 않아요."
    elif hasattr(integration.source, "collect"):
        notice_processing = "openai"
        notice_description = "접근 권한이 확인된 Teams 채널의 공지와 답글을 확인합니다. 일정을 추출할 때 본문과, 이미지 분석 설정이 켜져 있으면 지원되는 이미지를 OpenAI API로 보낼 수 있습니다."
    else:
        notice_processing = "existing"
        notice_description = "접근 권한이 확인된 채널에 대해 기존 수집기가 추출한 일정 결과를 읽습니다."

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
            "teams_notice_processing": notice_processing,
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
        page = Path(__file__).parent.parent / "docs" / "TEST_SERVER_PRIVACY.html"
        return HTMLResponse(page.read_text(encoding="utf-8").replace("<!-- teams-notice-processing -->", notice_description))

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
