"""Execution context for synchronization, with no dependency on the web server.

Stores and providers keep structural injection without inheriting these ports.
This protocol separates ownership/callbacks from the HTTP adapter; individual
typed adapters continue to own the narrower storage and provider contracts.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Protocol, TypedDict, TypeGuard

from .collection_types import (
    Channel,
    CollectionReport,
    DeviceSnapshot,
    PreparedCollection,
    SourceEvent,
)
from .config import Settings
from .ports import AnalysisStore, ConnectionStore, TokenProvider
from .sync_results import BusyResult, IngestionSummary

SyncResult = IngestionSummary | BusyResult | PreparedCollection | None


# SQLite uses an expiry integer; PostgreSQL uses an opaque fencing token.
SyncLease = int | str


class PermissionGraph(Protocol):
    def channels(self, token: str, object_id: str, tenant_id: str) -> list[Channel]: ...


class EventSource(Protocol):
    @property
    def configured(self) -> bool: ...

    def events(self, channels: list[Channel], tenant_id: str) -> list[SourceEvent]: ...


class CollectionSource(Protocol):
    @property
    def configured(self) -> bool: ...

    def collect(
        self,
        token: str,
        channels: list[Channel],
        tenant: str,
        db: AnalysisStore,
        user_id: str,
        *,
        on_progress: Callable[[CollectionReport], None] | None = None,
    ) -> list[SourceEvent]: ...


SyncSource = EventSource | CollectionSource


def is_collection_source(source: SyncSource) -> TypeGuard[CollectionSource]:
    """Keep the existing capability test, without runtime Protocol inspection."""
    return hasattr(source, "collect")


class IngestionCommit(TypedDict):
    attempt: str | None
    report: Mapping[str, object]
    reset_channels: bool


class SnapshotOptions(TypedDict, total=False):
    ingestion: IngestionCommit


class SnapshotStore(ConnectionStore, AnalysisStore, Protocol):
    def begin_sync(self, user: str, ttl: int, /, *, preserve_access: bool = False) -> SyncLease: ...

    def fail_sync(self, user: str, state: str, lease: SyncLease, /) -> bool | None: ...

    def commit_snapshot(
        self,
        device: DeviceSnapshot,
        channels: list[Channel],
        events: list[SourceEvent],
        ttl: int,
        lease: SyncLease,
        *,
        ingestion: IngestionCommit | None = None,
    ) -> bool | None: ...

    def mark_ingested(self, device_hash: str, /) -> bool | None: ...

    def known_oracle_events(
        self, user: str, tenant: str, channels: list[Channel], /
    ) -> Iterable[SourceEvent]: ...

    def audit(self, event: str, principal: str, /) -> None: ...


class IngestionTrackingStore(Protocol):
    """Called only when the existing planner flag enables tracking."""

    def claim_ingestion(self, device: DeviceSnapshot) -> str | None: ...

    def update_ingestion(
        self, device: DeviceSnapshot, attempt: str | None, report: Mapping[str, object]
    ) -> bool: ...

    def end_ingestion(
        self,
        device: DeviceSnapshot,
        attempt: str,
        *,
        error_code: str | None = None,
        queued: bool = False,
    ) -> bool: ...


class SharedCollectionStore(Protocol):
    """The shared path remains behind the existing shared-ingestion flag."""

    def sync_status(self, user: str, /) -> Mapping[str, object]: ...

    def materialize_cached_events(self, device: DeviceSnapshot) -> int | None: ...

    def finish_shared_collection(
        self,
        device: DeviceSnapshot,
        report: Mapping[str, object],
        *,
        attempt: str | None = None,
    ) -> bool: ...


class SyncContext(Protocol):
    @property
    def settings(self) -> Settings: ...

    @property
    def db(self) -> SnapshotStore: ...

    @property
    def graph(self) -> PermissionGraph: ...

    @property
    def source(self) -> SyncSource: ...

    @property
    def tokens(self) -> TokenProvider: ...

    @property
    def shared_ingestion_enabled(self) -> bool: ...

    @property
    def _collection_proofs(self) -> dict[str, PreparedCollection]: ...

    def collection_proof(self, device: DeviceSnapshot) -> PreparedCollection | None: ...

    def run_sync(
        self,
        device: DeviceSnapshot,
        ingest: bool = False,
        force: bool = False,
        *,
        prepare_collection: bool = False,
        prepared: PreparedCollection | None = None,
    ) -> SyncResult: ...
