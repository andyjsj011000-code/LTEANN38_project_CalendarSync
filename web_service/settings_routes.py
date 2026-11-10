"""Preference HTTP routes; capabilities come from the injected integration."""

from fastapi import Depends, FastAPI, HTTPException

from .personal_ports import PreferenceStore
from .request_dependencies import SessionDependency, SessionIdentity
from .schemas import ChannelPreference, Preferences


def install_settings_routes(
    app: FastAPI, db: PreferenceStore, require_session: SessionDependency, integration=None
) -> None:
    """Bind settings routes to the same authentication boundary as other APIs."""

    @app.put("/api/channels/{channel_id}/preference")
    def channel_preference(
        channel_id: str,
        body: ChannelPreference,
        session: SessionIdentity = Depends(require_session),
    ):
        db.channel_preference(session["user_id"], session["tenant_id"], channel_id, body.enabled)
        return {"id": channel_id, "enabled": body.enabled}

    @app.get("/api/settings")
    def preferences(session: SessionIdentity = Depends(require_session)):
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

    # 아직 연결되지 않은 외부 전송 기능은 켤 수 없게 하고 검증된 사용자 설정만 저장한다.
    @app.put("/api/settings")
    def save_preferences(body: Preferences, session: SessionIdentity = Depends(require_session)):
        if body.calendar_sync_enabled or body.teams_chat_enabled:
            raise HTTPException(409, "Delivery integrations are not connected in this foundation")
        return db.save_preferences(session["user_id"], body.to_values())
