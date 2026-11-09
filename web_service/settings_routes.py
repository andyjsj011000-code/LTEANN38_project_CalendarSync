"""Preference HTTP routes; capabilities come from the injected integration."""

from fastapi import Depends, FastAPI, HTTPException

from .schemas import ChannelPreference, Preferences


def install_settings_routes(app: FastAPI, db, require_session, integration=None) -> None:
    """Bind settings routes to the same authentication boundary as other APIs."""

    @app.put("/api/channels/{channel_id}/preference")
    def channel_preference(
        channel_id: str, body: ChannelPreference, session=Depends(require_session)
    ):
        db.channel_preference(session["user_id"], session["tenant_id"], channel_id, body.enabled)
        return {"id": channel_id, "enabled": body.enabled}

    @app.get("/api/settings")
    def preferences(session=Depends(require_session)):
        return {
            "preferences": db.preferences(session["user_id"]),
            "capabilities": {
                "push_delivery": bool(integration and getattr(integration, "push_enabled", False)),
                "teams_chat_delivery": False,
                "calendar_export": False,
                "live_teams_ingestion": bool(integration and integration.source.configured),
            },
            "connection": integration.status(session) if integration else None,
        }

    @app.put("/api/settings")
    def save_preferences(body: Preferences, session=Depends(require_session)):
        if body.calendar_sync_enabled or body.teams_chat_enabled:
            raise HTTPException(409, "Delivery integrations are not connected in this foundation")
        return db.save_preferences(session["user_id"], body.model_dump())
