"""Execution context for synchronization, with no dependency on the web server.

Legacy stores/providers retain their existing duck-typed injection seams.
This protocol separates ownership/callbacks from the HTTP adapter; individual
typed adapters continue to own the narrower storage and provider contracts.
"""

from typing import Any, Protocol

from .collection_types import DeviceSnapshot, PreparedCollection
from .config import Settings
from .ports import TokenProvider
from .sync_results import BusyResult, IngestionSummary

SyncResult = IngestionSummary | BusyResult | PreparedCollection | None


class SyncContext(Protocol):
    @property
    def settings(self) -> Settings: ...

    @property
    def db(self) -> Any: ...

    @property
    def graph(self) -> Any: ...

    @property
    def source(self) -> Any: ...

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
