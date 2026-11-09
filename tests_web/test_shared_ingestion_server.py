"""Shared ingestion serves verified cache before deferred source processing."""

import pytest

from test_server import server
from test_ingestion_status import CatalogSource, seed_catalog, session_device
from web_service.school_graph import GraphFailure
from web_service.storage import Missing, digest


class DeferredTasks:
    def __init__(self):
        self.items = []

    def add_task(self, function, *args, **kwargs):
        self.items.append((function, args, kwargs))


def enable_shared(server):
    server.db.shared_ingestion_enabled = True
    server.integration.source = CatalogSource()
    client = server.client()
    session, device = session_device(server, client)
    seed_catalog(server)
    return client, session, device


def test_manual_sync_publishes_only_authorized_cache_before_queuing_work(server):
    _, session, device = enable_shared(server)
    server.db.mark_ingested(device["id_hash"])
    server.db.defer_device(device["id_hash"], 300)
    tasks = DeferredTasks()

    result = server.integration.sync(session, tasks)

    assert result["state"] == "connected"
    assert result["ingestion"] == {"state": "queued"}
    assert server.integration.source.calls == 0
    assert len(tasks.items) == 1
    assert tasks.items[0][0] == server.integration.run_deferred_ingestion
    current = server.db.device(device["id_hash"])
    assert current["ingest_at"] == 0
    assert current["next_sync"] == server.now[0]
    events = server.db.agenda(session["user_id"], session["tenant_id"], "2026-10-22")["teams"]
    assert [event["title"] for event in events] == ["Cached deadline"]


def test_background_collection_releases_snapshot_lease_and_allows_permission_rechecks(
    server, monkeypatch
):
    client, session, device = enable_shared(server)
    original_collect = server.integration.source.collect

    def collect(*args):
        state = server.db.sync_status(session["user_id"])
        assert state["state"] == "connected" and state["lease_until"] == 0
        assert server.db.channels(session["user_id"], session["tenant_id"])
        # A long analysis must not prevent the ordinary access recheck after
        # the short permission TTL, or erase the existing authorized agenda.
        server.now[0] += 61
        assert [event["title"] for event in client.get("/api/today").json()["teams"]] == [
            "Cached deadline"
        ]
        return original_collect(*args)

    monkeypatch.setattr(server.integration.source, "collect", collect)
    report = server.integration.run_sync(device, ingest=True)

    assert report["state"] == "completed"
    assert server.graph.calls == 3  # Initial proof, view recheck, final proof.
    assert server.db.device(device["id_hash"])["ingest_at"] == server.now[0] + 300


def test_final_snapshot_rechecks_membership_after_background_collection(server, monkeypatch):
    _, session, device = enable_shared(server)
    original_collect = server.integration.source.collect

    def collect(*args):
        result = original_collect(*args)
        server.graph.allowed = False
        return result

    monkeypatch.setattr(server.integration.source, "collect", collect)
    report = server.integration.run_sync(device, ingest=True)

    assert report["state"] == "completed"
    assert server.graph.calls == 2
    assert server.db.channels(session["user_id"], session["tenant_id"]) == []
    assert server.db.agenda(session["user_id"], session["tenant_id"], "2026-10-22")["teams"] == []


def test_revoked_device_cannot_publish_final_background_snapshot(server, monkeypatch):
    _, session, device = enable_shared(server)
    original_collect = server.integration.source.collect
    marked = []

    def collect(*args):
        result = original_collect(*args)
        server.db.revoke_device(device["id_hash"])
        return result

    monkeypatch.setattr(server.integration.source, "collect", collect)
    monkeypatch.setattr(server.db, "mark_ingested", marked.append)

    assert server.integration.run_sync(device, ingest=True) is None
    assert marked == []
    assert server.db.sync_status(session["user_id"])["state"] == "reauth_required"
    assert server.db.channels(session["user_id"], session["tenant_id"]) == []


@pytest.mark.parametrize("failure", ["verification_failed", "source_unavailable"])
def test_interrupted_collection_reverifies_and_retains_authorized_cache_without_completing(
    server, monkeypatch, failure
):
    client, session, device = enable_shared(server)
    marked = []

    def collect(*args):
        assert server.db.sync_status(session["user_id"])["state"] == "connected"
        raise GraphFailure(failure)

    monkeypatch.setattr(server.integration.source, "collect", collect)
    monkeypatch.setattr(server.db, "mark_ingested", marked.append)

    report = server.integration.run_sync(device, ingest=True)

    assert report == {
        "channels_total": 1,
        "channels_scanned": 0,
        "messages_seen": 0,
        "messages_analyzed": 0,
        "messages_cached": 0,
        "messages_failed": 0,
        "partial": True,
        "state": "partial",
    }
    assert marked == [] and server.graph.calls == 2
    assert server.db.sync_status(session["user_id"])["state"] == "connected"
    assert [event["title"] for event in client.get("/api/today").json()["teams"]] == [
        "Cached deadline"
    ]
    assert server.db.device(device["id_hash"])["ingest_at"] == 0


@pytest.mark.parametrize("failure", ["reauth_required", "account_not_allowed", "unexpected"])
def test_auth_and_unexpected_collection_failures_do_not_use_cache_recovery(
    server, monkeypatch, failure
):
    _, session, device = enable_shared(server)
    marked = []

    def collect(*args):
        if failure == "unexpected":
            raise ValueError("Unexpected source contract failure")
        raise GraphFailure(failure)

    monkeypatch.setattr(server.integration.source, "collect", collect)
    monkeypatch.setattr(server.db, "mark_ingested", marked.append)

    assert server.integration.run_sync(device, ingest=True) is None
    assert server.graph.calls == 1 and marked == []
    assert server.db.sync_status(session["user_id"])["state"] == (
        "source_unavailable" if failure == "unexpected" else failure
    )
    assert server.db.channels(session["user_id"], session["tenant_id"]) == []


@pytest.mark.parametrize("busy", [False, True])
def test_manual_sync_does_not_queue_without_successful_permission_commit(server, monkeypatch, busy):
    _, session, device = enable_shared(server)
    tasks = DeferredTasks()
    queued = []
    monkeypatch.setattr(server.db, "request_ingestion", queued.append)
    if busy:
        assert server.db.begin_sync(session["user_id"], 0)
    else:

        def channels(*args):
            raise GraphFailure("verification_failed")

        monkeypatch.setattr(server.graph, "channels", channels)

    result = server.integration.sync(session, tasks)

    assert result["state"] == ("checking" if busy else "verification_failed")
    assert result.get("ingestion") == ({"state": "busy"} if busy else None)
    assert tasks.items == [] and queued == []
    assert server.integration.source.calls == 0


def test_final_commit_failure_does_not_mark_background_ingestion_complete(server, monkeypatch):
    _, session, device = enable_shared(server)
    original_commit = server.db.commit_snapshot
    commits = []
    marked = []

    def commit(*args):
        commits.append(1)
        if len(commits) == 2:
            raise Missing()
        return original_commit(*args)

    monkeypatch.setattr(server.db, "commit_snapshot", commit)
    monkeypatch.setattr(server.db, "mark_ingested", marked.append)

    assert server.integration.run_sync(device, ingest=True) is None
    assert len(commits) == 2 and marked == []
    assert server.db.sync_status(session["user_id"])["state"] == "source_unavailable"


def test_worker_picks_up_durable_request_without_response_background_task(server):
    _, session, device = enable_shared(server)
    result = server.integration.sync(session)
    assert result["ingestion"] == {"state": "queued"}
    assert server.integration.source.calls == 0

    server.integration.tick()

    assert server.integration.source.calls == 1
    assert server.db.device(device["id_hash"])["ingest_at"] == server.now[0] + 300
    assert server.db.sync_status(session["user_id"])["state"] == "connected"


def test_shared_manual_route_returns_queued_and_executes_deferred_callback(server):
    client, _, device = enable_shared(server)

    response = client.post("/api/teams/sync")

    assert response.status_code == 200
    assert response.json()["ingestion"] == {"state": "queued"}
    # TestClient waits for response background callbacks before returning.
    assert server.integration.source.calls == 1
    assert server.db.device(device["id_hash"])["ingest_at"] == server.now[0] + 300


def test_deferred_database_failure_preserves_worker_retry_without_failing_response(
    server, monkeypatch
):
    _, session, device = enable_shared(server)
    assert server.integration.sync(session)["ingestion"] == {"state": "queued"}
    audits = []

    def unavailable(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(server.integration, "run_sync", unavailable)
    monkeypatch.setattr(server.db, "audit", lambda event, user: audits.append((event, user)))

    server.integration.run_deferred_ingestion(device)

    assert audits == [("worker_failed", session["user_id"])]
    assert server.db.device(device["id_hash"])["ingest_at"] == 0


def test_queued_collection_reuses_foreground_proof_and_keeps_cache_readable_during_final_graph(
    server, monkeypatch
):
    client, session, device = enable_shared(server)
    tasks = DeferredTasks()
    result = server.integration.sync(session, tasks)
    assert result["ingestion"] == {"state": "queued"}
    assert server.graph.calls == 1
    assert "prepared" not in result and "private-access-token" not in str(result)
    original_collect = server.integration.source.collect
    original_channels = server.graph.channels
    observations = []

    def observe(stage):
        state = server.db.sync_status(session["user_id"])
        observations.append(
            (stage, state["state"], state["lease_until"] > server.now[0], server.graph.calls)
        )
        assert [event["title"] for event in client.get("/api/today").json()["teams"]] == [
            "Cached deadline"
        ]

    def collect(*args):
        observe("collect")
        return original_collect(*args)

    def channels(*args):
        # Even a fully cached collection reaches this immediately. The old
        # proof must stay readable throughout this potentially slow Graph call.
        observe("final_graph")
        lease = server.db.sync_status(session["user_id"])["lease_until"]
        # A newer proof cannot race this Graph call and then be overwritten by
        # its older result: the replacement proof owns the user's sync lease.
        assert server.db.begin_sync(session["user_id"], 0) is False
        assert server.db.sync_status(session["user_id"])["lease_until"] == lease
        return original_channels(*args)

    monkeypatch.setattr(server.integration.source, "collect", collect)
    monkeypatch.setattr(server.graph, "channels", channels)
    function, args, kwargs = tasks.items[0]
    function(*args, **kwargs)

    assert observations == [
        ("collect", "connected", False, 1),
        ("final_graph", "checking", True, 1),
    ]
    assert server.graph.calls == 2
    assert server.db.sync_status(session["user_id"])["state"] == "connected"
    assert server.db.device(device["id_hash"])["ingest_at"] == server.now[0] + 300


def test_expired_prepared_proof_is_verified_again_before_collecting(server, monkeypatch):
    _, session, device = enable_shared(server)
    tasks = DeferredTasks()
    server.integration.sync(session, tasks)
    server.now[0] += server.settings.access_seconds + 1
    original_collect = server.integration.source.collect
    calls_at_collection = []

    def collect(*args):
        calls_at_collection.append(server.graph.calls)
        return original_collect(*args)

    monkeypatch.setattr(server.integration.source, "collect", collect)
    function, args, kwargs = tasks.items[0]
    function(*args, **kwargs)

    assert calls_at_collection == [2]
    assert server.graph.calls == 3
    assert server.db.device(device["id_hash"])["ingest_at"] == server.now[0] + 300


def test_queued_prepared_proof_does_not_bypass_device_revocation(server):
    _, session, device = enable_shared(server)
    tasks = DeferredTasks()
    server.integration.sync(session, tasks)
    server.db.revoke_device(device["id_hash"])
    function, args, kwargs = tasks.items[0]

    function(*args, **kwargs)

    assert server.integration.source.calls == 0
    assert server.db.sync_status(session["user_id"])["state"] == "reauth_required"
    assert server.db.channels(session["user_id"], session["tenant_id"]) == []


def test_final_graph_failure_revokes_preserved_access(server, monkeypatch):
    _, session, device = enable_shared(server)
    tasks = DeferredTasks()
    server.integration.sync(session, tasks)

    def channels(*args):
        assert server.db.channels(session["user_id"], session["tenant_id"])
        raise GraphFailure("verification_failed")

    monkeypatch.setattr(server.graph, "channels", channels)
    function, args, kwargs = tasks.items[0]
    function(*args, **kwargs)

    assert server.db.sync_status(session["user_id"])["state"] == "verification_failed"
    assert server.db.channels(session["user_id"], session["tenant_id"]) == []
    assert server.db.device(device["id_hash"])["ingest_at"] == 0


def test_reads_publish_each_finished_post_without_waiting_for_collection_or_new_graph(server):
    client, session, device = enable_shared(server)
    server.integration.run_sync(device)
    calls = server.graph.calls
    before = server.db.sync_status(session["user_id"])
    event = server.db.catalog_events(["class"])[0]
    server.db.cache_analysis(
        digest("class|new"),
        "class",
        "new",
        "v1",
        [{**event, "id": "direct:new", "title": "Just analyzed"}],
    )
    response = client.get("/api/today")
    assert response.status_code == 200
    assert {item["title"] for item in response.json()["teams"]} == {
        "Cached deadline",
        "Just analyzed",
    }
    assert server.graph.calls == calls and server.integration.source.calls == 0
    assert server.db.sync_status(session["user_id"]) == before
    assert client.get("/api/settings").json()["connection"]["ingestion_pending"] is True
    server.db.mark_ingested(device["id_hash"])
    assert client.get("/api/settings").json()["connection"]["ingestion_pending"] is False
