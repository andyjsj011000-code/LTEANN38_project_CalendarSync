"""Build server collaborators before HTTP routes or worker tasks are installed.

Environment readers live in config.py. This module chooses the existing
adapters, preserves explicit injections, and does not initialize a database.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import DirectSourceConfig, OracleConfig, PushConfig, Settings
from .connections import PersistentIdentity
from .direct_source import TeamsGraph, direct_source_from_config
from .oracle_source import source_from_config
from .push import sender_from_config
from .render_guard import ensure_hosted_storage_supported
from .test_storage import TestDatabase


@dataclass(frozen=True)
class ServerComponents:
    settings: Settings
    # Existing entry points accept duck-typed test adapters at these seams.
    # Their operational contracts are checked at each typed service boundary.
    database: Any = field(repr=False)
    identity: Any = field(repr=False)
    graph: Any = field(repr=False)
    source: Any = field(repr=False)
    sender: Any = field(repr=False)


def compose_server(
    settings=None,
    database=None,
    identity=None,
    graph=None,
    source=None,
    sender=None,
    *,
    preview=False,
    notification_runtime=None,
    environ: Mapping[str, str] | None = None,
) -> ServerComponents:
    # One snapshot per composition; an explicit empty mapping is intentional.
    env = dict(os.environ if environ is None else environ)
    if database is None:
        ensure_hosted_storage_supported(env)
    if settings is None:
        settings = Settings.from_server_env(env)
    if not settings.auth_configured:
        raise ValueError("Configure school Entra authentication")
    if not settings.secure_cookies and not settings.allow_local_login:
        raise ValueError("HTTPS required")
    db = database or TestDatabase(settings.database_path, settings.data_key)
    if not isinstance(db, TestDatabase):
        # Preserve the optional import and the explicit PostgreSQL checks.
        from .postgres_database import PostgresDatabase

        if not isinstance(db, PostgresDatabase):
            raise ValueError("Test server requires its separate storage")
        if preview or db.tenant_id != settings.tenant_id:
            raise ValueError(
                "Experimental PostgreSQL storage requires its configured school tenant"
            )
        if notification_runtime is not None:
            raise ValueError("PostgreSQL notification runtime integration is not implemented")
    provider = identity or PersistentIdentity(settings)
    graph = graph or TeamsGraph()
    if source is None:
        mode = env.get("WEB_INGESTION_MODE", "direct")
        if mode not in {"direct", "oracle"}:
            raise ValueError("Invalid ingestion mode")
        source = (
            direct_source_from_config(graph, DirectSourceConfig.from_env(env))
            if mode == "direct"
            else source_from_config(OracleConfig.from_env(env))
        )
    sender = sender if sender is not None else sender_from_config(PushConfig.from_env(env))
    return ServerComponents(settings, db, provider, graph, source, sender)
