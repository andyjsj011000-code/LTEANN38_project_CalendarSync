from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import BOB
from fastapi.testclient import TestClient
from test_server import server as server

from web_service.schema_history import SQLiteHistory
from web_service.server import create_test_server


@pytest.fixture
def classroom_app(server):
    history = SQLiteHistory(server.settings.database_path, "sqlite-server")
    history.baseline(expected_sha256=history.plan()["plan_sha256"])
    for feature in ("classroom", "classroom-google", "classroom-courses"):
        history.upgrade(feature, expected_sha256=history.plan(feature)["plan_sha256"])
    settings = replace(
        server.settings,
        classroom_enabled=True,
        google_classroom_client_id="example.apps.googleusercontent.com",
        google_classroom_client_secret="fake-google-secret",
    )
    return create_test_server(
        settings,
        server.db,
        server.identity,
        server.graph,
        server.source,
        server.sender,
        worker=False,
    )


def sign_in(client, *, background=False):
    client.get("/auth/login", params={"background": int(background)}, follow_redirects=False)
    return client.get("/auth/callback?state=s&code=ok", follow_redirects=False)


@pytest.mark.parametrize(("background", "legacy_pending"), [(False, True), (True, False)])
def test_microsoft_sign_in_offers_classroom_consent_once(
    classroom_app, server, monkeypatch, background, legacy_pending
):
    if legacy_pending:
        server.client()
        with server.db.connect() as connection:
            connection.execute(
                """INSERT INTO web_classroom_google(user_id,connection_id,state,next_sync_at)
                SELECT id,'previous-manual-attempt','pending',0 FROM web_users"""
            )
    finish_microsoft = server.identity.finish
    monkeypatch.setattr(
        server.identity,
        "finish",
        lambda flow, query: {
            **finish_microsoft(flow, query),
            "preferred_username": "student@sshs.hs.kr",
        },
    )

    with TestClient(classroom_app, base_url=server.settings.origin) as client:
        result = sign_in(client, background=background)
        google = urlsplit(result.headers["location"])
        assert (google.scheme, google.hostname, google.path) == (
            "https",
            "accounts.google.com",
            "/o/oauth2/v2/auth",
        )
        params = parse_qs(google.query)
        assert params["login_hint"] == ["student@sshs.hs.kr"]
        assert params["prompt"] == ["consent"]
        assert params["access_type"] == ["offline"]
        assert client.get("/api/classroom").json() == {"connected": False, "snapshot": None}

        declined = client.get(
            "/auth/classroom/google/callback",
            params={"state": params["state"][0], "error": "access_denied"},
            follow_redirects=False,
        )
        assert declined.headers["location"] == "/?classroom=skipped"
        assert client.get("/api/me").status_code == 200

    with TestClient(classroom_app, base_url=server.settings.origin) as another_device:
        result = sign_in(another_device, background=background)
        assert result.headers["location"] == "/"
        me = another_device.get("/api/me").json()
        another_device.headers["x-csrf-token"] = me["csrf_token"]
        assert another_device.post("/api/classroom/google/connect").status_code == 200
        assert another_device.delete("/api/classroom/google/connection").status_code == 204
        assert sign_in(another_device, background=background).headers["location"] == "/"


def test_automatic_google_consent_links_the_microsoft_session(classroom_app, server):
    with TestClient(classroom_app, base_url=server.settings.origin) as client:
        start = sign_in(client)
        params = parse_qs(urlsplit(start.headers["location"]).query)

        def google(request):
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "first-access",
                        "refresh_token": "private-refresh",
                        "scope": params["scope"][0],
                    },
                )
            assert request.url.path == "/v1/courses"
            return httpx.Response(200, json={"courses": []})

        classroom_app.state.classroom_google.client_factory = lambda: httpx.Client(
            transport=httpx.MockTransport(google)
        )
        finished = client.get(
            "/auth/classroom/google/callback",
            params={"state": params["state"][0], "code": "approved"},
            follow_redirects=False,
        )
        assert finished.headers["location"] == "/?classroom=connected"
        assert sign_in(client).headers["location"] == "/"
        assert client.get("/api/classroom").json()["method"] == "google"


def test_google_connection_reads_classroom_and_preserves_the_existing_connection(
    server, classroom_app
):
    app = classroom_app
    settings = server.settings
    scopes = {
        "https://www.googleapis.com/auth/classroom.courses.readonly",
        "https://www.googleapis.com/auth/classroom.announcements.readonly",
        "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    }
    token_calls = []
    revoked = []
    assignment = "First assignment"
    submission_state = "CREATED"
    extracted_prompts = []

    class Extractor:
        configured = True

        def extract(self, text, images, previous):
            assert not images and not previous
            extracted_prompts.append(text)
            if text.endswith("Undated notice"):
                return []
            return [
                {
                    "key": "event",
                    "title": "Class schedule",
                    "notes": "Schedule summary",
                    "start_date": "2026-11-17",
                    "end_date": "2026-11-17",
                    "time": None,
                    "kind": "event",
                    "cancelled": False,
                }
            ]

    app.state.classroom_google.extractor = Extractor()

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
                        "scope": " ".join(scopes).replace(
                            "classroom.coursework.me.readonly",
                            "classroom.student-submissions.me.readonly",
                        ),
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
            return httpx.Response(
                200,
                json={
                    "courses": [
                        {"id": "1", "name": "Test course"},
                        {"id": "2", "name": "Test course"},
                    ]
                },
            )
        if request.url.path.startswith("/v1/courses/2/"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/announcements"):
            notices = [{"id": "21", "text": "Class notice", "creationTime": "2026-10-22T16:00:00Z"}]
            if "creationTime" in request.url.params["fields"]:
                notices.append(
                    {
                        "id": "22",
                        "text": "Undated notice",
                        "creationTime": "2026-10-22T16:00:00Z",
                    }
                )
            return httpx.Response(200, json={"announcements": notices})
        if request.url.path == "/v1/courses/1/courseWork/-/studentSubmissions":
            assert request.url.params["userId"] == "me"
            assert (
                request.url.params["fields"]
                == "studentSubmissions(courseWorkId,state),nextPageToken"
            )
            return httpx.Response(
                200,
                json={
                    "studentSubmissions": [
                        {"courseWorkId": "11", "state": submission_state},
                    ]
                },
            )
        assert request.url.path.endswith("/courseWork")
        return httpx.Response(
            200,
            json={
                "courseWork": [
                    {
                        "id": "11",
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
    with TestClient(app, base_url=settings.origin, cookies=owner.cookies) as client:
        client.headers["x-csrf-token"] = owner.headers["x-csrf-token"]
        legacy = client.post("/api/classroom/connection").json()["token"]
        with TestClient(app, base_url=settings.origin) as relogged:
            assert sign_in(relogged).headers["location"] == "/"
        other = server.client(BOB)
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
        assert snapshot["announcement_analysis"] is False
        assert extracted_prompts == []
        assert [item["title"] for item in snapshot["snapshot"]["items"]] == [
            "Class notice",
            "First assignment",
        ]
        assert snapshot["snapshot"]["items"][1]["due"] == "2026-11-17"
        assert snapshot["snapshot"]["items"][1]["submission_status"] == "working"
        assert [course["key"] for course in snapshot["courses"]] == ["id:1", "id:2"]
        preference = "/api/classroom/courses/preference"
        hidden = client.put(preference, json={"key": "id:1", "hidden": True}).json()
        assert [course["hidden"] for course in hidden["courses"]] == [True, False]
        assert hidden["snapshot"]["items"] == []
        same_owner = server.client()
        with TestClient(app, base_url=settings.origin, cookies=same_owner.cookies) as relogged:
            assert relogged.get("/api/classroom").json()["snapshot"]["items"] == []
        assert client.put(preference, json={"key": "id:1", "hidden": False}).status_code == 200
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
        submission_state = "TURNED_IN"
        app.state.classroom_google.tick()
        item = client.get("/api/classroom").json()["snapshot"]["items"][1]
        assert item["title"] == assignment
        assert item["submission_status"] == "submitted"
        assert len(token_calls) == 2
        analysis = "/api/classroom/google/announcement-analysis"
        assert client.put(analysis, json={"enabled": True}).json() == {"enabled": True}
        opted_in = client.get("/api/classroom").json()
        assert opted_in["announcement_analysis"] is True
        assert [event["title"] for event in opted_in["snapshot"]["events"]] == ["Class schedule"]
        assert extracted_prompts == [
            f"공지 작성일 (한국 시간): 2026-10-23 01:00\n공지 본문:\n{body}"
            for body in ("Class notice", "Undated notice")
        ]
        server.now[0] += 901
        app.state.classroom_google.tick()
        assert len(extracted_prompts) == 2
        hidden = client.put(preference, json={"key": "id:1", "hidden": True}).json()
        assert hidden["snapshot"]["events"] == []
        assert client.put(analysis, json={"enabled": False}).json() == {"enabled": False}
        assert "events" not in client.get("/api/classroom").json()["snapshot"]
        app.state.classroom_google.tick()
        assert len(extracted_prompts) == 2
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
