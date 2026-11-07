"""Broad ingestion fairness and truthful progress, with no external service calls."""
from datetime import datetime, timezone
import json

import httpx
import pytest

import web_service.direct_source as module
from web_service.direct_source import CollectionResult, DirectSource
from web_service.school_graph import GraphFailure


def channels(count):
    return [{'id': f'channel-{number:02}', 'team_id': 'team', 'name': 'Class'}
            for number in range(count)]


def message(identity, text=None):
    return {'id': identity, 'body': {'content': text or identity}, 'replies': []}


def event(title):
    return {'key': 'deadline', 'title': title, 'notes': '',
            'start_date': '2026-11-07', 'end_date': '2026-11-07',
            'time': None, 'kind': 'assignment', 'cancelled': False}


class Cache:
    def __init__(self):
        self.cursor, self.visits, self.values, self.writes = 0, [], {}, []

    def next_channels(self, user, candidates, count):
        assert count == 1, 'Do not reserve channels that may never be visited'
        selected = sorted(candidates, key=lambda row: row['id'])[self.cursor % len(candidates)]
        self.cursor = (self.cursor + 1) % len(candidates)
        self.visits.append(selected['id'])
        return [selected]

    def cached_messages(self, channel):
        return dict(self.values.get(channel, {}))

    def cache_analysis(self, key, channel, identity, version, events):
        self.writes.append((channel, identity))
        self.values.setdefault(channel, {})[identity] = {'version': version, 'events': events}

    def catalog_events(self, candidates):
        return [item for channel in candidates
                for cached in self.values.get(channel, {}).values()
                for item in cached['events']]

    def touch_analysis(self, key):
        pass

    def clock(self):
        return datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp()


class Graph:
    def __init__(self, values=None, on_read=None):
        self.values, self.on_read, self.reads = values or {}, on_read, []

    def messages(self, token, channel):
        self.reads.append(channel['id'])
        if self.on_read:
            self.on_read()
        return list(self.values.get(channel['id'], []))


class Extractor:
    configured = True

    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def extract(self, text, images, previous):
        self.calls.append((text, previous))
        if self.fail and 'bad-message' in text:
            raise self.fail
        return [event(text.splitlines()[-1])]


def test_every_verified_channel_can_be_visited_when_the_budget_allows(monkeypatch):
    monkeypatch.setattr(module, 'monotonic', lambda: 0)
    candidates, cache, graph = channels(12), Cache(), Graph()
    result = DirectSource(graph, Extractor(), images=False).collect(
        'token', candidates, 'tenant', cache, 'user')
    assert graph.reads == cache.visits == [row['id'] for row in candidates]
    assert isinstance(result, CollectionResult) and isinstance(result, list)
    assert json.dumps(result) == '[]'
    assert result.report == {'channels_total': 12, 'channels_scanned': 12,
                             'messages_seen': 0, 'messages_analyzed': 0,
                             'messages_cached': 0, 'messages_failed': 0, 'partial': False}


def test_slow_channel_does_not_reserve_or_starve_four_unvisited_channels(monkeypatch):
    now = [0]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    def slow_read():
        now[0] += 81
    cache, graph, candidates = Cache(), Graph(on_read=slow_read), channels(10)
    source = DirectSource(graph, Extractor(), images=False)
    for number in range(4):
        result = source.collect('token', candidates, 'tenant', cache, 'user')
        assert result.report['partial'] is True
        assert result.report['channels_scanned'] == 1
        assert len(cache.visits) == number + 1
    assert graph.reads == cache.visits == [row['id'] for row in candidates[:4]]


def test_deadline_stops_before_reserving_a_channel(monkeypatch):
    times = iter([0, 81])
    monkeypatch.setattr(module, 'monotonic', lambda: next(times))
    cache, graph = Cache(), Graph()
    result = DirectSource(graph, Extractor(), images=False).collect(
        'token', channels(2), 'tenant', cache, 'user')
    assert cache.visits == graph.reads == []
    assert result.report['channels_scanned'] == 0 and result.report['partial'] is True


@pytest.mark.parametrize('error', [ValueError('invalid response'), httpx.ReadTimeout('timeout')])
def test_one_failed_analysis_keeps_prior_events_and_continues_other_messages(monkeypatch, error):
    monkeypatch.setattr(module, 'monotonic', lambda: 0)
    candidates, cache = channels(2), Cache()
    old = {'version': 'old-version', 'events': [{'id': 'old-event', 'channel_id': candidates[0]['id'],
                                              'title': 'Prior deadline', 'cancelled': False}]}
    cache.values[candidates[0]['id']] = {'bad': old}
    graph = Graph({candidates[0]['id']: [message('bad', 'bad-message'), message('good')],
                   candidates[1]['id']: [message('other-channel')]})
    result = DirectSource(graph, Extractor(error), images=False).collect(
        'token', candidates, 'tenant', cache, 'user')
    assert cache.values[candidates[0]['id']]['bad'] is old
    assert (candidates[0]['id'], 'bad') not in cache.writes
    assert len(result) == 3 and result[0]['title'] == 'Prior deadline'
    assert result[0]['cancelled'] is False
    assert result.report == {'channels_total': 2, 'channels_scanned': 2,
                             'messages_seen': 3, 'messages_analyzed': 2,
                             'messages_cached': 0, 'messages_failed': 1, 'partial': True}


def test_unchanged_cached_message_is_reported_without_reanalysis(monkeypatch):
    monkeypatch.setattr(module, 'monotonic', lambda: 0)
    candidates, cache, extractor = channels(1), Cache(), Extractor()
    source = DirectSource(Graph({candidates[0]['id']: [message('unchanged')]}), extractor, images=False)
    first = source.collect('token', candidates, 'tenant', cache, 'user')
    second = source.collect('token', candidates, 'tenant', cache, 'user')
    assert second == first and len(extractor.calls) == 1
    assert second.report['messages_seen'] == second.report['messages_cached'] == 1
    assert second.report['messages_analyzed'] == 0 and second.report['partial'] is False


@pytest.mark.parametrize('state', ['reauth_required', 'account_not_allowed', 'verification_failed'])
def test_access_failures_propagate_instead_of_becoming_partial_success(monkeypatch, state):
    monkeypatch.setattr(module, 'monotonic', lambda: 0)
    candidates, cache = channels(2), Cache()
    graph = Graph({candidates[0]['id']: [message('bad', 'bad-message')]})
    with pytest.raises(GraphFailure) as raised:
        DirectSource(graph, Extractor(GraphFailure(state)), images=False).collect(
            'token', candidates, 'tenant', cache, 'user')
    assert raised.value.state == state
    assert graph.reads == [candidates[0]['id']] and cache.writes == []


def test_selected_channel_analysis_failure_remains_strict(monkeypatch):
    monkeypatch.setattr(module, 'monotonic', lambda: 0)
    candidates, cache = channels(1), Cache()
    graph = Graph({candidates[0]['id']: [message('bad', 'bad-message')]})
    with pytest.raises(ValueError):
        DirectSource(graph, Extractor(ValueError('invalid response')), images=False).collect(
            'token', candidates, 'tenant', cache, 'user', selected_channels=candidates)
    assert cache.visits == cache.writes == []


def test_incomplete_message_scan_is_counted_as_partial_channel(monkeypatch):
    now = [0]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    class SlowExtractor(Extractor):
        def extract(self, text, images, previous):
            now[0] += 81
            return super().extract(text, images, previous)
    candidates, cache = channels(2), Cache()
    graph = Graph({candidates[0]['id']: [message('first'), message('second')]})
    result = DirectSource(graph, SlowExtractor(), images=False).collect(
        'token', candidates, 'tenant', cache, 'user')
    assert graph.reads == [candidates[0]['id']]
    assert result.report['messages_analyzed'] == result.report['messages_seen'] == 1
    assert result.report['channels_scanned'] == 0 and result.report['partial'] is True
    assert cache.writes == [(candidates[0]['id'], 'first')]
