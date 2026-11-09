"""Shared-event review and overrides; persistence belongs to the store."""

from fastapi import Depends

from .schemas import Input, TaskInput


class ChangeDecision(Input):
    accept: bool


def install_review_routes(app, db, integration, require):
    @app.get("/api/changes")
    def changes(session=Depends(require)):
        return integration.read_teams(
            session, lambda: {"items": db.changes(session["user_id"], session["tenant_id"])}
        )

    @app.post("/api/changes/{change_id}")
    def decide(change_id: str, body: ChangeDecision, session=Depends(require)):
        db.resolve_change(session["user_id"], session["tenant_id"], change_id, body.accept)
        return {"saved": True}

    @app.put("/api/events/{event_id}/override")
    def override(event_id: str, body: TaskInput, session=Depends(require)):
        db.override_event(
            session["user_id"],
            session["tenant_id"],
            event_id,
            body.model_dump(mode="json", exclude={"display_mode", "selected_dates"}),
        )
        return {"saved": True}
