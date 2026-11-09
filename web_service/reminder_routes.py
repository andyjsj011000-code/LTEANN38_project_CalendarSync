"""Reminder HTTP validation and access checks, using pure time rules."""

from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from pydantic import Field, model_validator

from .reminder_rules import future_custom_times
from .schemas import Input
from .storage import Missing


class ReminderRule(Input):
    offsets: list[int] = Field(default_factory=lambda: [-1440, -60], max_length=10)
    custom_times: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def valid_times(self):
        if any(x > 0 or x < -10080 for x in self.offsets):
            raise ValueError("Invalid reminder offset")
        self.offsets = sorted(set(self.offsets))
        parsed = []
        for value in self.custom_times:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("Reminder times must include timezone")
            try:
                parsed.append(dt.astimezone(timezone.utc).isoformat())
            except OverflowError:
                raise ValueError("Reminder time is out of range") from None
        self.custom_times = sorted(set(parsed))
        return self


def install_reminder_routes(app, db, integration, require):
    def accessible_item(session, source, item_id):
        if source == "personal":
            return db.task(session["user_id"], item_id)
        if source != "teams":
            raise Missing()
        integration.run_sync(db.session_device(session), False)
        integration.require_permission_ready(session)
        return next(
            (e for e in db._shared(session["user_id"], session["tenant_id"]) if e["id"] == item_id),
            None,
        ) or (_ for _ in ()).throw(Missing())

    @app.get("/api/reminders/{source}/{item_id}")
    def reminders(source: str, item_id: str, session=Depends(require)):
        accessible_item(session, source, item_id)
        return db.rules(session["user_id"], item_id, source)

    @app.put("/api/reminders/{source}/{item_id}")
    def save_reminders(source: str, item_id: str, body: ReminderRule, session=Depends(require)):
        accessible_item(session, source, item_id)
        now = datetime.fromtimestamp(db.clock(), timezone.utc)
        existing = set(db.rules(session["user_id"], item_id, source)["custom_times"])
        try:
            future = future_custom_times(body.custom_times, existing, now)
        except ValueError:
            raise HTTPException(422, "Custom reminders must be within the next year") from None
        db.save_rules(
            session["user_id"], item_id, source, {**body.model_dump(), "custom_times": future}
        )
        return {"saved": True}
