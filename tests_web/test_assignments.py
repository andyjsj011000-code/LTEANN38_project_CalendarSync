"""Assignment reads, isolation, missing consent and timestamp boundaries."""

from types import SimpleNamespace

import httpx
import pytest
from conftest import ALICE, BOB, CLIENT
from test_postgres_server_integration import postgres_server as postgres_server
from test_server import server as server

from web_service.assignments import AssignmentGraph, AssignmentService, normalize_assignment
from web_service.connections import PersistentIdentity
from web_service.school_graph import GraphFailure
from web_service.server import create_test_server


def assignment(**changes):
    return {
        "id": "task-1",
        "classId": CLIENT,
        "displayName": "7주차 보고서 제출",
        "dueDateTime": "2026-11-12T14:59:00Z",
        "status": "assigned",
        "submissions": [{"status": "working", "recipient": {"userId": ALICE}}],
        **changes,
    }


def test_kst_due_date_and_personal_submission_status():
    item = normalize_assignment(
        assignment(dueDateTime="2026-11-11T15:01:00Z"), ALICE, {CLIENT: "물리실험"}
    )
    assert item["end_date"] == "2026-11-12" and item["time"] == "00:01"
    assert item["channel"] == "물리실험" and not item["completed"]
    assert item["source"] == "assignment" and item["source_url"].startswith(
        "https://teams.microsoft.com/"
    )


def test_completed_reassigned_and_other_students_results_are_distinct():
    submitted = assignment(
        submissions=[
            {"status": "submitted", "recipient": {"userId": BOB}},
            {"status": "reassigned", "recipient": {"userId": ALICE}},
        ]
    )
    item = normalize_assignment(submitted, ALICE, {})
    assert item["submission_status"] == "reassigned" and not item["completed"]
    assert normalize_assignment(submitted, BOB, {})["completed"]
    assert (
        normalize_assignment(assignment(submissions=[]), ALICE, {})["submission_status"]
        == "unknown"
    )


def test_undated_future_draft_and_unsafe_link():
    assert normalize_assignment(assignment(status="draft"), ALICE, {}) is None
    assert normalize_assignment(assignment(status="scheduled"), ALICE, {}) is None
    item = normalize_assignment(
        assignment(dueDateTime=None, webUrl="javascript:alert(1)"), ALICE, {}
    )
    assert item["start_date"] is None and item["time"] is None
    assert item["source_url"].startswith("https://teams.microsoft.com/l/entity/")


@pytest.mark.parametrize("due", ["bad", "2026-11-12T23:59:00", 10])
def test_bad_deadline_does_not_become_a_made_up_date(due):
    with pytest.raises(GraphFailure):
        normalize_assignment(assignment(dueDateTime=due), ALICE, {})


def graph_with(handler):
    return AssignmentGraph(lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_delegated_me_endpoint_pagination_and_class_names():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        if request.url.path == "/v1.0/me":
            return httpx.Response(200, json={"id": ALICE, "userType": "Member"})
        if request.url.path.endswith("/joinedTeams"):
            return httpx.Response(200, json={"value": [{"id": CLIENT, "displayName": "물리실험"}]})
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"value": [assignment(id="second")]})
        assert request.url.path == "/v1.0/education/me/assignments"
        assert request.url.params["$expand"] == "submissions"
        assert "dueDateTime ge" in request.url.params["$filter"]
        return httpx.Response(
            200,
            json={
                "value": [assignment()],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/education/me/assignments?page=2",
            },
        )

    items = graph_with(handler).assignments("private-token", ALICE, 1794000000)
    assert len(items) == 2 and items[0]["channel"] == "물리실험"
    assert len(calls) == 4


def test_permission_denied_is_not_an_empty_success():
    def handler(request):
        return (
            httpx.Response(200, json={"id": ALICE, "userType": "Member"})
            if request.url.path == "/v1.0/me"
            else httpx.Response(403)
        )

    with pytest.raises(GraphFailure) as error:
        graph_with(handler).assignments("private-token", ALICE, 1794000000)
    assert error.value.state == "assignment_permission_required"


def test_foreign_next_link_never_receives_the_bearer():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/v1.0/me":
            return httpx.Response(200, json={"id": ALICE, "userType": "Member"})
        return httpx.Response(
            200,
            json={
                "value": [assignment()],
                "@odata.nextLink": "https://attacker.invalid/v1.0/education/me/assignments",
            },
        )

    with pytest.raises(GraphFailure):
        graph_with(handler).assignments("private-token", ALICE, 1794000000)
    assert len(requests) == 2 and all("attacker.invalid" not in x for x in requests)


class PrivateGraph:
    def __init__(self):
        self.calls = 0
        self.failure = None
        self.on_read = None

    def assert_member(self, *args):
        if self.failure:
            raise GraphFailure(self.failure)

    def assignments(self, token, oid, now):
        self.calls += 1
        self.assert_member()
        if self.on_read:
            self.on_read()
        return [normalize_assignment(assignment(displayName="private-" + oid), oid, {})]


def grant(server, client):
    session = server.db.session(client.cookies.get("__Host-calendarsync_session"))
    device = server.db.session_device(session)
    payload = server.db.unseal(device["credential_blob"])
    payload["assignment_access"] = True
    with server.db.connect() as conn:
        sql = "UPDATE web_connections SET credential_blob=? WHERE id_hash=?"
        if getattr(server.db, "storage_backend", "") == "postgresql-experimental":
            sql = sql.replace("?", "%s")
        conn.execute(sql, (server.db.seal(payload), device["id_hash"]))
    return session, server.db.device(device["id_hash"])


def service(server):
    graph = PrivateGraph()
    result = AssignmentService(server.db, server.integration.tokens, graph)
    result.initialize()
    return result, graph


def test_missing_consent_does_not_read_graph_or_break_existing_calendar(server):
    c = server.client()
    svc, graph = service(server)
    session = server.db.session(c.cookies.get("__Host-calendarsync_session"))
    assert svc.read(session)["state"] == "permission_required"
    assert graph.calls == 0
    assert c.get("/api/today").status_code == 200


def test_snapshots_are_private_encrypted_and_reused_only_with_permission(server):
    svc, graph = service(server)
    a, _ = grant(server, server.client())
    b, _ = grant(server, server.client(BOB))
    first = svc.read(a)
    assert first["state"] == "connected" and ALICE in first["items"][0]["title"]
    assert BOB in svc.read(b)["items"][0]["title"]
    assert ALICE not in svc.read(b)["items"][0]["title"]
    assert svc.read(a)["items"] == first["items"] and graph.calls == 2
    assert b"private-" not in server.db.path.read_bytes()
    graph.failure = "assignment_permission_required"
    assert svc.read(a)["items"] == []


def test_failed_collection_does_not_destroy_previous_snapshot_or_claim_success(server):
    svc, graph = service(server)
    session, _ = grant(server, server.client())
    first = svc.read(session)
    server.now[0] += 61
    graph.failure = "source_unavailable"
    result = svc.read(session)
    assert result["state"] == "unavailable" and not result["items"]
    assert svc._cached(session["user_id"])[0] == first["items"]


def test_revocation_during_a_fetch_cannot_publish_or_cache_results(server):
    svc, graph = service(server)
    session, device = grant(server, server.client())
    graph.on_read = lambda: server.db.revoke_device(device["id_hash"])
    result = svc.read(session)
    assert result["state"] == "reauth_required" and result["items"] == []
    assert svc._cached(session["user_id"]) == ([], 0)


def test_refresh_keeps_optional_scope_metadata(server):
    c = server.client()
    _, device = grant(server, c)
    lease = server.db.lock_refresh(device["id_hash"])
    assert server.db.save_refresh(device["id_hash"], lease, "rotated", 3600, "cache")
    assert server.db.unseal(server.db.device(device["id_hash"])["credential_blob"])[
        "assignment_access"
    ]


def test_http_feature_is_opt_in_and_routes_are_authenticated(server):
    assert server.client().get("/api/assignments").status_code == 404
    app = create_test_server(
        server.settings,
        server.db,
        server.identity,
        server.graph,
        server.source,
        server.sender,
        worker=False,
        assignments_enabled=True,
    )
    from fastapi.testclient import TestClient

    with TestClient(app, base_url=server.settings.origin) as c:
        assert c.get("/api/assignments").status_code == 401
        existing = server.client()
        c.cookies.update(existing.cookies)
        assert c.get("/api/assignments").json()["state"] == "permission_required"
        assert c.get("/api/service").json()["assignments_enabled"]
        session, _ = grant(server, c)
        app.state.assignments.graph = PrivateGraph()
        result = c.get("/api/assignments").json()
        assert result["state"] == "connected" and result["items"][0]["source"] == "assignment"
        assert c.get("/api/calendar?start=2026-11-01&end=2026-11-30").status_code == 200


def test_optional_consent_keeps_normal_login_scopes(server, monkeypatch):
    calls = []
    app = SimpleNamespace(
        initiate_auth_code_flow=lambda **kwargs: (
            calls.append(kwargs) or {"state": "s", "auth_uri": "https://login.example"}
        )
    )
    identity = PersistentIdentity(server.settings)
    monkeypatch.setattr(identity, "application", lambda **kwargs: app)
    identity.begin()
    identity.begin(assignments=True)
    assert "EduAssignments.ReadBasic" not in calls[0]["scopes"]
    assert "EduAssignments.ReadBasic" in calls[1]["scopes"]


def test_postgres_private_snapshots_and_revocation(postgres_server):
    s = postgres_server
    svc, graph = service(s)
    a, device = grant(s, s.client())
    b, _ = grant(s, s.client(BOB))
    assert ALICE in svc.read(a)["items"][0]["title"]
    assert BOB in svc.read(b)["items"][0]["title"]
    with s.db.connect() as conn:
        blobs = conn.execute("SELECT payload_blob FROM web_assignment_snapshots").fetchall()
    assert len(blobs) == 2 and all("private-" not in row[0] for row in blobs)
    s.now[0] += 61
    graph.on_read = lambda: s.db.revoke_device(device["id_hash"])
    result = svc.read(a)
    assert result["state"] == "reauth_required" and result["items"] == []
    assert ALICE in svc._cached(a["user_id"])[0][0]["title"]


def test_postgres_assignment_scope_survives_refresh(postgres_server):
    s = postgres_server
    _, device = grant(s, s.client())
    lease = s.db.lock_refresh(device["id_hash"])
    assert s.db.save_refresh(device["id_hash"], lease, "new-token", 3600, "new-cache")
    assert s.db.unseal(s.db.device(device["id_hash"])["credential_blob"])["assignment_access"]
