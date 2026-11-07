"""Explicit additive upgrade for shared channel ingestion; existing rows are untouched."""
from .postgres_connection import PostgresConnectionFactory


def enable_shared_channel_schema(factory, tenant_id):
    if not isinstance(factory, PostgresConnectionFactory) or not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError('An explicit dedicated PostgreSQL connection is required')
    with factory() as db:
        if db.execute('SELECT version FROM web_schema_version').fetchall() != [(1,)]:
            raise ValueError('Unsupported base web schema')
        # Transaction-scoped lock serializes independent startup processes.
        db.execute("SELECT pg_advisory_xact_lock(731904226)")
        db.execute('''CREATE TABLE IF NOT EXISTS web_channel_ingestion (
            channel_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, team_id TEXT NOT NULL,
            checked_at BIGINT NOT NULL DEFAULT 0, lease_token TEXT,
            lease_until BIGINT NOT NULL DEFAULT 0)''')
        db.execute('''SELECT channel_id,tenant_id,team_id,checked_at,lease_token,lease_until
            FROM web_channel_ingestion WHERE 1=0''')
        row = db.execute('SELECT 1 FROM web_channel_ingestion WHERE tenant_id<>%s LIMIT 1',
                         (tenant_id,)).fetchone()
        if row:
            raise ValueError('Shared ingestion table belongs to another school')
