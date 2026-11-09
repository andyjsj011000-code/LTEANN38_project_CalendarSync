"""Incomplete bodies preserve schedules; deleted replies cannot abort ingestion."""

from copy import deepcopy

import pytest

from test_direct_ingestion_progress import channels, message
from test_direct_input_completeness import InputGraph, Recorder, legacy_version, seed
from test_direct_shared_ingestion import SharedCache
from test_ingestion_status_orchestration import tracked_server
from web_service.direct_source import DirectSource
from web_service.school_graph import GraphFailure


def incomplete_message(kind):
    root = message("post")
    if kind == "root_null":
        root["body"]["content"] = None
    elif kind == "root_object":
        root["body"]["content"] = {"unexpected": "source"}
    elif kind == "reply_null":
        root["replies"] = [{"body": {"content": None}}]
    elif kind == "reply_number":
        root["replies"] = [{"body": {"content": 12}}]
    elif kind == "reply_missing":
        root["replies"] = [{"body": {}}]
    elif kind == "reply_body":
        root["replies"] = [{"body": "unexpected"}]
    elif kind == "reply_object":
        root["replies"] = [None]
    elif kind == "replies_object":
        root["replies"] = {"unexpected": "source"}
    return root


@pytest.mark.parametrize("images", [False, True])
@pytest.mark.parametrize(
    "kind",
    [
        "root_null",
        "root_object",
        "reply_null",
        "reply_number",
        "reply_missing",
        "reply_body",
        "reply_object",
        "replies_object",
    ],
)
def test_incomplete_body_preserves_even_same_version_cache_and_continues(kind, images):
    candidates, cache, extractor = channels(2), SharedCache(), Recorder()
    root = incomplete_message(kind)
    prior = seed(cache, candidates[0])
    prior["version"] = legacy_version(root)
    graph = InputGraph(candidates, root)
    graph.values[candidates[1]["id"]] = [message("other-channel")]

    result = DirectSource(graph, extractor, images=images).collect(
        "token", candidates, "tenant", cache, "alice"
    )

    assert cache.values[candidates[0]["id"]]["post"] is prior
    assert result[0] == prior["events"][0] and result[0]["cancelled"] is False
    assert cache.writes == [(candidates[0]["id"], "good"), (candidates[1]["id"], "other-channel")]
    assert len(extractor.calls) == 2
    assert result.report["partial"] is True and result.report["messages_failed"] == 1
    assert result.report["messages_cached"] == 0
    assert candidates[0]["id"] not in cache.fresh_until
    assert candidates[1]["id"] in cache.fresh_until and cache.leases == {}


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("kind", ["root_null", "reply_null", "reply_body"])
def test_incomplete_selected_body_does_not_acknowledge_or_overwrite(kind, exact):
    candidates, cache, extractor = channels(1), SharedCache(), Recorder()
    prior = seed(cache, candidates[0])
    graph = InputGraph(candidates, incomplete_message(kind))
    arguments = {"selected_channels": candidates}
    if exact:
        arguments["selected_messages"] = ["post"]

    with pytest.raises(GraphFailure) as error:
        DirectSource(graph, extractor).collect(
            "token", candidates, "tenant", cache, "alice", **arguments
        )

    assert error.value.state == "source_unavailable"
    assert cache.values[candidates[0]["id"]]["post"] is prior
    assert cache.writes == extractor.calls == []
    assert cache.fresh_until == cache.leases == {}


@pytest.mark.parametrize("content", [None, "REMOVED_DEADLINE", '<img src="old-image">'])
@pytest.mark.parametrize("long_body", [False, True])
def test_deleted_reply_is_excluded_and_old_cached_input_is_reanalyzed_once(content, long_body):
    candidates, cache, extractor = channels(1), SharedCache(), Recorder()
    root = message("post", "x" * 11000 if long_body else "Root deadline")
    root["replies"] = [
        {"body": {"content": content}, "deletedDateTime": "2026-11-07T00:00:00Z"},
        {"body": {"content": "CURRENT_REPLY"}},
    ]
    prior = seed(cache, candidates[0])
    prior["version"] = legacy_version(root)
    if long_body:
        from web_service.storage import digest

        prior["version"] = digest(prior["version"] + "|complete-input-v1")
    graph = InputGraph(candidates, root)
    graph.values[candidates[0]["id"]] = [root]
    source = DirectSource(graph, extractor)

    first = source.collect("token", candidates, "tenant", cache, "alice")
    corrected = cache.values[candidates[0]["id"]]["post"]["version"]
    second = source.collect(
        "token",
        candidates,
        "tenant",
        cache,
        "alice",
        selected_channels=candidates,
        selected_messages=["post"],
    )

    assert first.report["partial"] is False and first.report["messages_analyzed"] == 1
    assert corrected != prior["version"] and len(extractor.calls) == 1
    text, images, _ = extractor.calls[0]
    assert "CURRENT_REPLY" in text and "REMOVED_DEADLINE" not in text
    assert "<img" not in text and images == []
    assert second.report["messages_cached"] == 1 and second.report["partial"] is False


def test_deleted_reply_can_cancel_its_old_deadline_after_complete_analysis():
    candidates, cache, extractor = channels(1), SharedCache(), Recorder()
    prior = seed(cache, candidates[0])
    root = message("post", "No remaining deadline")
    root["replies"] = [{"body": {"content": None}, "deletedDateTime": "2026-11-07T00:00:00Z"}]
    graph = InputGraph(candidates, root)
    graph.values[candidates[0]["id"]] = [root]
    extractor.extract = lambda *args: []

    result = DirectSource(graph, extractor).collect("token", candidates, "tenant", cache, "alice")

    assert result.report["partial"] is False
    assert result[0]["id"] == prior["events"][0]["id"] and result[0]["cancelled"] is True


def test_null_reply_keeps_real_snapshot_connected_with_partial_progress(tracked_server):
    s = tracked_server
    # Exercise the shared server orchestration with the existing SQLite cache.
    # Channel lease fencing has separate native PostgreSQL integration coverage.
    s.graph.messages = lambda *args: [
        {**message("cached"), "replies": [{"body": {"content": None}}]},
        message("good"),
    ]
    extractor = Recorder()
    source = DirectSource(s.graph, extractor)
    collect = source.collect

    def sqlite_collect(token, candidates, tenant, db, user, **options):
        # SQLite has no shared-channel leases; keep the surrounding server's
        # shared cache publication and permission checks enabled.
        class CacheAdapter:
            shared_ingestion_enabled = False

            def __getattr__(self, name):
                return getattr(db, name)

        return collect(token, candidates, tenant, CacheAdapter(), user, **options)

    source.collect = sqlite_collect
    s.integration.source = source
    prior = deepcopy(s.db.catalog_events(["class"]))

    s.integration.run_sync(s.device, ingest=True)

    status = s.db.ingestion_status(s.device)
    assert s.db.sync_status(s.device["user_id"])["state"] == "connected"
    assert s.db.channels(s.device["user_id"], s.device["tenant_id"])
    assert status["state"] == "partial" and status["error_code"] is None
    assert status["last_success_at"] is None and status["counts"]["messages_failed"] == 1
    current = s.db.catalog_events(["class"])
    assert all(item in current for item in prior)
    assert len(extractor.calls) == 1
