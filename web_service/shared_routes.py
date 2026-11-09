"""Account and shared-calendar HTTP reads and event state updates."""

from datetime import date, datetime

from fastapi import Depends, HTTPException

from .calendar_rules import KST
from .schemas import EventState


def install_shared_routes(app, settings, db, current_session, integration):
    def teams_read(session, operation):
        if integration and hasattr(integration, "read_teams"):
            return integration.read_teams(session, operation)
        return operation()

    def window(start, end):
        if end < start or (end - start).days > 366:
            raise HTTPException(422, "Query range must be ordered and no longer than 367 dates")
        return start.isoformat(), end.isoformat()

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
