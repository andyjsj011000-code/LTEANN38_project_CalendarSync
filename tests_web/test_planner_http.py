"""Planner opt-in APIs keep the public server's existing security boundaries.

These are full-server tests: authentication creates a real viewing session and
device link, and requests pass through SecurityBoundary and current_session.
All identity, Teams and delivery implementations are deterministic test doubles.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from conftest import ALICE, BOB, CLIENT, TENANT
from test_server import Graph, Identity, Sender, Source
from web_service.config import Settings
from web_service.server import create_test_server
from web_service.test_storage import TestDatabase


READ_ROUTES = ("/api/task-draft", "/api/task-trash", "/api/teams/ingestion-status")
PRIVATE_MARKER = "planner-private-sentinel"


def draft_body(version=0, title=PRIVATE_MARKER, **context):
    return {
        "version": version,
        "draft": {
            "task_id": None,
            "base_version": None,
            **context,
            "fields": {
                "title": title,
                "notes": "",
                "start_date": "",
                "end_date": "",
                "time": "",
                "display_mode": "daily",
                "selected_dates": [],
            },
        },
    }


def task_body(title=PRIVATE_MARKER):
    return {
        "title": title,
        "notes": "private task notes",
        "start_date": "2026-10-22",
        "end_date": "2026-10-22",
        "time": "18:00",
        "display_mode": "daily",
        "selected_dates": [],
    }


def session_device(s, client):
    session = s.db.session(client.cookies.get(s.integration.session_cookie))
    return session, s.db.session_device(session)


@pytest.fixture
def planner_server_factory(tmp_path):
    clients = []
    count = [0]

    def build(enabled=True):
        count[0] += 1
        now = [int(datetime(2026, 10, 22, 8, tzinfo=timezone.utc).timestamp())]
        settings = Settings(
            database_path=tmp_path / f"planner-{count[0]}.sqlite3",
            data_key=Fernet.generate_key().decode(),
            origin="https://test.example.school",
            tenant_id=TENANT,
            client_id=CLIENT,
            client_secret="test-only-secret",
            session_seconds=28800,
            idle_seconds=1800,
        )
        db = TestDatabase(
            settings.database_path,
            settings.data_key,
            clock=lambda: now[0],
            planner_enhancements_enabled=enabled,
        )
        identity, graph, source, sender = Identity(db.clock), Graph(), Source(), Sender()
        app = create_test_server(settings, db, identity, graph, source, sender, worker=False)

        def client(oid=ALICE):
            c = TestClient(app, base_url=settings.origin)
            clients.append(c)
            if oid is not None:
                identity.oid = oid
                assert c.get("/auth/login?background=1", follow_redirects=False).status_code == 303
                response = c.get("/auth/callback?state=s&code=ok", follow_redirects=False)
                assert response.status_code == 303 and response.headers["location"] == "/"
                response = c.get("/api/me")
                assert response.status_code == 200
                c.headers["x-csrf-token"] = response.json()["csrf_token"]
            return c

        return SimpleNamespace(
            now=now,
            settings=settings,
            db=db,
            identity=identity,
            graph=graph,
            source=source,
            sender=sender,
            app=app,
            integration=app.state.integration,
            client=client,
        )

    yield build
    for client in clients:
        client.close()


@pytest.fixture
def planner_server(planner_server_factory):
    return planner_server_factory()


def test_disabled_planner_exposes_no_sidecar_apis_or_schema(planner_server_factory):
    s = planner_server_factory(enabled=False)
    client = s.client()
    assert client.get("/api/service").json()["planner_enhancements"] is False
    for method, path, kwargs in (
        ("GET", "/api/task-draft", {}),
        ("PUT", "/api/task-draft", {"json": draft_body()}),
        ("DELETE", "/api/task-draft?version=0", {}),
        ("GET", "/api/task-trash", {}),
        ("POST", "/api/tasks/missing/trash", {"json": {"version": 1}}),
        ("POST", "/api/task-trash/missing/restore", {}),
        ("GET", "/api/teams/ingestion-status", {}),
    ):
        assert client.request(method, path, **kwargs).status_code == 404
    with s.db.connect() as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names.isdisjoint({"web_task_drafts", "web_task_trash", "web_ingestion_status"})


def test_disabled_planner_keeps_legacy_personal_delete_working(planner_server_factory):
    s = planner_server_factory(enabled=False)
    client = s.client()
    created = client.post("/api/tasks", json=task_body())
    assert created.status_code == 201
    task = created.json()
    response = client.delete(f"/api/tasks/{task['id']}?version={task['version']}")
    assert response.status_code == 204 and response.content == b""
    assert client.get("/api/tasks/" + task["id"]).status_code == 404


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/task-draft", None),
        ("PUT", "/api/task-draft", draft_body()),
        ("DELETE", "/api/task-draft?version=0", None),
        ("GET", "/api/task-trash", None),
        ("POST", "/api/tasks/missing/trash", {"version": 1}),
        ("POST", "/api/task-trash/missing/restore", None),
        ("GET", "/api/teams/ingestion-status", None),
    ],
)
def test_planner_routes_require_a_real_school_session(planner_server, method, path, body):
    response = planner_server.client(None).request(method, path, json=body)
    assert response.status_code == 401
    assert PRIVATE_MARKER not in response.text


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", "/api/task-draft", draft_body()),
        ("DELETE", "/api/task-draft?version=0", None),
        ("POST", "/api/tasks/missing/trash", {"version": 1}),
        ("POST", "/api/task-trash/missing/restore", None),
        ("DELETE", "/api/tasks/missing?version=1", None),
    ],
)
def test_planner_writes_keep_csrf_and_browser_origin_checks(planner_server, method, path, body):
    client = planner_server.client()
    for headers in (
        {"x-csrf-token": ""},
        {"origin": "https://foreign.example"},
        {"sec-fetch-site": "cross-site"},
    ):
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403
        assert PRIVATE_MARKER not in response.text
    assert client.get("/api/task-draft").json()["version"] == 0
    assert client.get("/api/task-trash").json()["items"] == []


@pytest.mark.parametrize("revocation", ["session", "device", "idle", "inactive", "tenant"])
def test_planner_rejects_expired_revoked_or_wrong_tenant_sessions(planner_server, revocation):
    s = planner_server
    client = s.client()
    assert client.put("/api/task-draft", json=draft_body()).status_code == 200
    session, device = session_device(s, client)
    if revocation == "session":
        s.db.revoke_session(client.cookies.get(s.integration.session_cookie))
    elif revocation == "device":
        s.db.revoke_device(device["id_hash"])
    elif revocation == "idle":
        s.now[0] += s.settings.idle_seconds + 1
    else:
        with s.db.connect() as db:
            if revocation == "inactive":
                db.execute("UPDATE web_users SET active=0 WHERE id=?", (session["user_id"],))
            else:
                db.execute(
                    "UPDATE web_users SET tenant_id=? WHERE id=?",
                    ("99999999-9999-4999-8999-999999999999", session["user_id"]),
                )
    for path in READ_ROUTES:
        response = client.get(path)
        assert response.status_code == 401
        assert PRIVATE_MARKER not in response.text


def test_account_switch_cannot_read_edit_archive_or_restore_another_accounts_data(planner_server):
    s = planner_server
    alice, bob = s.client(ALICE), s.client(BOB)
    assert alice.get("/api/service").json()["planner_enhancements"] is True
    assert alice.put("/api/task-draft", json=draft_body()).status_code == 200
    task = alice.post("/api/tasks", json=task_body()).json()
    assert bob.get("/api/task-draft").json() == {
        "draft": None,
        "version": 0,
        "updated_at": None,
        "expires_at": None,
    }
    attempted_edit = bob.put(
        "/api/task-draft", json=draft_body(task_id=task["id"], base_version=task["version"])
    )
    assert attempted_edit.status_code == 404 and PRIVATE_MARKER not in attempted_edit.text
    assert (
        bob.post(f"/api/tasks/{task['id']}/trash", json={"version": task["version"]}).status_code
        == 404
    )
    archived = alice.post(f"/api/tasks/{task['id']}/trash", json={"version": task["version"]})
    assert archived.status_code == 200
    assert bob.get("/api/task-trash").json()["items"] == []
    denied = bob.post(f"/api/task-trash/{archived.json()['id']}/restore")
    assert denied.status_code == 404 and PRIVATE_MARKER not in denied.text
    assert bob.put("/api/task-draft", json=draft_body(title="Bob draft")).status_code == 200
    assert alice.get("/api/task-draft").json()["draft"]["fields"]["title"] == PRIVATE_MARKER
    assert bob.get("/api/task-draft").json()["draft"]["fields"]["title"] == "Bob draft"


def test_draft_revision_protects_other_tabs_and_a_confirmed_clear(planner_server):
    s = planner_server
    first, other_tab = s.client(), s.client()
    assert first.get("/api/task-draft").json()["version"] == 0
    assert other_tab.get("/api/task-draft").json()["version"] == 0
    saved = first.put("/api/task-draft", json=draft_body())
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert saved.json()["expires_at"] - saved.json()["updated_at"] == 7 * 86400
    assert other_tab.put("/api/task-draft", json=draft_body(title="stale tab")).status_code == 409
    cleared = first.delete("/api/task-draft?version=1")
    assert cleared.status_code == 200 and cleared.json()["version"] == 2
    assert cleared.json()["draft"] is None
    late = other_tab.put("/api/task-draft", json=draft_body(version=1, title="late autosave"))
    assert late.status_code == 409 and PRIVATE_MARKER not in late.text
    recovered = other_tab.get("/api/task-draft").json()
    assert recovered["draft"] is None and recovered["version"] == 2
    assert other_tab.put("/api/task-draft", json=draft_body(version=2)).json()["version"] == 3
    assert PRIVATE_MARKER.encode() not in s.db.path.read_bytes()


def test_full_server_validation_errors_do_not_echo_draft_content(planner_server):
    client = planner_server.client()
    body = draft_body()
    body["draft"]["fields"]["start_date"] = "not-a-date"
    response = client.put("/api/task-draft", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Check the input and dates"}
    assert PRIVATE_MARKER not in response.text and "not-a-date" not in response.text
    assert client.get("/api/task-draft").json()["version"] == 0


def forbid_teams_calls(s, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reading personal planner data must not refresh or collect Teams")

    for target, name in (
        (s.integration, "run_sync"),
        (s.graph, "channels"),
        (s.source, "events"),
        (s.identity, "refresh"),
        (s.integration.tokens, "acquire"),
        (s.db, "materialize_cached_events"),
    ):
        monkeypatch.setattr(target, name, forbidden)


def test_legacy_delete_is_204_and_can_restore_content_state_and_reminders(
    planner_server, monkeypatch
):
    s = planner_server
    client = s.client()
    forbid_teams_calls(s, monkeypatch)
    created = client.post("/api/tasks", json=task_body())
    assert created.status_code == 201
    task = created.json()
    state = client.patch(
        f"/api/tasks/{task['id']}/state",
        json={"version": task["version"], "completed": True, "hidden": True},
    )
    assert state.status_code == 200
    task = state.json()
    rules = {"offsets": [-30], "custom_times": []}
    assert client.put(f"/api/reminders/personal/{task['id']}", json=rules).status_code == 200
    response = client.delete(f"/api/tasks/{task['id']}?version={task['version']}")
    assert response.status_code == 204 and response.content == b""
    assert client.get("/api/tasks/" + task["id"]).status_code == 404
    listing = client.get("/api/task-trash")
    assert listing.status_code == 200 and listing.json()["retention_days"] == 7
    (archived,) = listing.json()["items"]
    assert archived["title"] == task["title"] and archived["task_id"] == task["id"]
    assert archived["completed"] is True and archived["hidden"] is True
    assert "notes" not in archived
    restored = client.post(f"/api/task-trash/{archived['id']}/restore")
    assert restored.status_code == 200
    value = restored.json()
    assert value["id"] == task["id"] and value["version"] == task["version"] + 1
    for key in ("title", "notes", "completed", "hidden", "start_date", "end_date", "time"):
        assert value[key] == task[key]
    assert client.get(f"/api/reminders/personal/{task['id']}").json() == rules
    assert client.get("/api/task-trash").json()["items"] == []
    assert client.post(f"/api/task-trash/{archived['id']}/restore").status_code == 404
    assert client.delete(f"/api/tasks/{task['id']}?version={task['version']}").status_code == 409
    assert (
        client.put(
            "/api/tasks/" + task["id"], json={**task_body("stale edit"), "version": task["version"]}
        ).status_code
        == 409
    )
    assert client.get("/api/tasks/" + task["id"]).json()["title"] == PRIVATE_MARKER
    assert s.sender.sent == []


def test_status_reads_persisted_counts_without_graph_or_permission_refresh(
    planner_server, monkeypatch
):
    s = planner_server
    alice, another_device, bob = s.client(), s.client(), s.client(BOB)
    session, device = session_device(s, alice)
    attempt = s.db.claim_ingestion(device)
    assert attempt
    assert s.db.update_ingestion(
        device,
        attempt,
        {
            "channels_total": 2,
            "channels_checked": 1,
            "messages_seen": 5,
            "messages_cached": 3,
            "private_source": PRIVATE_MARKER,
        },
    )
    assert s.db.begin_sync(session["user_id"], 0)
    with s.db.connect() as db:
        before = tuple(
            db.execute(
                "SELECT * FROM web_ingestion_status WHERE device_hash=?", (device["id_hash"],)
            ).fetchone()
        )
    forbid_teams_calls(s, monkeypatch)
    response = alice.get("/api/teams/ingestion-status")
    assert response.status_code == 200
    result = response.json()
    assert result["state"] == "running"
    assert result["counts"] == {
        "channels_total": 2,
        "channels_checked": 1,
        "messages_seen": 5,
        "messages_cached": 3,
    }
    assert result["last_success_at"] is None and result["error_code"] is None
    assert PRIVATE_MARKER not in response.text and attempt not in response.text
    assert "device_hash" not in result and "attempt_token" not in result
    assert response.headers["cache-control"] == "no-store"
    for client in (another_device, bob):
        other = client.get("/api/teams/ingestion-status")
        assert other.status_code == 200
        assert other.json()["state"] == "idle" and other.json()["counts"] is None
    with s.db.connect() as db:
        after = tuple(
            db.execute(
                "SELECT * FROM web_ingestion_status WHERE device_hash=?", (device["id_hash"],)
            ).fetchone()
        )
    assert after == before
    assert s.db.sync_status(session["user_id"])["state"] == "checking"
    assert s.sender.sent == []


@pytest.mark.parametrize("path", READ_ROUTES)
def test_planner_reads_share_the_existing_request_rate_limit(planner_server, monkeypatch, path):
    s = planner_server
    client = s.client()
    session, _ = session_device(s, client)
    original = s.db.allow

    def allow(key, *args, **kwargs):
        return False if key == "api:" + session["user_id"] else original(key, *args, **kwargs)

    monkeypatch.setattr(s.db, "allow", allow)
    response = client.get(path)
    assert response.status_code == 429 and PRIVATE_MARKER not in response.text
