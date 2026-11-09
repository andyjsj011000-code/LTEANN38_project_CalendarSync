"""Static assignments checked by basedpyright, not runtime isinstance checks.

Return types force concrete adapters and doubles to keep the same call shapes.
These functions are never called and do not construct providers or databases.
"""

from support.fakes import Cache, Extractor, Graph, Refresher, SharedCache

from web_service.beta import BetaDatabase
from web_service.connections import ConnectionTokens, PersistentIdentity
from web_service.direct_source import AIExtractor, DirectSource, TeamsGraph
from web_service.oracle_source import NoSource, OracleSource
from web_service.personal_ports import PersonalTaskStore, PreferenceStore, TaskTrashStore
from web_service.ports import (
    AnalysisStore,
    ConnectionStore,
    EventExtractor,
    MessageGraph,
    SharedAnalysisStore,
    TokenProvider,
    TokenRefresher,
)
from web_service.postgres_database import PostgresDatabase
from web_service.server import ServerIntegration
from web_service.storage import Database
from web_service.sync_ports import (
    CollectionSource,
    EventSource,
    IngestionTrackingStore,
    PermissionGraph,
    SharedCollectionStore,
    SnapshotStore,
    SyncContext,
)
from web_service.test_storage import TestDatabase


def providers(
    graph: TeamsGraph,
    extractor: AIExtractor,
    identity: PersistentIdentity,
    tokens: ConnectionTokens,
) -> tuple[MessageGraph, EventExtractor, TokenRefresher, TokenProvider]:
    return graph, extractor, identity, tokens


def stores(
    sqlite: TestDatabase, postgres: PostgresDatabase
) -> tuple[AnalysisStore, AnalysisStore, SharedAnalysisStore, ConnectionStore, ConnectionStore]:
    return sqlite, postgres, postgres, sqlite, postgres


def doubles(
    graph: Graph, extractor: Extractor, cache: Cache, shared: SharedCache, refresher: Refresher
) -> tuple[MessageGraph, EventExtractor, AnalysisStore, SharedAnalysisStore, TokenRefresher]:
    return graph, extractor, cache, shared, refresher


def personal_stores(
    foundation: Database, beta: BetaDatabase, sqlite: TestDatabase, postgres: PostgresDatabase
) -> tuple[PersonalTaskStore, PersonalTaskStore, PersonalTaskStore, PersonalTaskStore]:
    return foundation, beta, sqlite, postgres


def preference_stores(
    foundation: Database, beta: BetaDatabase, sqlite: TestDatabase, postgres: PostgresDatabase
) -> tuple[PreferenceStore, PreferenceStore, PreferenceStore, PreferenceStore]:
    return foundation, beta, sqlite, postgres


def planner_stores(
    sqlite: TestDatabase, postgres: PostgresDatabase
) -> tuple[TaskTrashStore, TaskTrashStore]:
    return sqlite, postgres


def synchronization(integration: ServerIntegration) -> SyncContext:
    return integration


def sync_providers(
    graph: TeamsGraph,
    direct: DirectSource,
    oracle: OracleSource,
    absent: NoSource,
) -> tuple[PermissionGraph, CollectionSource, EventSource, EventSource]:
    return graph, direct, oracle, absent


def sync_stores(
    sqlite: TestDatabase,
    postgres: PostgresDatabase,
) -> tuple[
    SnapshotStore,
    SnapshotStore,
    IngestionTrackingStore,
    IngestionTrackingStore,
    SharedCollectionStore,
    SharedCollectionStore,
]:
    return sqlite, postgres, sqlite, postgres, sqlite, postgres
