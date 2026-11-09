"""In-memory collection doubles with explicit inputs, failures and observations.

The collector visits one channel at a time. These doubles model that contract,
not database transactions or concurrent row locks; native PG tests cover those.
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from web_service.collection_types import (
    CachedAnalysis,
    Channel,
    EventDraft,
    RetiredEvent,
    SourceEvent,
)


def channels(count: int) -> list[Channel]:
    return [
        {"id": f"channel-{number:02}", "team_id": "team", "name": "Class"}
        for number in range(count)
    ]


def message(identity: str, text: str | None = None) -> dict[str, Any]:
    return {"id": identity, "body": {"content": text or identity}, "replies": []}


def event(title: str) -> EventDraft:
    return {
        "key": "deadline",
        "title": title,
        "notes": "",
        "start_date": "2026-11-07",
        "end_date": "2026-11-07",
        "time": None,
        "kind": "assignment",
        "cancelled": False,
    }


class Cache:
    def __init__(self) -> None:
        self.cursor = 0
        self.cursors: dict[str, int] = {}
        self.visits: list[str] = []
        self.values: dict[str, dict[str, CachedAnalysis]] = {}
        self.writes: list[tuple[str, str]] = []
        self.now = datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp()

    def next_channels(self, user: str, candidates: list[Channel], count: int) -> list[Channel]:
        assert count == 1, "Do not reserve channels that may never be visited"
        if not candidates:
            return []
        index = self.cursors.get(user, 0)
        selected = sorted(candidates, key=lambda row: row["id"])[index % len(candidates)]
        self.cursor = (index + 1) % len(candidates)
        self.cursors[user] = self.cursor
        self.visits.append(selected["id"])
        return [selected]

    def cached_messages(self, channel: str) -> dict[str, CachedAnalysis]:
        return dict(self.values.get(channel, {}))

    def cache_analysis(
        self, key: str, channel: str, identity: str, version: str, events: list[SourceEvent]
    ) -> bool | None:
        self.writes.append((channel, identity))
        self.values.setdefault(channel, {})[identity] = {"version": version, "events": events}
        return None

    def catalog_events(self, candidates: list[str]) -> list[SourceEvent]:
        return [
            item
            for channel in candidates
            for cached in self.values.get(channel, {}).values()
            for item in cached["events"]
        ]

    def touch_analysis(self, key: str) -> bool | None:
        return None

    def clock(self) -> float:
        return self.now


class SharedCache(Cache):
    shared_ingestion_enabled = True

    def __init__(self) -> None:
        super().__init__()
        self.leases: dict[str, str] = {}
        self.lease_until: dict[str, float] = {}
        self.fresh_until: dict[str, float] = {}
        self.claims: list[tuple[str, int]] = []
        self.finishes: list[tuple[str, str | None, bool]] = []
        self.sequence = 0

    def _active(self, channel: str, lease: str | None) -> bool:
        return bool(
            lease is not None
            and self.leases.get(channel) == lease
            and self.lease_until.get(channel, 0) > self.clock()
        )

    def channel_ingestion_fresh(self, channel: Channel, freshness_seconds: int = 300) -> bool:
        assert isinstance(channel, dict) and channel.get("team_id")
        return self.fresh_until.get(channel["id"], 0) > self.clock() and freshness_seconds > 0

    def claim_channel_ingestion(self, channel: Channel, freshness_seconds: int = 300) -> str | None:
        assert isinstance(channel, dict) and channel.get("team_id")
        identity = channel["id"]
        self.claims.append((identity, freshness_seconds))
        if identity in self.leases and self.lease_until.get(identity, float("inf")) <= self.clock():
            del self.leases[identity]
        if identity in self.leases or self.channel_ingestion_fresh(channel, freshness_seconds):
            return None
        self.sequence += 1
        lease = "lease-" + str(self.sequence)
        self.leases[identity] = lease
        self.lease_until[identity] = self.clock() + 180
        return lease

    def finish_channel_ingestion(self, channel: Channel, lease: str | None, success: bool) -> bool:
        assert isinstance(channel, dict) and channel.get("team_id")
        identity = channel["id"]
        self.finishes.append((identity, lease, success))
        if not self._active(identity, lease):
            return False
        del self.leases[identity]
        if success:
            self.fresh_until[identity] = self.clock() + 300
        return True

    def cache_analysis(
        self,
        key: str,
        channel: str,
        identity: str,
        version: str,
        events: list[SourceEvent],
        *,
        ingestion_lease: str | None = None,
    ) -> bool | None:
        if not self._active(channel, ingestion_lease):
            return False
        super().cache_analysis(key, channel, identity, version, events)
        return True

    def touch_analysis(self, key: str, *, ingestion_lease: str | None = None) -> bool | None:
        return any(self._active(channel, ingestion_lease) for channel in self.leases)


class Graph:
    def __init__(
        self,
        values: dict[str, list[dict[str, Any]]] | None = None,
        on_read: Callable[[], None] | None = None,
    ) -> None:
        self.values, self.on_read = values or {}, on_read
        self.reads: list[str] = []
        self.images: dict[str, str | None] = {}
        self.failure: Exception | None = None

    def messages(self, token: str, channel: Channel) -> list[dict[str, Any]]:
        self.reads.append(channel["id"])
        if self.on_read:
            self.on_read()
        if self.failure:
            raise self.failure
        return list(self.values.get(channel["id"], []))[:50]

    def message(
        self,
        token: str,
        channel: Channel,
        message_id: str,
        *,
        complete_replies: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        if self.failure:
            raise self.failure
        return next((m for m in self.values.get(channel["id"], []) if m["id"] == message_id), None)

    def hosted_image(self, token: str, channel: Channel, message_id: str, url: str) -> str | None:
        if self.failure:
            raise self.failure
        return self.images.get(url)


class Extractor:
    configured = True

    def __init__(self, fail: Exception | None = None, *, drafts: list[EventDraft] | None = None):
        self.fail, self.drafts = fail, drafts
        self.calls: list[tuple[str, Sequence[SourceEvent | RetiredEvent]]] = []

    def extract(
        self, text: str, images: list[str], previous: Sequence[SourceEvent | RetiredEvent]
    ) -> list[EventDraft]:
        self.calls.append((text, previous))
        if self.fail and "bad-message" in text:
            raise self.fail
        return list(self.drafts) if self.drafts is not None else [event(text.splitlines()[-1])]


class Refresher:
    """Script a refresh grant or provider failure without MSAL or network I/O."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, str, str, bool]] = []

    def refresh(
        self, serialized: str, oid: str, tenant: str, *, assignments: bool = False
    ) -> tuple[str, int, str]:
        self.calls.append((serialized, oid, tenant, assignments))
        if self.failure:
            raise self.failure
        return "synthetic-fresh-token", 3600, "synthetic-fresh-cache"
