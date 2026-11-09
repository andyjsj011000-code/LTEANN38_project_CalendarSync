"""Pure validation checks; native transaction/lock coverage lives next door."""

from types import SimpleNamespace

import pytest

from web_service.postgres_retention import (
    PostgresRetention,
    decode_cache_payload,
    retired_event_marker,
)
from web_service.postgres_sync import _source_hash


def event():
    return {
        "id": "direct:stable",
        "channel_id": "class",
        "key": "deadline",
        "title": "Private announcement",
        "notes": "Private notes",
        "source_url": "https://teams.microsoft.com/l/message/class/message",
        "source_version": "message-version",
        "start_date": "2026-11-01",
        "end_date": "2026-11-02",
        "kind": "assignment",
        "time": None,
    }


def test_legacy_lists_remain_readable_and_markers_retain_only_identity():
    value = event()
    assert decode_cache_payload([value], "class") == {"events": [value], "retired": []}
    marker = retired_event_marker(value)
    assert set(marker) == {"id", "key", "source_version", "source_hash", "end_date"}
    assert marker["source_hash"] == _source_hash({**value, "cancelled": False})
    assert "Private" not in str(marker) and "https://" not in str(marker)
    assert decode_cache_payload({"events": [], "retired": [marker]}, "class")["retired"] == [marker]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"events": [], "retired": [], "unexpected": "data"},
        {"events": {}, "retired": []},
        {"events": [], "retired": {}},
        [{"channel_id": "other"}],
    ],
)
def test_cache_envelope_and_channel_validation_fail_closed(payload):
    with pytest.raises(ValueError):
        decode_cache_payload(payload, "class")


@pytest.mark.parametrize(
    "change",
    [
        {"title": "must not remain"},
        {"id": ""},
        {"end_date": "2026-13-01"},
        {"key": "invalid key"},
        {"source_hash": "not-a-hash"},
        {"source_version": []},
    ],
)
def test_retired_markers_reject_text_corruption_and_invalid_identity(change):
    marker = {**retired_event_marker(event()), **change}
    with pytest.raises(ValueError):
        decode_cache_payload({"events": [], "retired": [marker]}, "class")


def test_active_and_retired_duplicates_are_rejected():
    marker = retired_event_marker(event())
    for payload in (
        {"events": [event()], "retired": [marker]},
        {"events": [], "retired": [marker, marker]},
    ):
        with pytest.raises(ValueError):
            decode_cache_payload(payload, "class")


def test_retention_is_disabled_without_opening_a_connection():
    def connect():
        pytest.fail("Disabled retention must not access storage")

    repo = SimpleNamespace(connect=connect)
    assert PostgresRetention.prune_expired_team_events(repo) == {
        "cache_rows_scanned": 0,
        "events_deleted": 0,
        "cached_events_retired": 0,
        "events_preserved": 0,
    }


@pytest.mark.parametrize("days,batch", [(-1, 100), (True, 100), (7, 0), (7, 1001), (7, False)])
def test_retention_bounds_are_validated_even_when_disabled(days, batch):
    with pytest.raises(ValueError):
        PostgresRetention.prune_expired_team_events(SimpleNamespace(), days, batch)
