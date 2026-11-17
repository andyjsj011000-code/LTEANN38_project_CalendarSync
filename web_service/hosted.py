"""Explicit PostgreSQL beta factory and read-only deployment preflight.

The default server entry point is unchanged. Configuration checks do not connect
or import a PostgreSQL driver. The database check is read-only. Only the separate
app factory's explicit bootstrap flag can create a previously absent schema.
The separate planner opt-in creates fixed additive sidecars in an existing web
schema; neither path migrates or overwrites existing tables or rows.
"""

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .postgres_connection import PostgresConnectionFactory
from .postgres_database import PostgresDatabase


@dataclass(frozen=True)
class HostedConfig:
    settings: Settings
    connection: PostgresConnectionFactory
    ai_key: str = field(repr=False)
    model: str = "gpt-4o-mini"
    images: bool = True
    bootstrap_new_schema: bool = False
    shared_ingestion: bool = False
    planner_enhancements: bool = False
    assignments: bool = False

    def describe(self):
        return {
            "storage_backend": "postgresql-experimental",
            "callback_url": self.settings.callback_url,
            "auth_configured": self.settings.auth_configured,
            "ai_configured": bool(self.ai_key),
            "ingestion_mode": "direct",
            "automatic_schema_changes": self.bootstrap_new_schema,
            "shared_channel_ingestion": self.shared_ingestion,
            "planner_enhancements": self.planner_enhancements,
            "assignments_enabled": self.assignments,
            "connection": self.connection.describe(),
        }


def configuration(environ=None):
    env = os.environ if environ is None else environ
    if env.get("WEB_STORAGE_BACKEND") != "postgresql":
        raise ValueError("Explicit PostgreSQL beta storage selection is required")
    if env.get("WEB_INGESTION_MODE", "direct") != "direct":
        raise ValueError("The separate beta factory supports direct ingestion only")
    settings = Settings(
        database_path=Path("unused-postgres-storage"),
        data_key=env.get("WEB_DATA_KEY", ""),
        origin=env.get("WEB_ORIGIN") or env.get("RENDER_EXTERNAL_URL", ""),
        tenant_id=env.get("WEB_ENTRA_TENANT_ID", "").lower(),
        client_id=env.get("WEB_ENTRA_CLIENT_ID", "").lower(),
        client_secret=env.get("WEB_ENTRA_CLIENT_SECRET", ""),
        session_seconds=28800,
        idle_seconds=1800,
        access_seconds=60,
        classroom_enabled=env.get("WEB_CLASSROOM_ENABLED") == "1",
        google_classroom_client_id=env.get("WEB_GOOGLE_CLASSROOM_CLIENT_ID", ""),
        google_classroom_client_secret=env.get("WEB_GOOGLE_CLASSROOM_CLIENT_SECRET", ""),
    )
    if not settings.auth_configured or not settings.secure_cookies:
        raise ValueError("HTTPS and school Entra authentication are required for the beta factory")
    schema = env.get("WEB_DATABASE_SCHEMA", "calendarsync_web")
    if schema in {"public", "information_schema"}:
        raise ValueError("Use a dedicated web schema instead of a shared or system schema")
    factory = PostgresConnectionFactory(
        env.get("DATABASE_URL", ""),
        schema,
        sslmode=env.get("WEB_DATABASE_SSLMODE") or None,
        sslrootcert=env.get("WEB_DATABASE_SSLROOTCERT") or None,
    )
    ai_key = env.get("WEB_OPENAI_API_KEY", "")
    if not ai_key:
        raise ValueError("A separate beta AI credential is required for direct ingestion")
    if env.get("WEB_AI_IMAGES_ENABLED", "1") not in {"0", "1"}:
        raise ValueError("Invalid beta image ingestion setting")
    if env.get("WEB_BOOTSTRAP_NEW_SCHEMA", "0") not in {"0", "1"}:
        raise ValueError("Invalid beta schema bootstrap setting")
    if env.get("WEB_SHARED_CHANNEL_INGESTION", "0") not in {"0", "1"}:
        raise ValueError("Invalid shared channel ingestion setting")
    if env.get("WEB_PLANNER_ENHANCEMENTS", "0") not in {"0", "1"}:
        raise ValueError("Invalid planner enhancement setting")
    if env.get("WEB_ASSIGNMENTS_ENABLED", "0") not in {"0", "1"}:
        raise ValueError("Invalid assignment ingestion setting")
    return HostedConfig(
        settings,
        factory,
        ai_key,
        env.get("WEB_LLM_MODEL", "gpt-4o-mini"),
        env.get("WEB_AI_IMAGES_ENABLED", "1") == "1",
        env.get("WEB_BOOTSTRAP_NEW_SCHEMA", "0") == "1",
        env.get("WEB_SHARED_CHANNEL_INGESTION", "0") == "1",
        env.get("WEB_PLANNER_ENHANCEMENTS", "0") == "1",
        env.get("WEB_ASSIGNMENTS_ENABLED", "0") == "1",
    )


def create_hosted_app(
    environ=None, *, identity=None, graph=None, source=None, sender=None, worker=True
):
    config = configuration(environ)
    if config.bootstrap_new_schema:
        from .postgres_bootstrap import apply_bootstrap, schema_exists

        # Explicit opt-in for the confirmed new beta DB. Existing namespaces
        # are validated below and never migrated or overwritten here.
        if not schema_exists(config.connection):
            apply_bootstrap(config.connection)
    from .direct_source import AIExtractor, DirectSource, TeamsGraph
    from .server import create_test_server

    graph = graph if graph is not None else TeamsGraph()
    source = (
        source
        if source is not None
        else DirectSource(graph, AIExtractor(config.ai_key, config.model), images=config.images)
    )
    if config.shared_ingestion:
        # Explicit opt-in installs only the reviewed additive lease table.
        from .postgres_shared_schema import enable_shared_channel_schema

        enable_shared_channel_schema(config.connection, config.settings.tenant_id)
    if config.planner_enhancements:
        from .planner_schema import enable_planner_schema

        enable_planner_schema(config.connection, config.settings.tenant_id)
    database = PostgresDatabase(
        config.connection,
        config.settings.data_key,
        config.settings.tenant_id,
        shared_ingestion_enabled=config.shared_ingestion,
        planner_enhancements_enabled=config.planner_enhancements,
    )
    options = {"assignments_enabled": True} if config.assignments else {}
    return create_test_server(
        config.settings, database, identity, graph, source, sender, worker=worker, **options
    )


def main():
    parser = argparse.ArgumentParser(description="Read-only PostgreSQL beta preflight")
    checks = parser.add_mutually_exclusive_group(required=True)
    checks.add_argument("--check-config", action="store_true")
    checks.add_argument("--check-database", action="store_true")
    args = parser.parse_args()
    try:
        config = configuration()
        if args.check_database:
            PostgresDatabase(
                config.connection,
                config.settings.data_key,
                config.settings.tenant_id,
                shared_ingestion_enabled=config.shared_ingestion,
                planner_enhancements_enabled=config.planner_enhancements,
            ).initialize()
    except Exception:
        # Do not echo input values, driver diagnostics or secrets on a console.
        parser.exit(
            1,
            "Beta preflight failed; check private environment and reviewed schema configuration.\n",
        )
    print(
        json.dumps({**config.describe(), "database_checked": args.check_database}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
