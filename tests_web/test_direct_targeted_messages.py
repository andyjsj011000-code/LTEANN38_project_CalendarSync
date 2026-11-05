"""Targeted root-message behavior; no external requests are made by these fakes."""
from datetime import datetime, timezone

import pytest

from web_service.direct_source import DirectSource
from web_service.school_graph import GraphFailure


CHANNEL = {'id': 'channel', 'team_id': 'team', 'name': 'Verified channel'}


class TargetGraph:
    def __init__(self, response):
        self.response, self.calls = response, []

    def messages(self, *args):
        raise AssertionError('A targeted update must not scan the recent channel list')

    def message(self, token, channel, message_id, *, complete_replies=False, deadline=None):
        assert complete_replies is True and deadline is not None
        self.calls.append((token, channel['id'], message_id))
        return self.response


class Extractor:
    configured = True

    def __init__(self): self.calls = []

    def extract(self, text, images, previous):
        self.calls.append((text, images, previous))
        return [{'key': 'deadline', 'title': 'Updated deadline', 'notes': '',
                 'start_date': '2026-11-05', 'end_date': '2026-11-05',
                 'time': None, 'kind': 'assignment', 'cancelled': False}]


class Cache:
    def __init__(self, known=None):
        self.known, self.saved = known or {}, []

    def next_channels(self, *args):
        raise AssertionError('A targeted update must not advance the polling cursor')

    def cached_messages(self, channel): return self.known

    def clock(self): return datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp()

    def cache_analysis(self, key, channel, message, version, events):
        self.saved.append((channel, message, events))

    def catalog_events(self, channels):
        return [event for _, _, events in self.saved for event in events]


def test_exact_old_root_is_fetched_without_recent_scan_or_unrelated_cached_reads():
    graph = TargetGraph({'id': 'old-root', 'createdDateTime': '2020-01-01T00:00:00Z',
                         'body': {'content': 'Updated old deadline'}})
    extractor, cache = Extractor(), Cache({'unrelated': {'version': 'v', 'events': []}})
    result = DirectSource(graph, extractor, images=False).collect(
        'delegated', [CHANNEL], 'tenant', cache, 'user',
        selected_channels=[CHANNEL], selected_messages=['old-root', 'old-root'])
    assert graph.calls == [('delegated', 'channel', 'old-root')]
    assert len(extractor.calls) == 1
    assert result[0]['title'] == 'Updated deadline'
    assert cache.saved[0][1] == 'old-root'


@pytest.mark.parametrize('targets', [[], 'root', [None], ['../root'], ['a/b'], ['a%2fb'], ['a\x00b'], ['.'], ['x'] * 21])
def test_invalid_targets_are_rejected_before_any_read(targets):
    graph = TargetGraph(None)
    with pytest.raises(GraphFailure):
        DirectSource(graph, Extractor()).collect('token', [CHANNEL], 'tenant', Cache(), 'user',
            selected_channels=[CHANNEL], selected_messages=targets)
    assert graph.calls == []


def test_targets_require_exactly_one_verified_channel():
    graph = TargetGraph(None)
    source = DirectSource(graph, Extractor())
    with pytest.raises(GraphFailure):
        source.collect('token', [CHANNEL], 'tenant', Cache(), 'user', selected_messages=['root'])
    other = {'id': 'other', 'team_id': 'team'}
    with pytest.raises(GraphFailure):
        source.collect('token', [CHANNEL, other], 'tenant', Cache(), 'user',
            selected_channels=[CHANNEL, other], selected_messages=['root'])
    assert graph.calls == []


def test_verified_missing_root_cancels_known_events_without_extraction():
    graph, extractor = TargetGraph(None), Extractor()
    prior = {'id': 'direct:event', 'channel_id': 'channel', 'title': 'Prior deadline',
             'start_date': '2026-11-05', 'end_date': '2026-11-05', 'cancelled': False}
    cache = Cache({'root': {'version': 'previous', 'events': [prior]}})
    result = DirectSource(graph, extractor).collect('token', [CHANNEL], 'tenant', cache, 'user',
        selected_channels=[CHANNEL], selected_messages=['root'])
    assert result[0]['cancelled'] is True
    assert extractor.calls == []


def test_target_response_must_match_requested_root():
    graph, extractor = TargetGraph({'id': 'another-root'}), Extractor()
    with pytest.raises(GraphFailure):
        DirectSource(graph, extractor).collect('token', [CHANNEL], 'tenant', Cache(), 'user',
            selected_channels=[CHANNEL], selected_messages=['root'])
    assert extractor.calls == []
