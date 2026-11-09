"""Static assignments checked by basedpyright, not runtime isinstance checks.

Return types force concrete adapters and doubles to keep the same call shapes.
These functions are never called and do not construct providers or databases.
"""

from support.fakes import Cache, Extractor, Graph, Refresher, SharedCache

from web_service.connections import ConnectionTokens, PersistentIdentity
from web_service.direct_source import AIExtractor, TeamsGraph
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
