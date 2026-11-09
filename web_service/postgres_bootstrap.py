"""Explicit first-time creation of a dedicated PostgreSQL web schema.

The default CLI only prints an offline plan. Applying creates a new namespace
and the bundled draft in one transaction; any existing namespace is rejected,
even if it is empty. This is not a migration or an arbitrary SQL runner.
No existing tables are altered, deleted, reset, or upgraded by this module.
"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from .postgres_connection import PostgresConnectionFactory

_DRAFT = Path(__file__).with_name("postgresql_schema_draft.sql")
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}", re.ASCII)
_OBJECT = r"(?:web|graph)_[a-z0-9_]+"
_TABLE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(" + _OBJECT + r")\s*\((.+)\)\s*\Z", re.ASCII | re.DOTALL
)
_INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+("
    + _OBJECT
    + r")\s+ON\s+("
    + _OBJECT
    + r")\s*\([a-z0-9_,\s]+\)\s*(?:WHERE\s+active\s*=\s*1)?\s*\Z",
    re.ASCII | re.DOTALL,
)
_LITERAL = re.compile(r"'(?:[^']|'')*'", re.DOTALL)
_VERSION = "INSERT INTO web_schema_version(version) VALUES (1) ON CONFLICT DO NOTHING"
_FORBIDDEN = frozenset(
    {
        "ALTER",
        "DROP",
        "TRUNCATE",
        "UPDATE",
        "COPY",
        "DO",
        "SELECT",
        "INSERT",
        "SET",
        "GRANT",
        "REVOKE",
        "EXECUTE",
        "FUNCTION",
        "PROCEDURE",
        "TRIGGER",
        "RULE",
        "VIEW",
        "EXTENSION",
        "INHERITS",
        "TABLESPACE",
        "SCHEMA",
    }
)


class BootstrapRejected(ValueError):
    """A safe, content-free refusal before existing data can be changed."""


def _schema_name(value):
    if (
        not isinstance(value, str)
        or _SCHEMA.fullmatch(value) is None
        or value in {"public", "information_schema"}
        or value.startswith("pg_")
    ):
        raise BootstrapRejected("A dedicated non-system PostgreSQL schema is required")
    return value


def _reviewed_draft():
    """Guard our fixed plain-DDL draft, rather than parse untrusted SQL."""
    payload = _DRAFT.read_bytes()
    source = payload.decode("utf-8")
    source = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
    statements = tuple(part.strip() for part in source.split(";") if part.strip())
    tables, indexes, references = set(), {}, set()
    versions = 0
    for statement in statements:
        normalized = " ".join(statement.split())
        if normalized == _VERSION:
            versions += 1
            continue
        # Closed identifiers, plain literals and this draft's constraint grammar
        # exclude schema qualification, comments, quoted identifiers and blocks.
        # The source file remains code that must be reviewed before deployment.
        plain = _LITERAL.sub("''", statement)
        if re.fullmatch(r"[A-Za-z0-9_(),<>=\s']+", plain, re.ASCII) is None:
            raise BootstrapRejected("The bundled schema draft contains unsupported SQL")
        if plain.count("'") % 2 or _LITERAL.sub("", plain).find("'") >= 0:
            raise BootstrapRejected("The bundled schema draft contains invalid literals")
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", plain, re.ASCII)
        for index, token in enumerate(tokens):
            upper = token.upper()
            if upper in _FORBIDDEN or (upper == "CREATE" and index != 0):
                raise BootstrapRejected("The bundled schema draft is not creation-only")
            if upper == "DELETE" and not (
                index
                and tokens[index - 1].upper() == "ON"
                and index + 1 < len(tokens)
                and tokens[index + 1].upper() == "CASCADE"
            ):
                raise BootstrapRejected("The bundled schema draft is not creation-only")
        table, index = _TABLE.fullmatch(statement), _INDEX.fullmatch(statement)
        if table:
            name = table[1]
            if name in tables or name in indexes:
                raise BootstrapRejected("The bundled schema draft has duplicate objects")
            tables.add(name)
            found = re.findall(r"\bREFERENCES\s+(" + _OBJECT + r")\s*\(", plain, re.ASCII)
            if len(found) != sum(token.upper() == "REFERENCES" for token in tokens):
                raise BootstrapRejected("The bundled schema draft has an unsupported reference")
            references.update(found)
        elif index:
            name, target = index.groups()
            if name in tables or name in indexes:
                raise BootstrapRejected("The bundled schema draft has duplicate objects")
            indexes[name] = target
        else:
            raise BootstrapRejected("The bundled schema draft contains unsupported SQL")
    if (
        not tables
        or "web_schema_version" not in tables
        or versions != 1
        or not references.issubset(tables)
        or not set(indexes.values()).issubset(tables)
    ):
        raise BootstrapRejected("The bundled schema draft is incomplete")
    summary = {
        "draft_file": "web_service/postgresql_schema_draft.sql",
        "draft_sha256": hashlib.sha256(payload).hexdigest(),
        "create_tables": len(tables),
        "create_indexes": len(indexes),
        "statements": len(statements),
    }
    return statements, summary


def bootstrap_plan(schema="calendarsync_web"):
    """Validate the fixed draft and return an offline, credential-free plan."""
    schema = _schema_name(schema)
    _, summary = _reviewed_draft()
    return {
        "schema": schema,
        "operation": "create-new-schema-only",
        "existing_schema_action": "reject",
        "automatic_migration": False,
        "applied": False,
        **summary,
    }


def schema_exists(factory):
    """Read only the target namespace's existence; never provision anything."""
    if not isinstance(factory, PostgresConnectionFactory):
        raise BootstrapRejected("An explicit PostgreSQL connection factory is required")
    schema = _schema_name(factory.schema)
    try:
        with factory() as connection:
            return (
                connection.execute(
                    """SELECT 1 FROM pg_catalog.pg_namespace
                WHERE nspname=%s""",
                    (schema,),
                ).fetchone()
                is not None
            )
    except Exception:
        raise RuntimeError(
            "PostgreSQL schema lookup failed; check the private connection configuration"
        ) from None


def apply_bootstrap(factory, *, expected_sha256=None):
    """Create only a fresh namespace; schema and tables commit or roll back together.

    The explicit factory controls the database/TLS destination and must be
    supplied by the operator. An optional plan hash prevents an accidental
    draft revision from being applied between review and execution.
    """
    if not isinstance(factory, PostgresConnectionFactory):
        raise BootstrapRejected("An explicit PostgreSQL connection factory is required")
    schema = _schema_name(factory.schema)
    statements, summary = _reviewed_draft()
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256, re.ASCII) is None
        or expected_sha256 != summary["draft_sha256"]
    ):
        raise BootstrapRejected("The reviewed schema hash does not match the bundled draft")
    try:
        from psycopg import sql

        with factory() as connection:
            exists = connection.execute(
                """SELECT 1 FROM pg_catalog.pg_namespace
                WHERE nspname=%s""",
                (schema,),
            ).fetchone()
            if exists is not None:
                raise BootstrapRejected("The target schema already exists; no changes were applied")
            # Deliberately omit IF NOT EXISTS. A competing creator must cause
            # a rollback, never redirect this bootstrap into an existing schema.
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            if connection.execute("SELECT current_schema()").fetchone() != (schema,):
                raise BootstrapRejected("The new PostgreSQL schema could not be selected")
            for statement in statements:
                connection.execute(statement)
            if connection.execute("SELECT version FROM web_schema_version").fetchall() != [(1,)]:
                raise BootstrapRejected("The created PostgreSQL schema did not validate")
    except BootstrapRejected:
        raise
    except Exception:
        # This handler is outside the transaction context, so exceptions first
        # trigger rollback. A network loss at commit can have an uncertain
        # outcome; never promise success or blindly retry in that situation.
        raise RuntimeError(
            "PostgreSQL bootstrap failed; verify the dedicated schema before retrying"
        ) from None
    return {
        "schema": schema,
        "operation": "create-new-schema-only",
        "existing_schema_action": "reject",
        "automatic_migration": False,
        "applied": True,
        **summary,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Plan or explicitly create a new PostgreSQL beta schema"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--plan", action="store_true", help="Offline plan only (the default)")
    action.add_argument("--apply", action="store_true", help="Create only a new dedicated schema")
    parser.add_argument("--schema", default=None)
    parser.add_argument("--expected-sha256", default=None)
    args = parser.parse_args()
    try:
        schema = (
            args.schema
            if args.schema is not None
            else os.environ.get("WEB_DATABASE_SCHEMA", "calendarsync_web")
        )
        if args.apply:
            factory = PostgresConnectionFactory(
                os.environ.get("DATABASE_URL", ""),
                schema,
                sslmode=os.environ.get("WEB_DATABASE_SSLMODE") or None,
                sslrootcert=os.environ.get("WEB_DATABASE_SSLROOTCERT") or None,
            )
            result = apply_bootstrap(factory, expected_sha256=args.expected_sha256)
        else:
            result = bootstrap_plan(schema)
    except BootstrapRejected as error:
        parser.exit(1, str(error) + "\n")
    except Exception:
        parser.exit(
            1,
            "Bootstrap failed; check the private connection configuration and dedicated schema.\n",
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
