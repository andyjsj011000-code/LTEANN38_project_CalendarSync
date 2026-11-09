"""Explicit schema history and additive upgrades, independent of app startup.

Existing schemas are adopted as observations, never claimed as past executions.
Business tables/version rows are not rewritten. DDL and history commit together.
"""

import re
import sqlite3
import time
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from .postgres_connection import PostgresConnectionFactory
from .schema_catalog import (
    FEATURE_TABLES,
    FEATURES,
    HISTORY_SQL,
    PROFILES,
    REVISIONS,
    HistoryRejected,
    schema_plan,
)
from .schema_inspection import query, table_names, validate_structure


class _History:
    backend: str

    def __init__(self, profile: str, clock=time.time):
        schema_plan(profile)
        self.profile, self.clock = profile, clock

    def _connection(self, write: bool) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def plan(self, feature: str | None = None) -> dict:
        return schema_plan(self.profile, feature)

    def _inspect(self, db) -> dict:
        names = table_names(db, self.backend)
        if "web_schema_version" not in names or [
            tuple(row) for row in db.execute("SELECT version FROM web_schema_version").fetchall()
        ] != [(1,)]:
            raise HistoryRejected("Unsupported or missing base schema version")
        if self.backend == "sqlite":
            markers = {
                "beta_environment": {"sqlite-beta"},
                "web_school_tokens": {"sqlite-school", "sqlite-server"},
                "web_connections": {"sqlite-server"},
            }
            if any(
                table in names and self.profile not in profiles
                for table, profiles in markers.items()
            ):
                raise HistoryRejected("The selected SQLite profile does not match this database")
            if self.profile == "sqlite-beta" and (
                "beta_environment" not in names
                or db.execute("SELECT format FROM beta_environment").fetchall()
                != [("calendarsync-synthetic-beta-v1",)]
            ):
                raise HistoryRejected("The synthetic beta database marker is missing or invalid")
        elif "beta_environment" in names:
            raise HistoryRejected("Synthetic beta data cannot be adopted as a school schema")
        features = []
        for feature, tables in FEATURE_TABLES.items():
            if tables & names:
                if feature not in FEATURES.get(self.profile, {}) or not tables <= names:
                    raise HistoryRejected("An optional feature schema is partial or incompatible")
                features.append(feature)
        present = (
            *PROFILES[self.profile],
            *(FEATURES.get(self.profile, {})[name] for name in features),
        )
        history = "web_schema_history" in names
        validate_structure(db, self.backend, present, history=history)
        rows = []
        if history:
            for id, checksum, profile, kind, at in db.execute(
                "SELECT revision,checksum,profile,kind,recorded_at FROM web_schema_history ORDER BY revision"
            ).fetchall():
                if (
                    id not in present
                    or checksum != REVISIONS[id].checksum
                    or profile != self.profile
                    or kind not in {"observed", "applied"}
                    or type(at) is not int
                    or at < 0
                ):
                    raise HistoryRejected("Unknown, missing or modified schema history revision")
                rows.append({"id": id, "checksum": checksum, "kind": kind, "recorded_at": at})
        recorded = {row["id"] for row in rows}
        if any(not set(REVISIONS[id].parents) <= recorded for id in recorded):
            raise HistoryRejected("Schema history revision dependencies are incomplete")
        unrecorded = [id for id in present if id not in recorded]
        return {
            "profile": self.profile,
            "base_version": 1,
            "tracked": history and not unrecorded,
            "features": features,
            "recorded": rows,
            "unrecorded": unrecorded,
        }

    def check(self) -> dict:
        try:
            with self._connection(False) as db:
                return self._inspect(db)
        except HistoryRejected:
            raise
        except Exception:
            raise RuntimeError(
                "Schema history inspection failed; check the explicit database target"
            ) from None

    def _record(self, db, id: str, kind: str) -> None:
        query(
            db,
            self.backend,
            """INSERT INTO web_schema_history(revision,checksum,profile,kind,recorded_at)
            VALUES(%s,%s,%s,%s,%s)""",
            (id, REVISIONS[id].checksum, self.profile, kind, int(self.clock())),
        )

    def baseline(self, *, expected_sha256: str) -> dict:
        return self._change(None, expected_sha256)

    def upgrade(self, feature: str, *, expected_sha256: str) -> dict:
        return self._change(feature, expected_sha256)

    def _change(self, feature: str | None, expected_sha256: str) -> dict:
        plan = self.plan(feature)
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or expected_sha256 != plan["plan_sha256"]
        ):
            raise HistoryRejected("The reviewed schema history plan hash does not match")
        changed = []
        try:
            with self._connection(True) as db:
                status = self._inspect(db)
                if feature is None:
                    if "web_schema_history" not in table_names(db, self.backend):
                        db.execute(HISTORY_SQL)
                    for id in status["unrecorded"]:
                        self._record(db, id, "observed")
                        changed.append(id)
                else:
                    if not status["tracked"]:
                        raise HistoryRejected("Register the observed baseline before upgrading")
                    id = FEATURES[self.profile][feature]
                    if feature not in status["features"]:
                        for statement in REVISIONS[id].statements():
                            if (
                                re.match(
                                    r"CREATE (?:TABLE|INDEX) IF NOT EXISTS web_[a-z_]+\b", statement
                                )
                                is None
                            ):
                                raise HistoryRejected(
                                    "Feature upgrades must use the fixed additive statements"
                                )
                            db.execute(statement)
                        # Verify target structure before recording a successful execution.
                        self._inspect(db)
                        self._record(db, id, "applied")
                        changed.append(id)
                result = self._inspect(db)
        except HistoryRejected:
            raise
        except Exception:
            # Transactions roll back first. A lost commit acknowledgment needs
            # inspection, rather than an automatic retry or a success claim.
            raise RuntimeError(
                "Schema history update failed; inspect the target before retrying"
            ) from None
        return {
            **result,
            "operation": plan["operation"],
            "changed": changed,
            "plan_sha256": expected_sha256,
        }


class SQLiteHistory(_History):
    backend = "sqlite"

    def __init__(self, path, profile: str, *, clock=time.time):
        if profile not in PROFILES or not profile.startswith("sqlite-"):
            raise HistoryRejected("An explicit SQLite schema profile is required")
        super().__init__(profile, clock)
        self.path = Path(path)

    @contextmanager
    def _connection(self, write: bool):
        if not self.path.is_file():
            raise HistoryRejected("Schema history requires an existing SQLite database file")
        uri = self.path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
        db = sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class PostgresHistory(_History):
    backend = "postgres"

    def __init__(self, factory: PostgresConnectionFactory, tenant_id: str, *, clock=time.time):
        if (
            not isinstance(factory, PostgresConnectionFactory)
            or factory.schema in {"public", "information_schema"}
            or not isinstance(tenant_id, str)
            or not tenant_id
        ):
            raise HistoryRejected(
                "An explicit dedicated PostgreSQL factory and school tenant are required"
            )
        super().__init__("postgres-server", clock)
        self.factory, self.tenant_id = factory, tenant_id

    @contextmanager
    def _connection(self, write: bool):
        with self.factory() as db:
            if not write:
                db.execute("SET TRANSACTION READ ONLY")
            if db.execute("SELECT current_schema()").fetchone() != (self.factory.schema,):
                raise HistoryRejected("Schema history must use the dedicated target namespace")
            # Also coordinate with the two existing additive startup installers.
            for lock in (731904228, 731904226, 731904227):
                db.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            managed = ("web_users", "web_channels")
            if "web_channel_ingestion" in table_names(db, self.backend):
                managed += ("web_channel_ingestion",)
            if write:
                # Names are fixed managed tables, never input identifiers.
                db.execute("LOCK TABLE " + ",".join(managed) + " IN SHARE MODE")
            for table in managed:
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE tenant_id<>%s LIMIT 1", (self.tenant_id,)
                ).fetchone():
                    raise HistoryRejected("The schema belongs to another school tenant")
            yield db
