"""Previously published channels survive independent collection and verification."""

import pytest
from test_server import server as server
from test_shared_ingestion_server import DeferredTasks, enable_shared
from test_test_progressive_cache import cache, event
from test_test_progressive_cache import progress_db as progress_db

from web_service.school_graph import GraphFailure
from web_service.storage import PermissionPending, digest


def test_collection_preserves_previous_channels_and_publishes_each_new_result(server, monkeypatch):
    client, session, device = enable_shared(server)
    original = server.db.catalog_events(["class"])[0]
    original_channels = server.graph.channels

    def channels(*args):
        first = original_channels(*args)
        return first + [{**first[0], "id": f"class-{i}", "name": f"Class {i}"} for i in range(1, 5)]

    monkeypatch.setattr(server.graph, "channels", channels)
    for index in range(1, 4):
        item = {**original, "channel_id": f"class-{index}", "id": f"direct:existing-{index}"}
        server.db.cache_analysis(
            digest(f"class-{index}|post"), f"class-{index}", "post", "v1", [item]
        )
    tasks = DeferredTasks()
    server.integration.sync(session, tasks)
    before = server.db.sync_status(session["user_id"])

    def collect(*args):
        first = client.get("/api/calendar?start=2026-10-01&end=2026-10-31").json()["items"]
        assert {item["id"] for item in first} == {
            "direct:cached",
            *[f"direct:existing-{i}" for i in range(1, 4)],
        }
        new = {
            **original,
            "channel_id": "class-4",
            "id": "direct:new",
            "title": "New channel result",
        }
        server.db.cache_analysis(digest("class-4|new"), "class-4", "new", "v1", [new])
        second = client.get("/api/calendar?start=2026-10-01&end=2026-10-31").json()["items"]
        assert len(second) == 5
        assert server.db.sync_status(session["user_id"]) == before
        raise GraphFailure("source_unavailable")

    monkeypatch.setattr(server.integration.source, "collect", collect)
    function, args, kwargs = tasks.items[0]
    function(*args, **kwargs)
    assert server.db.sync_status(session["user_id"]) == before
    assert server.graph.calls == 1
    assert len(client.get("/api/today").json()["teams"]) == 5
    assert server.db.device(device["id_hash"])["ingest_at"] == 0


def test_long_collection_never_renews_or_revokes_permission_and_reads_reverify(server, monkeypatch):
    client, session, device = enable_shared(server)
    tasks = DeferredTasks()
    server.integration.sync(session, tasks)
    before = server.db.sync_status(session["user_id"])
    original_collect = server.integration.source.collect

    def collect(*args):
        server.now[0] += 81
        return original_collect(*args)

    monkeypatch.setattr(server.integration.source, "collect", collect)
    function, args, kwargs = tasks.items[0]
    function(*args, **kwargs)
    assert server.graph.calls == 1
    assert server.db.sync_status(session["user_id"]) == before
    assert [row["title"] for row in server.db.catalog_events(["class"])] == ["Cached deadline"]
    assert client.get("/api/today").json()["teams"][0]["title"] == "Cached deadline"
    assert server.graph.calls == 2


def test_permission_wait_allows_only_existing_client_snapshot_not_new_server_data(
    server, monkeypatch
):
    client, session, device = enable_shared(server)
    server.integration.run_sync(device)
    server.now[0] += 61

    def unavailable(*args):
        raise GraphFailure("verification_failed")

    monkeypatch.setattr(server.graph, "channels", unavailable)
    response = client.get("/api/calendar?start=2026-10-01&end=2026-10-31")
    assert response.status_code == 503
    assert response.json()["retain_teams_snapshot"] is True
    assert "items" not in response.json()
    assert server.db.channels(session["user_id"], session["tenant_id"]) == []


def test_independent_verification_removes_revoked_channel_without_destroying_catalog(server):
    client, session, device = enable_shared(server)
    server.integration.run_sync(device, ingest=True)
    server.graph.allowed = False
    server.integration.run_sync(device, force=True)
    assert client.get("/api/today").json()["teams"] == []
    assert server.db.catalog_events(["class"])


def test_unedited_source_updates_and_cancellations_preserve_personal_state(progress_db):
    p = progress_db
    first = event()
    cache(p, first)
    p.repo.materialize_cached_events(p.device)
    p.repo.event_state(p.user, "tenant", first["id"], {"completed": True, "hidden": True})
    updated = {**first, "title": "New title", "source_version": "v2"}
    cache(p, updated)
    p.repo.materialize_cached_events(p.device)
    current = p.repo._shared(p.user, "tenant")[0]
    assert current["title"] == "New title" and current["completed"] and current["hidden"]
    assert p.repo.changes(p.user, "tenant") == []
    cache(p, {**updated, "cancelled": True, "source_version": "deleted"})
    p.repo.materialize_cached_events(p.device)
    assert p.repo._shared(p.user, "tenant")[0]["cancelled"] is True
    cache(p, {**updated, "source_version": "restored"})
    p.repo.materialize_cached_events(p.device)
    assert p.repo._shared(p.user, "tenant")[0]["cancelled"] is False


def test_confirmed_deletion_applies_even_to_locally_edited_event(progress_db):
    p = progress_db
    first = event()
    cache(p, first)
    p.repo.materialize_cached_events(p.device)
    with p.repo.connect() as db:
        db.execute(
            "UPDATE web_event_states SET override_blob=? WHERE user_id=? AND event_id=?",
            (p.repo.seal({**first, "title": "My own title"}), p.user, first["id"]),
        )
    cache(p, {**first, "cancelled": True, "source_version": "verified-deletion"})
    p.repo.materialize_cached_events(p.device)
    current = p.repo._shared(p.user, "tenant")[0]
    assert current["title"] == "My own title" and current["cancelled"] is True
    assert len(p.repo.changes(p.user, "tenant")) == 1


def test_absence_from_later_partial_catalog_is_not_a_deletion(progress_db):
    p = progress_db
    cache(p, event())
    p.repo.materialize_cached_events(p.device)
    with p.repo.connect() as db:
        db.execute("DELETE FROM web_source_cache")
    cache(p, event("another"))
    p.repo.materialize_cached_events(p.device)
    rows = p.repo._shared(p.user, "tenant")
    assert {row["id"] for row in rows} == {"direct:first", "direct:another"}
    assert not any(row["cancelled"] for row in rows)


def test_reverified_scope_change_retains_only_still_authorized_snapshot_channels(server):
    _, session, device = enable_shared(server)
    server.integration.run_sync(device)

    def crossed_scope():
        server.graph.allowed = False
        server.integration.run_sync(device, force=True)
        return {"items": [{"title": "Old channel"}]}

    with pytest.raises(PermissionPending) as caught:
        server.integration.read_teams(session, crossed_scope)
    assert caught.value.retain_snapshot is True
    assert caught.value.snapshot_channels == []


def test_cached_permission_routes_skip_graph_on_repeated_manual_collection(server):
    _, session, device = enable_shared(server)
    server.integration.run_sync(device)
    calls = server.graph.calls
    tasks = DeferredTasks()
    server.integration.sync(session, tasks)
    assert server.graph.calls == calls
    function, args, kwargs = tasks.items[0]
    function(*args, **kwargs)
    assert server.graph.calls == calls
