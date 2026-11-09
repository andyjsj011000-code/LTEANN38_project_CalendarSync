"""Draft revisions, expiration and account boundaries; no real school traffic."""

from contextlib import contextmanager
import json
import re
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from web_service.api import create_app
from web_service.config import Settings
from web_service.planner_drafts import (
    DRAFT_RETENTION_SECONDS,
    DRAFT_SCHEMA_SQL,
    PostgresTaskDrafts,
    SQLiteTaskDrafts,
    TaskDraft,
    install_draft_routes,
)
from web_service.storage import Conflict, Database, Missing


class DraftDatabase(SQLiteTaskDrafts, Database):
    def initialize(self):
        super().initialize()
        with self.connect() as db:
            db.executescript(DRAFT_SCHEMA_SQL)


def draft(**fields):
    return {
        "task_id": None,
        "base_version": None,
        "fields": {
            "title": "Unfinished private task",
            "notes": "Private draft notes",
            "start_date": "2026-11-07",
            "end_date": "",
            "time": "",
            "display_mode": "daily",
            "selected_dates": [],
            **fields,
        },
    }


@pytest.fixture(params=["sqlite", "postgres-shim"])
def drafts(request, tmp_path):
    now = [2000000000]
    key = Fernet.generate_key().decode()
    base = DraftDatabase(tmp_path / "drafts.sqlite3", key, clock=lambda: now[0])
    base.initialize()
    alice = base.save_user("school", "alice", {"name": "Alice"})
    bob = base.save_user("school", "bob", {"name": "Bob"})
    other = base.save_user("other-school", "other", {"name": "Other"})
    if request.param == "postgres-shim":

        class Shim:
            def __init__(self, db):
                self.db = db

            def execute(self, query, values=()):
                return self.db.execute(re.sub(r" FOR UPDATE", "", query).replace("%s", "?"), values)

        @contextmanager
        def connect():
            with base.connect() as db:
                yield Shim(db)

        class Repository(PostgresTaskDrafts):
            pass

        repo = Repository()
        repo.connect, repo.seal, repo.unseal, repo.clock = (
            connect,
            base.seal,
            base.unseal,
            base.clock,
        )
    else:
        repo = base
    return SimpleNamespace(
        repo=repo, base=base, alice=alice, bob=bob, other=other, now=now, key=key
    )


def test_empty_then_saved_encrypted_draft_roundtrip(drafts):
    p = drafts
    assert p.repo.get_task_draft(p.alice, "school") == {
        "draft": None,
        "version": 0,
        "updated_at": None,
        "expires_at": None,
    }
    value = draft(title="  Keep whitespace  ", end_date="2026-11-01")
    saved = p.repo.save_task_draft(p.alice, "school", value, 0)
    assert saved == {
        "draft": value,
        "version": 1,
        "updated_at": p.now[0],
        "expires_at": p.now[0] + DRAFT_RETENTION_SECONDS,
    }
    assert p.repo.get_task_draft(p.alice, "school") == saved
    assert p.repo.get_task_draft(p.bob, "school")["draft"] is None
    with p.base.connect() as db:
        blob = db.execute(
            "SELECT payload_blob FROM web_task_drafts WHERE user_id=?", (p.alice,)
        ).fetchone()[0]
    assert "Keep whitespace" not in blob and "Private draft notes" not in blob
    assert p.base.unseal(blob) == value


def test_revisions_reject_another_tab_and_old_clear_without_losing_edits(drafts):
    p = drafts
    first = p.repo.save_task_draft(p.alice, "school", draft(), 0)
    with pytest.raises(Conflict):
        p.repo.save_task_draft(p.alice, "school", draft(title="old tab"), 0)
    changed = p.repo.save_task_draft(
        p.alice, "school", draft(title="newer edits"), first["version"]
    )
    with pytest.raises(Conflict):
        p.repo.clear_task_draft(p.alice, "school", first["version"])
    assert p.repo.get_task_draft(p.alice, "school") == changed


def test_clear_tombstone_blocks_late_save_and_revision_zero_aba(drafts):
    p = drafts
    first = p.repo.save_task_draft(p.alice, "school", draft(), 0)
    cleared = p.repo.clear_task_draft(p.alice, "school", first["version"])
    assert cleared["draft"] is None and cleared["version"] == 2
    for old in (0, first["version"]):
        with pytest.raises(Conflict):
            p.repo.save_task_draft(p.alice, "school", draft(title="late autosave"), old)
    assert p.repo.get_task_draft(p.alice, "school") == cleared
    restored = p.repo.save_task_draft(
        p.alice, "school", draft(title="new draft"), cleared["version"]
    )
    assert restored["version"] == 3


def test_clearing_initial_empty_draft_still_blocks_delayed_first_save(drafts):
    p = drafts
    cleared = p.repo.clear_task_draft(p.alice, "school", 0)
    assert cleared["version"] == 1 and cleared["draft"] is None
    with pytest.raises(Conflict):
        p.repo.save_task_draft(p.alice, "school", draft(), 0)


def test_expired_content_cannot_be_saved_with_old_revision_then_get_commits_tombstone(drafts):
    p = drafts
    first = p.repo.save_task_draft(p.alice, "school", draft(), 0)
    p.now[0] = first["expires_at"] - 1
    assert p.repo.get_task_draft(p.alice, "school") == first
    p.now[0] += 1
    with pytest.raises(Conflict):
        p.repo.save_task_draft(p.alice, "school", draft(title="expired request"), first["version"])
    expired = p.repo.get_task_draft(p.alice, "school")
    assert expired["draft"] is None and expired["version"] == 2 and expired["expires_at"] is None
    assert p.repo.save_task_draft(p.alice, "school", draft(), expired["version"])["version"] == 3


def test_prune_clears_expired_payload_only_and_retains_monotonic_tombstones(drafts):
    p = drafts
    alice = p.repo.save_task_draft(p.alice, "school", draft(), 0)
    p.now[0] += 10
    bob = p.repo.save_task_draft(p.bob, "school", draft(title="Bob"), 0)
    p.now[0] = alice["expires_at"]
    assert p.repo.prune_task_drafts() == 1
    assert p.repo.prune_task_drafts() == 0
    assert p.repo.get_task_draft(p.bob, "school") == bob
    assert p.repo.get_task_draft(p.alice, "school")["version"] == 2
    p.now[0] += DRAFT_RETENTION_SECONDS * 10
    p.repo.prune_task_drafts()
    assert p.repo.get_task_draft(p.alice, "school")["version"] == 2
    with pytest.raises(Conflict):
        p.repo.save_task_draft(p.alice, "school", draft(), 0)


@pytest.mark.parametrize("identity", ["wrong-tenant", "other-user-tenant", "inactive", "missing"])
def test_invalid_identity_cannot_read_save_or_clear_content(drafts, identity):
    p = drafts
    saved = p.repo.save_task_draft(p.alice, "school", draft(), 0)
    user, tenant = p.alice, "school"
    if identity == "wrong-tenant":
        tenant = "different-school"
    elif identity == "other-user-tenant":
        user = p.other
    elif identity == "inactive":
        with p.base.connect() as db:
            db.execute("UPDATE web_users SET active=0 WHERE id=?", (p.alice,))
    else:
        user = "missing-user"
    for operation in (
        lambda: p.repo.get_task_draft(user, tenant),
        lambda: p.repo.save_task_draft(user, tenant, draft(), 1),
        lambda: p.repo.clear_task_draft(user, tenant, 1),
    ):
        with pytest.raises(Missing):
            operation()
    with p.base.connect() as db:
        row = db.execute(
            "SELECT payload_blob,version FROM web_task_drafts WHERE user_id=?", (p.alice,)
        ).fetchone()
    assert p.base.unseal(row[0]) == saved["draft"] and row[1] == 1


def test_edit_drafts_require_owned_personal_task_but_keep_older_base_revision(drafts):
    p = drafts
    payload = {"title": "Task", "start_date": "2026-11-07", "end_date": "2026-11-07"}
    own = p.base.add_task(p.alice, payload)
    foreign = p.base.add_task(p.bob, payload)
    with pytest.raises(Missing):
        p.repo.save_task_draft(
            p.alice, "school", {**draft(), "task_id": foreign["id"], "base_version": 1}, 0
        )
    with pytest.raises(Missing):
        p.repo.save_task_draft(
            p.alice, "school", {**draft(), "task_id": "direct:teams-item", "base_version": 1}, 0
        )
    p.base.update_task(
        p.alice, own["id"], {**payload, "title": "Other device change"}, own["version"]
    )
    value = {
        **draft(title="Unsent local edits"),
        "task_id": own["id"],
        "base_version": own["version"],
    }
    assert p.repo.save_task_draft(p.alice, "school", value, 0)["draft"] == value


@pytest.mark.parametrize("version", [-1, True, 0.5, "1"])
def test_storage_refuses_invalid_revision_types(drafts, version):
    p = drafts
    with pytest.raises(ValueError):
        p.repo.save_task_draft(p.alice, "school", draft(), version)
    with pytest.raises(ValueError):
        p.repo.clear_task_draft(p.alice, "school", version)


@pytest.mark.parametrize(
    "value",
    [
        {"source": "teams", "fields": {}},
        {"task_id": "task", "fields": {}},
        {"base_version": 1, "fields": {}},
        {"fields": {"title": "x" * 501}},
        {"fields": {"notes": "x" * 10001}},
        {"fields": {"start_date": "2026-02-30"}},
        {"fields": {"selected_dates": ["2026-11-07", "invalid"]}},
        {"fields": {"time": "25:00"}},
        {"fields": {"display_mode": "weekly"}},
    ],
)
def test_draft_model_rejects_invalid_or_teams_input(value):
    with pytest.raises(ValidationError):
        TaskDraft.model_validate(value)


@pytest.fixture
def draft_api(tmp_path):
    key = Fernet.generate_key().decode()
    repo = DraftDatabase(tmp_path / "api.sqlite3", key)
    settings = Settings(database_path=repo.path, data_key=key, origin="http://localhost:8080")
    app = create_app(settings, repo)
    install_draft_routes(app, repo, app.state.require_session)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        # The test-server installs this existing private-validation boundary.
        return JSONResponse({"detail": "Check the input and dates"}, status_code=422)

    alice = repo.save_user("school", "alice", {"name": "Alice"})
    bob = repo.save_user("school", "bob", {"name": "Bob"})
    raw = repo.issue_session(alice, 3600)
    csrf = repo.session(raw)["csrf_token"]
    with TestClient(app, base_url=settings.origin) as client:
        yield SimpleNamespace(client=client, repo=repo, raw=raw, csrf=csrf, alice=alice, bob=bob)


def test_api_authentication_csrf_and_clear_conflict(draft_api):
    p = draft_api
    assert p.client.get("/api/task-draft").status_code == 401
    p.client.cookies.set("calendarsync_session", p.raw)
    assert p.client.get("/api/task-draft").json()["version"] == 0
    body = {"version": 0, "draft": draft()}
    assert p.client.put("/api/task-draft", json=body).status_code == 403
    headers = {"x-csrf-token": p.csrf, "origin": "http://wrong-site.example"}
    assert p.client.put("/api/task-draft", json=body, headers=headers).status_code == 403
    headers["origin"] = "http://localhost:8080"
    saved = p.client.put("/api/task-draft", json=body, headers=headers)
    assert saved.status_code == 200 and saved.json()["draft"] == body["draft"]
    assert p.client.put("/api/task-draft", json=body, headers=headers).status_code == 409
    cleared = p.client.delete("/api/task-draft?version=1", headers=headers)
    assert cleared.status_code == 200 and cleared.json()["version"] == 2
    assert (
        p.client.put("/api/task-draft", json={**body, "version": 1}, headers=headers).status_code
        == 409
    )
    assert p.client.get("/api/task-draft").json()["draft"] is None


def test_api_rejects_invalid_body_and_another_users_task(draft_api):
    p = draft_api
    p.client.cookies.set("calendarsync_session", p.raw)
    headers = {"x-csrf-token": p.csrf}
    rejected = p.client.put(
        "/api/task-draft",
        json={"version": 0, "draft": {**draft(notes="private invalid input"), "source": "teams"}},
        headers=headers,
    )
    assert rejected.status_code == 422
    assert "private invalid input" not in rejected.text
    foreign = p.repo.add_task(
        p.bob, {"title": "Task", "start_date": "2026-11-07", "end_date": "2026-11-07"}
    )
    rejected = p.client.put(
        "/api/task-draft",
        json={"version": 0, "draft": {**draft(), "task_id": foreign["id"], "base_version": 1}},
        headers=headers,
    )
    assert rejected.status_code == 404
    assert p.client.get("/api/task-draft").json()["draft"] is None


def test_api_expired_session_cannot_read_saved_draft(draft_api):
    p = draft_api
    p.repo.save_task_draft(p.alice, "school", draft(), 0)
    p.client.cookies.set("calendarsync_session", p.raw)
    p.repo.revoke_session(p.raw)
    assert p.client.get("/api/task-draft").status_code == 401
