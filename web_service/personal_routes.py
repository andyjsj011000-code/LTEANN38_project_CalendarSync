"""Personal-task HTTP routes using the application's shared session dependency.

Ownership, revisions and writes stay in storage. Personal agenda reads do not
require a Teams permission proof; the integration still checks each request.
"""

from datetime import date, datetime
from typing import cast

from fastapi import Depends, FastAPI, Query, Response

from .calendar_rules import KST
from .personal_ports import PersonalTaskStore, TaskTrashStore
from .request_dependencies import SessionDependency, SessionIdentity
from .schemas import TaskInput, TaskState, TaskUpdate


def install_personal_routes(
    app: FastAPI, db: PersonalTaskStore, require_session: SessionDependency
) -> None:
    """Register personal routes, including the app's existing planner opt-in."""

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskInput, session: SessionIdentity = Depends(require_session)):
        return db.add_task(session["user_id"], body.to_payload())

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, session: SessionIdentity = Depends(require_session)):
        return db.task(session["user_id"], task_id)

    @app.put("/api/tasks/{task_id}")
    def update_task(
        task_id: str, body: TaskUpdate, session: SessionIdentity = Depends(require_session)
    ):
        payload = body.to_payload()
        return db.update_task(session["user_id"], task_id, payload, body.version)

    @app.patch("/api/tasks/{task_id}/state")
    def task_state(
        task_id: str, body: TaskState, session: SessionIdentity = Depends(require_session)
    ):
        return db.task_state(session["user_id"], task_id, body.to_changes(), body.version)

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(
        task_id: str,
        version: int = Query(ge=1),
        session: SessionIdentity = Depends(require_session),
    ):
        if getattr(db, "planner_enhancements_enabled", False):
            # The existing planner opt-in guarantees this additional capability.
            trash = cast(TaskTrashStore, db)
            trash.trash_task(session["user_id"], task_id, version)
        else:
            db.delete_task(session["user_id"], task_id, version)
        return Response(status_code=204)

    @app.get("/api/personal/today")
    def personal_today(
        day: date | None = None, session: SessionIdentity = Depends(require_session)
    ):
        selected = day or datetime.fromtimestamp(db.clock(), KST).date()
        return db.personal_agenda(session["user_id"], session["tenant_id"], selected.isoformat())

    if getattr(db, "planner_enhancements_enabled", False):
        from .planner_drafts import install_draft_routes
        from .planner_trash import install_trash_routes

        install_draft_routes(app, db, require_session)
        install_trash_routes(app, db, require_session)
