"""Verified-channel shared collection, ownership fences and retired-key hints."""

import json
import threading

import pytest
from support.fakes import Extractor, Graph, SharedCache, channels, message

import web_service.direct_source as module
from web_service.direct_source import DirectSource
from web_service.school_graph import GraphFailure
from web_service.storage import digest


def test_same_users_devices_cannot_count_duplicate_cursor_visits_as_completion():
    candidates = channels(2)
    second_entered, second_release = threading.Event(), threading.Event()
    results, failures = {}, []

    class InterleavedGraph(Graph):
        def messages(self, token, channel):
            if token == "device-b":
                second_entered.set()
                if not second_release.wait(5):
                    raise AssertionError("Second device did not resume")
            return super().messages(token, channel)

    class InterleavedCache(SharedCache):
        first_visits = 0

        def next_channels(self, user, candidates, count):
            if threading.current_thread() is first_thread:
                self.first_visits += 1
                if self.first_visits == 2:
                    second.start()
                    if not second_entered.wait(5):
                        raise AssertionError("Second device did not claim its channel")
            return super().next_channels(user, candidates, count)

    first_thread = threading.current_thread()
    cache, extractor = InterleavedCache(), Extractor()
    graph = InterleavedGraph({row["id"]: [message(row["id"])] for row in candidates})
    source = DirectSource(graph, extractor, images=False)

    def other_device():
        try:
            results["second"] = source.collect(
                "device-b", candidates, "tenant", cache, "same-user", on_progress=lambda _: None
            )
        except BaseException as error:
            failures.append(error)

    second = threading.Thread(target=other_device)
    try:
        first = source.collect(
            "device-a", candidates, "tenant", cache, "same-user", on_progress=lambda _: None
        )
        # B still owns the other channel. The first device's cursor has made a
        # full two-call turn, but only one distinct channel is actually ready.
        assert cache.visits == [candidates[0]["id"], candidates[1]["id"], candidates[0]["id"]]
        assert first.report["partial"] is True
        assert first.report["channels_total"] == 2
        assert first.report["channels_checked"] == 1
        assert first.report["channels_scanned"] == 1
        assert cache.fresh_until.keys() == {candidates[0]["id"]}
        assert len(extractor.calls) == 1
    finally:
        second_release.set()
        if second.ident is not None:
            second.join(5)
    assert not second.is_alive() and not failures
    assert results["second"].report["partial"] is False
    assert results["second"].report["channels_checked"] == 2
    assert results["second"].report["channels_scanned"] == 1
    assert len(extractor.calls) == 2
    assert graph.reads == [row["id"] for row in candidates]


@pytest.fixture(autouse=True)
def no_elapsed_time(monkeypatch):
    monkeypatch.setattr(module, "monotonic", lambda: 0)


def test_second_account_reuses_fresh_shared_results_and_empty_channel_marker():
    candidates, cache, extractor = channels(2), SharedCache(), Extractor()
    graph = Graph({candidates[0]["id"]: [message("one")]})
    source = DirectSource(graph, extractor, images=False)
    first = source.collect("token-a", candidates, "tenant", cache, "alice")
    assert len(first) == 1 and first.report["partial"] is False
    assert graph.reads == [row["id"] for row in candidates]
    assert len(cache.fresh_until) == 2
    second = source.collect("token-b", candidates, "tenant", cache, "bob")
    assert second == first and len(extractor.calls) == 1
    assert graph.reads == [row["id"] for row in candidates]
    assert second.report["channels_total"] == 2 and second.report["channels_scanned"] == 0
    assert second.report["partial"] is False


def test_progress_counts_fresh_channels_without_calling_them_new_scans():
    candidates, cache = channels(2), SharedCache()
    source = DirectSource(Graph({candidates[0]["id"]: [message("one")]}), Extractor(), images=False)
    source.collect("token", candidates, "tenant", cache, "alice")
    progress = []
    result = source.collect(
        "token", candidates, "tenant", cache, "bob", on_progress=progress.append
    )
    assert result.report["channels_scanned"] == 0 and result.report["channels_checked"] == 2
    assert progress[-1]["channels_checked"] == 2
    assert all(
        set(value)
        <= {
            "channels_total",
            "channels_scanned",
            "channels_checked",
            "messages_seen",
            "messages_analyzed",
            "messages_cached",
            "messages_failed",
            "partial",
        }
        for value in progress
    )


def test_failed_channel_is_not_counted_checked_and_progress_failure_is_isolated():
    candidates, cache = channels(1), SharedCache()
    source = DirectSource(
        Graph({candidates[0]["id"]: [message("bad", "bad-message")]}),
        Extractor(ValueError("bad response")),
        images=False,
    )

    def broken_callback(_):
        raise RuntimeError("Status database unavailable")

    result = source.collect(
        "token", candidates, "tenant", cache, "alice", on_progress=broken_callback
    )
    assert result.report["partial"] is True
    assert result.report["channels_scanned"] == 1 and result.report["channels_checked"] == 0
    assert result.report["messages_failed"] == 1


def test_only_new_or_stale_verified_channels_are_scanned_for_next_account():
    candidates, cache, extractor = channels(3), SharedCache(), Extractor()
    graph = Graph({row["id"]: [message(row["id"])] for row in candidates})
    source = DirectSource(graph, extractor, images=False)
    source.collect("token-a", candidates[:2], "tenant", cache, "alice")
    cache.fresh_until[candidates[1]["id"]] = cache.clock() - 1
    second = source.collect("token-b", candidates, "tenant", cache, "bob")
    assert graph.reads == [
        candidates[0]["id"],
        candidates[1]["id"],
        candidates[1]["id"],
        candidates[2]["id"],
    ]
    assert second.report["channels_scanned"] == 2 and second.report["partial"] is False
    assert len(extractor.calls) == 3  # Stale unchanged data reuses its message cache.
    assert second.report["messages_cached"] == 1


def test_busy_shared_channel_does_not_duplicate_extraction_or_claim_completion():
    candidates, cache = channels(1), SharedCache()
    graph = Graph({candidates[0]["id"]: [message("one")]})
    nested = []

    class ConcurrentExtractor(Extractor):
        def extract(self, text, images, previous):
            nested.append(source.collect("token-b", candidates, "tenant", cache, "bob"))
            return super().extract(text, images, previous)

    extractor = ConcurrentExtractor()
    source = DirectSource(graph, extractor, images=False)
    first = source.collect("token-a", candidates, "tenant", cache, "alice")
    assert len(extractor.calls) == 1 and graph.reads == [candidates[0]["id"]]
    assert nested[0].report["channels_scanned"] == 0 and nested[0].report["partial"] is True
    assert first.report["partial"] is False
    assert source.collect("token-b", candidates, "tenant", cache, "bob") == first
    assert len(extractor.calls) == 1


def test_failed_analysis_releases_lease_without_marking_channel_fresh():
    candidates, cache = channels(1), SharedCache()
    source = DirectSource(
        Graph({candidates[0]["id"]: [message("bad", "bad-message")]}),
        Extractor(ValueError("bad response")),
        images=False,
    )
    result = source.collect("token", candidates, "tenant", cache, "alice")
    assert result.report["messages_failed"] == 1 and result.report["partial"] is True
    assert cache.finishes[-1][2] is False and cache.leases == {} and cache.fresh_until == {}
    assert cache.writes == []


def test_deadline_partial_never_marks_channel_fresh(monkeypatch):
    now = [0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    candidates, cache = channels(2), SharedCache()

    class SlowExtractor(Extractor):
        def extract(self, text, images, previous):
            now[0] += 81
            return super().extract(text, images, previous)

    graph = Graph({candidates[0]["id"]: [message("first"), message("later")]})
    result = DirectSource(graph, SlowExtractor(), images=False).collect(
        "token", candidates, "tenant", cache, "alice"
    )
    assert result.report["partial"] is True and result.report["channels_scanned"] == 0
    assert cache.finishes[-1][2] is False and cache.fresh_until == {} and cache.leases == {}
    assert cache.visits == [candidates[0]["id"]]


def test_lost_write_fence_preserves_prior_cache_and_other_owner_lease():
    candidates, cache = channels(1), SharedCache()
    prior = {
        "version": "prior",
        "events": [
            {
                "id": "prior-event",
                "channel_id": candidates[0]["id"],
                "title": "Prior deadline",
                "cancelled": False,
            }
        ],
    }
    cache.values[candidates[0]["id"]] = {"one": prior}

    class ReplacedLeaseExtractor(Extractor):
        def extract(self, text, images, previous):
            cache.leases[candidates[0]["id"]] = "new-owner"
            return super().extract(text, images, previous)

    result = DirectSource(
        Graph({candidates[0]["id"]: [message("one")]}), ReplacedLeaseExtractor(), images=False
    ).collect("token", candidates, "tenant", cache, "alice")
    assert result == prior["events"] and result.report["partial"] is True
    assert result.report["channels_scanned"] == 0
    assert cache.values[candidates[0]["id"]]["one"] is prior and cache.writes == []
    assert cache.leases[candidates[0]["id"]] == "new-owner" and cache.fresh_until == {}


def test_lost_final_fence_does_not_report_fresh_complete_channel():
    candidates = channels(1)

    class LostFinishCache(SharedCache):
        def finish_channel_ingestion(self, channel, lease, success):
            self.leases[channel["id"]] = "new-owner"
            return False

    cache = LostFinishCache()
    result = DirectSource(Graph(), Extractor(), images=False).collect(
        "token", candidates, "tenant", cache, "alice"
    )
    assert result.report["partial"] is True and result.report["channels_scanned"] == 0
    assert cache.fresh_until == {}


@pytest.mark.parametrize("targeted", [False, True])
def test_selected_work_forces_freshness_zero_and_never_silently_acknowledges_busy(targeted):
    candidates, cache = channels(1), SharedCache()
    channel = candidates[0]["id"]
    cache.fresh_until[channel] = cache.clock() + 300

    class TargetGraph(Graph):
        def message(self, token, channel, identity, **kwargs):
            return message(identity)

    graph = TargetGraph({channel: [message("one")]})
    source = DirectSource(graph, Extractor(), images=False)
    kwargs = {"selected_channels": candidates}
    if targeted:
        kwargs["selected_messages"] = ["one"]
    result = source.collect("token", candidates, "tenant", cache, "alice", **kwargs)
    assert cache.claims[-1] == (channel, 0) and result.report["channels_scanned"] == 1
    assert cache.finishes[-1][2] is (not targeted)
    cache.leases[channel] = "another-owner"
    with pytest.raises(GraphFailure) as raised:
        source.collect("token", candidates, "tenant", cache, "alice", **kwargs)
    assert raised.value.state == "source_unavailable"


@pytest.mark.parametrize("state", ["reauth_required", "account_not_allowed", "verification_failed"])
def test_access_error_releases_owned_lease_and_still_propagates(state):
    candidates, cache = channels(1), SharedCache()
    source = DirectSource(
        Graph({candidates[0]["id"]: [message("bad", "bad-message")]}),
        Extractor(GraphFailure(state)),
        images=False,
    )
    with pytest.raises(GraphFailure) as raised:
        source.collect("token", candidates, "tenant", cache, "alice")
    assert raised.value.state == state and cache.leases == {} and cache.fresh_until == {}
    assert cache.finishes[-1][2] is False


def test_retired_keys_are_ai_context_only_and_never_emit_cancelled_events():
    candidates, cache, extractor = channels(1), SharedCache(), Extractor()
    channel, identity = candidates[0]["id"], "one"
    retired = {
        "id": "retired-event",
        "key": "deadline",
        "source_version": "old",
        "end_date": "2026-10-01",
    }
    cache.values[channel] = {identity: {"version": "old", "events": [], "retired": [retired]}}

    class EmptyExtractor(Extractor):
        def extract(self, text, images, previous):
            self.calls.append((text, previous))
            return []

    extractor = EmptyExtractor()
    result = DirectSource(Graph({channel: [message(identity)]}), extractor, images=False).collect(
        "token", candidates, "tenant", cache, "alice"
    )
    assert extractor.calls[0][1] == [retired]
    assert result == [] and cache.values[channel][identity]["events"] == []


def test_unchanged_retired_message_skips_ai_without_resurrecting_event():
    candidates, cache, extractor = channels(1), SharedCache(), Extractor()
    channel, root = candidates[0]["id"], message("one")
    version = digest(
        json.dumps(
            {
                key: root.get(key)
                for key in ("body", "subject", "replies", "deletedDateTime", "lastModifiedDateTime")
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    cache.values[channel] = {
        "one": {
            "version": version,
            "events": [],
            "retired": [{"id": "retired-event", "key": "deadline"}],
        }
    }
    result = DirectSource(Graph({channel: [root]}), extractor, images=False).collect(
        "token", candidates, "tenant", cache, "alice"
    )
    assert extractor.calls == [] and result == []
    assert result.report["messages_cached"] == 1 and result.report["partial"] is False
