import pytest

from web_service.direct_source import DirectSource
from web_service.school_graph import GraphFailure


def test_selected_channel_is_restricted_and_does_not_advance_polling_cursor():
    class Graph:
        called = []

        def messages(self, token, channel):
            self.called.append(channel["id"])
            return []

    class Extractor:
        configured = True

    class DB:
        def next_channels(self, *args):
            raise AssertionError("must not poll")

        def cached_messages(self, channel):
            return {}

        def catalog_events(self, channels):
            return list(channels)

    a = {"id": "a", "team_id": "team", "name": "A"}
    b = {"id": "b", "team_id": "team", "name": "B"}
    graph = Graph()
    src = DirectSource(graph, Extractor())
    assert src.collect("token", [a, b], "tenant", DB(), "user", selected_channels=[b]) == ["a", "b"]
    assert graph.called == ["b"]
    with pytest.raises(GraphFailure):
        src.collect(
            "token",
            [a, b],
            "tenant",
            DB(),
            "user",
            selected_channels=[{"id": "b", "team_id": "different"}],
        )
    assert graph.called == ["b"]


def test_selected_channel_timeout_does_not_acknowledge_partial_scan(monkeypatch):
    import web_service.direct_source as module

    times = iter([0, 0, 81])
    monkeypatch.setattr(module, "monotonic", lambda: next(times))

    class Graph:
        def messages(self, token, channel):
            return [{"id": "a"}]

    class Extractor:
        configured = True

    class DB:
        def cached_messages(self, channel):
            return {}

        def catalog_events(self, channels):
            raise AssertionError("must retry, not acknowledge")

    channel = {"id": "a", "team_id": "team"}
    with pytest.raises(GraphFailure):
        DirectSource(Graph(), Extractor()).collect(
            "token", [channel], "tenant", DB(), "user", selected_channels=[channel]
        )
