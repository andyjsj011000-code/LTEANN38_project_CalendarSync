from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
from conftest import BOB
from fastapi.testclient import TestClient
from test_server import server as server

from web_service.schema_history import SQLiteHistory
from web_service.server import create_test_server


def test_google_connection_reads_classroom_and_preserves_the_existing_connection(server):
    history = SQLiteHistory(server.settings.database_path, "sqlite-server")
    history.baseline(expected_sha256=history.plan()["plan_sha256"])
    for feature in ("classroom", "classroom-google"):
        history.upgrade(feature, expected_sha256=history.plan(feature)["plan_sha256"])
    settings = replace(
        server.settings,
        classroom_enabled=True,
        google_classroom_client_id="example.apps.googleusercontent.com",
        google_classroom_client_secret="fake-google-secret",
    )
    app = create_test_server(
        settings,
        server.db,
        server.identity,
        server.graph,
        server.source,
        server.sender,
        worker=False,
    )
    scopes = {
        "https://www.googleapis.com/auth/classroom.courses.readonly",
        "https://www.googleapis.com/auth/classroom.announcements.readonly",
        "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    }
    token_calls = []
    revoked = []
    assignment = "First assignment"

    def google(request):
        if request.url.host == "oauth2.googleapis.com":
            fields = parse_qs(request.content.decode())
            if request.url.path == "/revoke":
                revoked.append(fields["token"][0])
                return httpx.Response(200)
            token_calls.append(fields)
            if fields["grant_type"] == ["authorization_code"]:
                assert fields["code_verifier"] and fields["code"] == ["code-1"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "first-access",
                        "refresh_token": "private-refresh",
                        "scope": " ".join(scopes),
                    },
                )
            assert fields["refresh_token"] == ["private-refresh"]
            return httpx.Response(200, json={"access_token": "refreshed-access"})
        assert request.url.host == "classroom.googleapis.com"
        assert request.headers["authorization"] in {
            "Bearer first-access",
            "Bearer refreshed-access",
        }
        if request.url.path == "/v1/courses":
            assert request.url.params["studentId"] == "me"
            return httpx.Response(200, json={"courses": [{"id": "1", "name": "Test course"}]})
        if request.url.path.endswith("/announcements"):
            return httpx.Response(200, json={"announcements": [{"text": "Class notice"}]})
        assert request.url.path.endswith("/courseWork")
        return httpx.Response(
            200,
            json={
                "courseWork": [
                    {
                        "title": assignment,
                        "workType": "ASSIGNMENT",
                        "dueDate": {"year": 2026, "month": 11, "day": 17},
                    },
                    {"title": "Question", "workType": "SHORT_ANSWER_QUESTION"},
                ]
            },
        )

    app.state.classroom_google.client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(google)
    )
    owner = server.client()
    other = server.client(BOB)
    with TestClient(app, base_url=settings.origin, cookies=owner.cookies) as client:
        client.headers["x-csrf-token"] = owner.headers["x-csrf-token"]
        legacy = client.post("/api/classroom/connection").json()["token"]
        assert client.get("/api/service").json()["classroom_google_enabled"] is True
        start = client.post("/api/classroom/google/connect")
        params = parse_qs(urlsplit(start.json()["authorization_url"]).query)
        assert set(params["scope"][0].split()) == scopes
        assert params["access_type"] == ["offline"]
        assert params["code_challenge_method"] == ["S256"]
        callback = {"state": params["state"][0], "code": "code-1"}
        with TestClient(app, base_url=settings.origin, cookies=other.cookies) as outsider:
            outsider.cookies.update(start.cookies)
            rejected = outsider.get(
                "/auth/classroom/google/callback", params=callback, follow_redirects=False
            )
            assert rejected.headers["location"] == "/?classroom=failed"
        assert token_calls == []
        start = client.post("/api/classroom/google/connect")
        callback["state"] = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        result = client.get(
            "/auth/classroom/google/callback", params=callback, follow_redirects=False
        )
        assert result.headers["location"] == "/?classroom=connected"
        snapshot = client.get("/api/classroom").json()
        assert snapshot["method"] == "google"
        assert [item["title"] for item in snapshot["snapshot"]["items"]] == [
            "Class notice",
            "First assignment",
        ]
        assert snapshot["snapshot"]["items"][1]["due"] == "2026-11-17"
        assert "private-refresh" not in str(snapshot)
        with server.db.connect() as connection:
            credential = connection.execute(
                "SELECT credential_blob FROM web_classroom_google"
            ).fetchone()[0]
            assert "private-refresh" not in credential
        with TestClient(app, base_url=settings.origin, cookies=other.cookies) as outsider:
            assert outsider.get("/api/classroom").json() == {"connected": False, "snapshot": None}
        assert (
            client.get(
                "/auth/classroom/google/callback", params=callback, follow_redirects=False
            ).headers["location"]
            == "/?classroom=failed"
        )
        assert len(token_calls) == 1
        server.now[0] += 901
        assignment = "New assignment"
        app.state.classroom_google.tick()
        assert client.get("/api/classroom").json()["snapshot"]["items"][1]["title"] == assignment
        assert len(token_calls) == 2
        assert client.delete("/api/classroom/google/connection").status_code == 204
        assert revoked == ["private-refresh"]
        assert client.get("/api/classroom").json() == {"connected": True, "snapshot": None}
        assert (
            client.post(
                "/api/classroom/import",
                headers={"authorization": f"Bearer {legacy}"},
                json={"captured_at": server.now[0] * 1000, "items": []},
            ).status_code
            == 204
        )
