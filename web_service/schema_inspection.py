"""Compare managed table structure with pinned definitions, without row rewrites.

This checks columns/types/nullability, primary/unique keys, foreign keys and
named indexes. It is not a proof of historical execution or CHECK/default
expression equivalence; baseline entries are explicitly observations.
"""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from .schema_catalog import HISTORY_SQL, REVISIONS, HistoryRejected


@dataclass(frozen=True)
class TableShape:
    # Columns are (name, declared type, not-null).
    columns: tuple[tuple[str, str, bool], ...]
    primary: tuple[str, ...]
    foreign: frozenset[tuple[str, str, str, str, str]]
    unique: frozenset[tuple[str, ...]]
    indexes: dict[str, tuple[bool, tuple[str, ...], str]]


def query(db, backend, statement, parameters=()):
    return db.execute(
        statement.replace("%s", "?") if backend == "sqlite" else statement, parameters
    )


def table_names(db, backend) -> set[str]:
    statement = (
        "SELECT name FROM sqlite_master WHERE type='table'"
        if backend == "sqlite"
        else """SELECT c.relname FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')"""
    )
    return {row[0] for row in db.execute(statement).fetchall()}


def _predicate(value: str | None) -> str:
    return re.sub(r"[\s()]", "", value or "").lower()


def sqlite_shape(db, table: str) -> TableShape:
    def pragma(operation, name):
        # Identifiers may include operator-created indexes; always quote them.
        quoted = name.replace('"', '""')
        return db.execute(f'PRAGMA {operation}("{quoted}")').fetchall()

    columns = pragma("table_info", table)
    primary = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
    foreign = frozenset(
        (row[3], row[2], row[4], row[5], row[6]) for row in pragma("foreign_key_list", table)
    )
    unique, indexes = set(), {}
    for _, name, is_unique, origin, _ in pragma("index_list", table):
        fields = tuple(row[2] for row in pragma("index_info", name))
        if origin == "u":
            unique.add(fields)
        elif origin == "c":
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            predicate = re.split(r"\bWHERE\b", sql, flags=re.I)[1:]
            indexes[name] = (bool(is_unique), fields, _predicate(predicate[0] if predicate else ""))
    return TableShape(
        tuple((row[1], row[2].upper(), bool(row[3])) for row in columns),
        primary,
        foreign,
        frozenset(unique),
        indexes,
    )


def postgres_shape(db, table: str) -> TableShape:
    columns: list[tuple[str, str, str]] = db.execute(
        """SELECT column_name,udt_name,is_nullable FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    rows = db.execute(
        """SELECT ci.relname,i.indisunique,i.indisprimary,pg_get_expr(i.indpred,i.indrelid),
            array_agg(a.attname::text ORDER BY k.ord),COALESCE(c.contype::text,'')
        FROM pg_catalog.pg_index i
        JOIN pg_catalog.pg_class ci ON ci.oid=i.indexrelid
        CROSS JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum,ord)
        JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
        LEFT JOIN pg_catalog.pg_constraint c ON c.conindid=i.indexrelid AND c.contype IN ('p','u')
        WHERE i.indrelid=%s::regclass
        GROUP BY ci.relname,i.indisunique,i.indisprimary,
            pg_get_expr(i.indpred,i.indrelid),c.contype""",
        (table,),
    ).fetchall()
    primary, unique, indexes = (), set(), {}
    for name, is_unique, is_primary, predicate, fields, constraint in rows:
        if is_primary:
            primary = tuple(fields)
        elif constraint == "u":
            unique.add(tuple(fields))
        else:
            indexes[name] = (bool(is_unique), tuple(fields), _predicate(predicate))
    rows = db.execute(
        """SELECT a.attname,t.relname,ta.attname,c.confupdtype,c.confdeltype,n.nspname
        FROM pg_catalog.pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey,c.confkey) AS k(local_col,target_col)
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.local_col
        JOIN pg_catalog.pg_class t ON t.oid=c.confrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_catalog.pg_attribute ta ON ta.attrelid=c.confrelid AND ta.attnum=k.target_col
        WHERE c.conrelid=%s::regclass AND c.contype='f'""",
        (table,),
    ).fetchall()
    namespace = db.execute("SELECT current_schema()").fetchone()[0]
    if any(row[5] != namespace for row in rows):
        raise HistoryRejected("Managed foreign keys must remain in the dedicated schema")
    actions = {
        "a": "NO ACTION",
        "c": "CASCADE",
        "r": "RESTRICT",
        "n": "SET NULL",
        "d": "SET DEFAULT",
    }
    types = {"int8": "BIGINT", "int4": "INTEGER", "text": "TEXT", "bytea": "BYTEA"}
    return TableShape(
        tuple((name, types.get(kind, kind), nullable == "NO") for name, kind, nullable in columns),
        primary,
        frozenset((a, t, ta, actions[u], actions[d]) for a, t, ta, u, d, _ in rows),
        frozenset(unique),
        indexes,
    )


def validate_structure(db, backend, revisions: tuple[str, ...], *, history: bool) -> None:
    # Both pinned base grammars use DDL understood by SQLite. The reference is
    # memory-only, including when inspecting PostgreSQL; no target DDL runs.
    with closing(sqlite3.connect(":memory:")) as reference:
        for id in revisions:
            for statement in REVISIONS[id].statements():
                reference.execute(statement)
        if history:
            reference.execute(HISTORY_SQL)
        required = table_names(reference, "sqlite")
        if not required <= table_names(db, backend):
            raise HistoryRejected("The selected schema profile is incomplete")
        for table in sorted(required):
            expected = sqlite_shape(reference, table)
            actual = sqlite_shape(db, table) if backend == "sqlite" else postgres_shape(db, table)
            columns = expected.columns
            if backend == "postgres":
                columns = tuple(
                    (name, kind, not_null or name in expected.primary)
                    for name, kind, not_null in columns
                )
            if (
                columns != actual.columns
                or expected.primary != actual.primary
                or expected.foreign != actual.foreign
                or expected.unique != actual.unique
                or any(
                    actual.indexes.get(name) != value for name, value in expected.indexes.items()
                )
            ):
                # The table name comes from the bundled catalog, not target row values.
                raise HistoryRejected(
                    f"Managed table differs from its published structure: {table}"
                )
