"""Per-device Web Push HTTP endpoints."""

from fastapi import Depends, HTTPException

from .push import Subscription


def install_push_routes(app, db, integration, require):
    @app.get("/api/push")
    def push_info(session=Depends(require)):
        device = db.session_device(session)
        return {
            "configured": integration.sender.configured,
            "public_key": integration.sender.public,
            "registered": bool(db.subscriptions(device["id_hash"])),
            "background_enabled": bool(device["background"]),
        }

    @app.post("/api/push", status_code=201)
    def subscribe(body: Subscription, session=Depends(require)):
        if not integration.sender.configured:
            raise HTTPException(503, "Push is not configured")
        device = db.session_device(session)
        if not device["background"]:
            raise HTTPException(409, "Sign in with device connection enabled first")
        db.subscribe(device, body.model_dump())
        return {"saved": True}

    @app.delete("/api/push", status_code=204)
    def unsubscribe(session=Depends(require)):
        db.unsubscribe(db.session_device(session)["id_hash"])

    @app.post("/api/push/test")
    def test_push(session=Depends(require)):
        device = db.session_device(session)
        subscriptions = db.subscriptions(device["id_hash"])
        if not integration.sender.configured or not subscriptions:
            raise HTTPException(409, "Enable notifications on this device first")
        if not db.allow("push-test:" + device["id_hash"], 1):
            raise HTTPException(429, "Wait a minute before testing again")
        outcomes = [
            integration.sender.send(
                s["subscription"],
                {
                    "title": "CalendarSync",
                    "body": "테스트 알림이 도착했어요.",
                    "url": "/",
                    "tag": "calendarsync-test",
                },
            )
            for s in subscriptions
        ]
        return {
            "accepted": outcomes.count("sent"),
            "failed": len(outcomes) - outcomes.count("sent"),
        }
