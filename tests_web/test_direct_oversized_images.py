"""Oversized source images leave their post pending without blocking others."""

import httpx
import pytest
from support.fakes import Extractor, SharedCache, channels, message

from web_service.direct_source import DirectSource, TeamsGraph
from web_service.school_graph import BASE, GraphFailure


class ImageBytes(httpx.SyncByteStream):
    def __init__(self, size):
        self.size = size

    def __iter__(self):
        yield b"x" * 1_500_000
        yield b"x" * (self.size - 1_500_000)


class ImageGraph(TeamsGraph):
    def __init__(self, candidates, *, size=3_000_001, status=200):
        self.reads = []
        self.image_reads = []

        def handler(request):
            self.image_reads.append(request.url.path)
            return httpx.Response(
                status, headers={"Content-Type": "image/png"}, stream=ImageBytes(size)
            )

        super().__init__(lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        image_url = BASE + self.message_path(candidates[0], "bad") + "/hostedContents/image/$value"
        self.bad = message("bad", '<img src="' + image_url + '">')
        self.values = {candidates[0]["id"]: [self.bad, message("good")]}
        if len(candidates) > 1:
            self.values[candidates[1]["id"]] = [message("other-channel")]

    def messages(self, token, channel):
        self.reads.append(channel["id"])
        return list(self.values[channel["id"]])

    def message(self, token, channel, identity, *, complete_replies=False, deadline=None):
        assert complete_replies and deadline is not None
        assert identity == "bad"
        return self.bad


def prior_analysis(cache, channel):
    prior = {
        "version": "prior-version",
        "events": [
            {
                "id": "prior-event",
                "channel_id": channel["id"],
                "title": "Prior image deadline",
                "cancelled": False,
                "source_version": "prior-version",
            }
        ],
    }
    cache.values[channel["id"]] = {"bad": prior}
    return prior


def test_oversized_image_preserves_post_analysis_and_continues_later_posts_and_channels():
    candidates, cache, extractor = channels(2), SharedCache(), Extractor()
    prior = prior_analysis(cache, candidates[0])
    graph = ImageGraph(candidates)
    result = DirectSource(graph, extractor).collect("token", candidates, "tenant", cache, "alice")
    assert cache.values[candidates[0]["id"]]["bad"] is prior
    assert cache.writes == [(candidates[0]["id"], "good"), (candidates[1]["id"], "other-channel")]
    assert graph.reads == [channel["id"] for channel in candidates] and len(graph.image_reads) == 1
    assert len(extractor.calls) == 2 and all(
        "<img" not in text for text, previous in extractor.calls
    )
    assert len(result) == 3 and result[0] == prior["events"][0] and result[0]["cancelled"] is False
    assert result.report["partial"] is True and result.report["messages_failed"] == 1
    assert result.report["messages_seen"] == 3 and result.report["messages_analyzed"] == 2
    assert cache.finishes == [
        (candidates[0]["id"], "lease-1", False),
        (candidates[1]["id"], "lease-2", True),
    ]
    assert candidates[0]["id"] not in cache.fresh_until and candidates[1]["id"] in cache.fresh_until
    assert cache.leases == {}


@pytest.mark.parametrize("exact", [False, True])
def test_selected_and_exact_work_still_fail_without_acknowledging_incomplete_post(exact):
    candidates, cache, extractor = channels(1), SharedCache(), Extractor()
    prior = prior_analysis(cache, candidates[0])
    graph = ImageGraph(candidates)
    selected = {"selected_channels": candidates}
    if exact:
        selected["selected_messages"] = ["bad"]
    with pytest.raises(GraphFailure) as error:
        DirectSource(graph, extractor).collect(
            "token", candidates, "tenant", cache, "alice", **selected
        )
    assert error.value.state == "source_unavailable"
    assert cache.values[candidates[0]["id"]]["bad"] is prior and cache.writes == []
    assert extractor.calls == [] and cache.finishes == [(candidates[0]["id"], "lease-1", False)]
    assert cache.leases == {} and cache.fresh_until == {}


@pytest.mark.parametrize("status,state", [(401, "reauth_required"), (403, "verification_failed")])
def test_image_auth_failures_still_stop_all_collection(status, state):
    candidates, cache, extractor = channels(2), SharedCache(), Extractor()
    prior = prior_analysis(cache, candidates[0])
    graph = ImageGraph(candidates, status=status)
    with pytest.raises(GraphFailure) as error:
        DirectSource(graph, extractor).collect("token", candidates, "tenant", cache, "alice")
    assert error.value.state == state and graph.reads == [candidates[0]["id"]]
    assert cache.values[candidates[0]["id"]]["bad"] is prior and cache.writes == []
    assert extractor.calls == [] and cache.finishes == [(candidates[0]["id"], "lease-1", False)]
    assert cache.leases == {} and cache.fresh_until == {}


@pytest.mark.parametrize("size", [3_000_000, 3_000_001])
def test_hosted_image_stream_retains_existing_three_megabyte_boundary(size):
    candidates = channels(1)
    graph = ImageGraph(candidates, size=size)
    url = BASE + graph.message_path(candidates[0], "bad") + "/hostedContents/image/$value"
    if size > 3_000_000:
        with pytest.raises(GraphFailure) as error:
            graph.hosted_image("token", candidates[0], "bad", url)
        assert error.value.state == "source_unavailable"
    else:
        image = graph.hosted_image("token", candidates[0], "bad", url)
        prefix = "data:image/png;base64,"
        assert image.startswith(prefix) and len(image) == len(prefix) + 4_000_000
