"""Bounded complete-thread cases using fake Graph HTTP."""

import httpx
import pytest

import web_service.direct_source as module
from web_service.direct_source import DirectSource, TeamsGraph
from web_service.school_graph import BASE, GraphFailure

CHANNEL = {"id": "channel", "team_id": "team"}
ROOT = "/v1.0/teams/team/channels/channel/messages/root"
REPLIES = ROOT + "/replies"


def graph(handler):
    return TeamsGraph(lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def reply(number, content="reply"):
    return {"id": str(number), "replyToId": "root", "body": {"content": content}}


def test_complete_thread_reads_reply_after_first_page_and_normal_mode_is_preserved():
    calls = []

    def response(request):
        calls.append(str(request.url))
        if request.url.path == ROOT:
            return httpx.Response(200, json={"id": "root"})
        if request.url.params.get("page") == "second":
            return httpx.Response(200, json={"value": [reply(30, "last changed reply")]})
        return httpx.Response(
            200,
            json={
                "value": [reply(i) for i in range(30)],
                "@odata.nextLink": BASE
                + "/teams/team/channels/channel/messages/root/replies?page=second",
            },
        )

    api = graph(response)
    complete = api.message("token", CHANNEL, "root", complete_replies=True)
    assert len(complete["replies"]) == 31
    assert complete["replies"][-1]["body"]["content"] == "last changed reply"
    calls.clear()
    normal = api.message("token", CHANNEL, "root")
    assert len(normal["replies"]) == 30 and len(calls) == 2
    assert "$top=30" in calls[-1]
    assert (
        normal["replies@odata.nextLink"]
        == BASE + "/teams/team/channels/channel/messages/root/replies?page=second"
    )


@pytest.mark.parametrize(
    "next_link",
    [
        "https://example.invalid/v1.0/teams/team/channels/channel/messages/root/replies",
        BASE + "/teams/team/channels/other/messages/root/replies",
        BASE + "/teams/team/channels/channel/messages/other/replies",
        "",
        False,
        {"not": "a URL"},
    ],
)
def test_invalid_continuation_never_acknowledges_partial_thread(next_link):
    def response(request):
        if request.url.path == ROOT:
            return httpx.Response(200, json={"id": "root"})
        assert request.url.path == REPLIES
        return httpx.Response(200, json={"value": [reply(1)], "@odata.nextLink": next_link})

    with pytest.raises(GraphFailure):
        graph(response).message("token", CHANNEL, "root", complete_replies=True)


@pytest.mark.parametrize("failure", ["missing", "cycle", "too_many", "wrong_parent", "duplicate"])
def test_incomplete_threads_and_limits_fail_closed(failure):
    def response(request):
        if request.url.path == ROOT:
            return httpx.Response(200, json={"id": "root"})
        if failure == "missing":
            return httpx.Response(404)
        values = [reply(1)]
        body = {"value": values}
        if failure == "cycle":
            body["@odata.nextLink"] = (
                BASE + "/teams/team/channels/channel/messages/root/replies?$top=50"
            )
        if failure == "too_many":
            body["value"] = [reply(i) for i in range(501)]
        if failure == "wrong_parent":
            body["value"][0]["replyToId"] = "other"
        if failure == "duplicate":
            body["value"].append(reply(1))
        return httpx.Response(200, json=body)

    with pytest.raises(GraphFailure):
        graph(response).message("token", CHANNEL, "root", complete_replies=True)


def test_slow_final_page_is_not_marked_complete(monkeypatch):
    now = [0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])

    def response(request):
        if request.url.path == ROOT:
            return httpx.Response(200, json={"id": "root"})
        now[0] = 81
        return httpx.Response(200, json={"value": []})

    with pytest.raises(GraphFailure):
        graph(response).message("token", CHANNEL, "root", complete_replies=True, deadline=80)


class Cache:
    def cached_messages(self, channel):
        return {}

    def cache_analysis(self, *args):
        pass

    def catalog_events(self, channels):
        return []


class Extractor:
    configured = True

    def __init__(self):
        self.text = None

    def extract(self, text, images, previous):
        self.text = text
        return []


class TargetGraph:
    def __init__(self, replies):
        self.replies = replies

    def message(self, token, channel, root, *, complete_replies, deadline):
        assert complete_replies and deadline is not None
        return {"id": root, "body": {"content": "root"}, "replies": self.replies}


def test_targeted_source_includes_reply_after_thirtieth():
    extractor = Extractor()
    source = DirectSource(
        TargetGraph([reply(i) for i in range(30)] + [reply(30, "late-marker")]),
        extractor,
        images=False,
    )
    source.collect(
        "token",
        [CHANNEL],
        "tenant",
        Cache(),
        "user",
        selected_channels=[CHANNEL],
        selected_messages=["root"],
    )
    assert "late-marker" in extractor.text


def test_targeted_source_does_not_use_legacy_text_truncation():
    extractor = Extractor()
    source = DirectSource(
        TargetGraph([reply(1, "x" * 11000 + "end-marker")]), extractor, images=False
    )
    source.collect(
        "token",
        [CHANNEL],
        "tenant",
        Cache(),
        "user",
        selected_channels=[CHANNEL],
        selected_messages=["root"],
    )
    assert extractor.text.endswith("end-marker")


@pytest.mark.parametrize("replies", [[reply(1, "x" * 16001)], [reply(1, '<IMG src="image" />')]])
def test_targeted_context_overflow_or_reply_image_is_not_silently_ignored(replies):
    extractor = Extractor()
    with pytest.raises(GraphFailure):
        DirectSource(TargetGraph(replies), extractor, images=True).collect(
            "token",
            [CHANNEL],
            "tenant",
            Cache(),
            "user",
            selected_channels=[CHANNEL],
            selected_messages=["root"],
        )
    assert extractor.text is None
