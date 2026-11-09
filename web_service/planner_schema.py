"""Explicit, additive schema for the opt-in planner features.

The existing schema and its data are validated, never migrated or overwritten.
Only the three fixed sidecar tables and their fixed indexes may be created.
"""

import re

from .planner_drafts import DRAFT_SCHEMA_SQL
from .planner_trash import TRASH_SCHEMA
from .postgres_connection import PostgresConnectionFactory
from .postgres_ingestion_status import STATUS_SCHEMA_SQL


PLANNER_COLUMNS = {
    "web_task_drafts": "user_id,payload_blob,version,updated_at,expires_at",
    "web_task_trash": "id,user_id,task_id,payload_blob,deleted_at,expires_at",
    "web_ingestion_status": (
        "device_hash,state,requested_at,started_at,finished_at,"
        "last_success_at,attempt_token,attempt_until,report_blob,error_code"
    ),
}
_INDEXES = frozenset({"web_task_drafts_expiry", "web_task_trash_expiry"})
_CREATE = re.compile(
    r"CREATE\s+(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)\s*", re.ASCII | re.I
)


def planner_statements():
    """Return only our reviewed, closed-name CREATE statements."""
    statements = []
    for source in (DRAFT_SCHEMA_SQL, TRASH_SCHEMA, STATUS_SCHEMA_SQL):
        for chunk in (source,) if isinstance(source, str) else source:
            for raw in chunk.split(";"):
                statement = raw.strip()
                if not statement:
                    continue
                match = _CREATE.match(statement)
                if not match or match[2] not in (
                    PLANNER_COLUMNS if match[1].upper() == "TABLE" else _INDEXES
                ):
                    raise ValueError("Planner schema contains an unsupported statement")
                statements.append(statement)
    return tuple(statements)


def validate_planner_schema(db):
    """Read-only validation, also used by the PostgreSQL facade preflight."""
    for table, columns in PLANNER_COLUMNS.items():
        db.execute(f"SELECT {columns} FROM {table} WHERE 1=0")


def enable_planner_schema(factory, tenant_id):
    if (
        not isinstance(factory, PostgresConnectionFactory)
        or factory.schema in {"public", "information_schema"}
        or not isinstance(tenant_id, str)
        or not tenant_id
    ):
        raise ValueError("An explicit dedicated PostgreSQL connection is required")
    statements = planner_statements()
    with factory() as db:
        if db.execute("SELECT current_schema()").fetchone() != (factory.schema,):
            raise ValueError("Planner schema must use its dedicated namespace")
        if db.execute("SELECT version FROM web_schema_version").fetchall() != [(1,)]:
            raise ValueError("Unsupported base web schema")
        db.execute("SELECT pg_advisory_xact_lock(731904227)")
        for table in ("web_users", "web_channels"):
            if db.execute(
                f"SELECT 1 FROM {table} WHERE tenant_id<>%s LIMIT 1", (tenant_id,)
            ).fetchone():
                raise ValueError("Planner schema belongs to another school")
        for statement in statements:
            db.execute(statement)
        validate_planner_schema(db)


def enable_sqlite_planner_schema(db):
    """Install the same fixed sidecars inside the caller's write transaction."""
    if [tuple(row) for row in db.execute("SELECT version FROM web_schema_version")] != [(1,)]:
        raise ValueError("Unsupported base web schema")
    for statement in planner_statements():
        db.execute(statement)
    validate_planner_schema(db)
