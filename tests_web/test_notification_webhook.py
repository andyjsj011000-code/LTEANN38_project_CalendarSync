import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_service.notification_queue import NotificationQueue
from web_service.notification_webhook import create_notification_router


def test_challenge_and_authenticated_enqueue(tmp_path):
    q = NotificationQueue(lambda: sqlite3.connect(tmp_path / "notifications.sqlite3"))
    q.register("sub", "correct-state", "tenant", "team", "channel")
    app = FastAPI()
    app.include_router(create_notification_router(q))
    with TestClient(app) as client:
        response = client.post("/hooks/graph", params={"validationToken": "hello + test"})
        assert response.status_code == 200
        assert response.text == "hello + test"
        assert response.headers["content-type"].startswith("text/plain")
        response = client.post(
            "/hooks/graph", json={"value": [{"subscriptionId": "sub", "clientState": "bad"}]}
        )
        assert response.status_code == 202
        assert q.claim() is None
        response = client.post(
            "/hooks/graph",
            json={"value": [{"subscriptionId": "sub", "clientState": "correct-state"}]},
        )
        assert response.status_code == 202
        assert q.claim()["channel_id"] == "channel"
        response = client.post(
            "/hooks/graph/lifecycle",
            json={
                "value": [
                    {
                        "subscriptionId": "sub",
                        "clientState": "correct-state",
                        "lifecycleEvent": "subscriptionRemoved",
                    }
                ]
            },
        )
        assert response.status_code == 202
        assert q.accept([{"subscriptionId": "sub", "clientState": "correct-state"}]) == 0


def test_invalid_request_and_failed_storage(tmp_path):
    class Broken:
        def accept(self, value, **kwargs):
            raise RuntimeError("private failure")

    app = FastAPI()
    app.include_router(create_notification_router(Broken()))
    with TestClient(app) as client:
        assert client.post("/hooks/graph", content=b"not json").status_code == 400
        assert client.post("/hooks/graph", content=b"x" * 65537).status_code == 413
        response = client.post("/hooks/graph", json={"value": []})
        assert response.status_code == 503
        assert "private" not in response.text


def test_early_unknown_subscription_is_retried_instead_of_acknowledged(tmp_path):
    q = NotificationQueue(lambda: sqlite3.connect(tmp_path / "notifications.sqlite3"))
    app = FastAPI()
    app.include_router(create_notification_router(q))
    with TestClient(app) as client:
        value = {"value": [{"subscriptionId": "sub", "clientState": "correct-state"}]}
        assert client.post("/hooks/graph", json=value).status_code == 503
        q.register("sub", "correct-state", "tenant", "team", "channel")
        assert client.post("/hooks/graph", json=value).status_code == 202
        assert q.claim()["channel_id"] == "channel"
