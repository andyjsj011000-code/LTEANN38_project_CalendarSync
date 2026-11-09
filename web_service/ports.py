"""Structural interfaces for collection and credential adapters.

Implementations and test doubles do not inherit from these protocols. Optional
shared-ingestion capabilities are separate from the base cache contract; the
existing runtime feature checks still decide which operations may run.
"""

from collections.abc import Sequence
from typing import Any, Protocol

from .collection_types import (
    CachedAnalysis,
    Channel,
    CredentialPayload,
    DeviceSnapshot,
    EventDraft,
    RetiredEvent,
    SourceEvent,
)


class MessageGraph(Protocol):
    """Raw Graph dictionaries stay untrusted until the collector checks them."""

    def messages(self, token: str, channel: Channel) -> list[dict[str, Any]]: ...

    def message(
        self,
        token: str,
        channel: Channel,
        message_id: str,
        *,
        complete_replies: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any] | None: ...

    def hosted_image(
        self, token: str, channel: Channel, message_id: str, url: str
    ) -> str | None: ...


class EventExtractor(Protocol):
    @property
    def configured(self) -> bool: ...

    def extract(
        self, text: str, images: list[str], previous: Sequence[SourceEvent | RetiredEvent]
    ) -> list[EventDraft]: ...


class AnalysisStore(Protocol):
    """Legacy cache operations; every channel is verified by the caller."""

    def clock(self) -> float: ...

    def next_channels(self, user: str, channels: list[Channel], count: int, /) -> list[Channel]: ...

    def cached_messages(self, channel: str) -> dict[str, CachedAnalysis]: ...

    def cache_analysis(
        self, key: str, channel: str, message: str, version: str, events: list[SourceEvent], /
    ) -> bool | None: ...

    def touch_analysis(self, key: str) -> bool | None: ...

    def catalog_events(self, channels: list[str], /) -> list[SourceEvent]: ...


class SharedAnalysisStore(AnalysisStore, Protocol):
    """Additional fenced operations, only when shared ingestion is enabled."""

    def claim_channel_ingestion(
        self, channel: Channel, freshness_seconds: int = 300
    ) -> str | None: ...

    def channel_ingestion_fresh(self, channel: Channel, freshness_seconds: int = 300) -> bool: ...

    def finish_channel_ingestion(
        self, channel: Channel, lease: str | None, success: bool
    ) -> bool: ...

    def cache_analysis(
        self,
        key: str,
        channel: str,
        message: str,
        version: str,
        events: list[SourceEvent],
        /,
        *,
        ingestion_lease: str | None = None,
    ) -> bool | None: ...

    def touch_analysis(self, key: str, *, ingestion_lease: str | None = None) -> bool | None: ...


class TokenProvider(Protocol):
    def acquire(self, device: DeviceSnapshot) -> str: ...


class TokenRefresher(Protocol):
    def refresh(
        self, serialized: str, oid: str, tenant: str, *, assignments: bool = False
    ) -> tuple[str, int, str]: ...


class ConnectionStore(Protocol):
    """Only the persistence operations needed to acquire a current token."""

    def clock(self) -> float: ...

    def device(self, device_hash: str) -> DeviceSnapshot | None: ...

    def unseal(self, value: str, /) -> CredentialPayload: ...

    def lock_refresh(self, device_hash: str) -> str | None: ...

    def save_refresh(
        self, device_hash: str, lease: str, token: str, lifetime: int, cache: str
    ) -> bool: ...

    def fail_connection(self, device_hash: str, lease: str | None = None) -> None: ...
