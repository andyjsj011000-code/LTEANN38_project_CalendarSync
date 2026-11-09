"""Pinned revision sources for existing web schemas and additive features.

Baseline scripts are reference definitions, never replayed against an existing
database. Only the two closed, creation-only feature scripts can be upgraded.
Keep published scripts/checksums immutable; add a revision for future changes.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).with_name("schema_revisions")


class HistoryRejected(ValueError):
    """Content-free rejection; driver errors and row values are never exposed."""


@dataclass(frozen=True)
class Revision:
    id: str
    script: str
    checksum: str
    parents: tuple[str, ...] = ()

    def source(self) -> str:
        source = (_ROOT / self.script).read_text(encoding="utf-8").strip() + "\n"
        if hashlib.sha256(source.encode()).hexdigest() != self.checksum:
            raise HistoryRejected("A published schema revision checksum has changed")
        return source

    def statements(self) -> tuple[str, ...]:
        source = "\n".join(
            line for line in self.source().splitlines() if not line.lstrip().startswith("--")
        )
        return tuple(part.strip() for part in source.split(";") if part.strip())


REVISIONS = {
    r.id: r
    for r in (
        Revision(
            "sqlite-base-v1",
            "sqlite_base_v1.sql",
            "05b5c5b853848cfec7c402c68133a10720a71dc8557f2c3600d1c1c5cf2a8167",
        ),
        Revision(
            "sqlite-school-v1",
            "sqlite_school_v1.sql",
            "deefe1b91b33ef0b41d8a8f70637a2ffecacc8c6238db90e2c532515285ef185",
            ("sqlite-base-v1",),
        ),
        Revision(
            "sqlite-server-v1",
            "sqlite_server_v1.sql",
            "4f43c6a606e48ced35493c8d952da0658790420e3a829b40d3a770ae69566352",
            ("sqlite-school-v1",),
        ),
        Revision(
            "sqlite-beta-v1",
            "sqlite_beta_v1.sql",
            "373b422cfb01e3127804f405947b27cc5249c7b045e1faacdbb753c4c03992ca",
            ("sqlite-base-v1",),
        ),
        Revision(
            "postgres-base-v1",
            "postgres_base_v1.sql",
            "1b46c0451ef0fae2d2cf65e9595c80e3cc1dd152f80bcf322982ad35a006ccb2",
        ),
        Revision(
            "sqlite-planner-v1",
            "planner_v1.sql",
            "9c3d04fc18f2e3cf49ca5340c92d950a30f795c48a8bf80760f2de30cea98b44",
            ("sqlite-server-v1",),
        ),
        Revision(
            "postgres-shared-v1",
            "shared_ingestion_v1.sql",
            "f5e974c6d90c88e8ef32dad23413b7f6be0d6ad9820d80c9ab9f415c724cfa3d",
            ("postgres-base-v1",),
        ),
        Revision(
            "postgres-planner-v1",
            "planner_v1.sql",
            "9c3d04fc18f2e3cf49ca5340c92d950a30f795c48a8bf80760f2de30cea98b44",
            ("postgres-base-v1",),
        ),
    )
}
PROFILES = {
    "sqlite-foundation": ("sqlite-base-v1",),
    "sqlite-school": ("sqlite-base-v1", "sqlite-school-v1"),
    "sqlite-server": ("sqlite-base-v1", "sqlite-school-v1", "sqlite-server-v1"),
    "sqlite-beta": ("sqlite-base-v1", "sqlite-beta-v1"),
    "postgres-server": ("postgres-base-v1",),
}
FEATURES = {
    "sqlite-server": {"planner": "sqlite-planner-v1"},
    "postgres-server": {"shared-ingestion": "postgres-shared-v1", "planner": "postgres-planner-v1"},
}
FEATURE_TABLES = {
    "planner": frozenset({"web_task_drafts", "web_task_trash", "web_ingestion_status"}),
    "shared-ingestion": frozenset({"web_channel_ingestion"}),
}
HISTORY_SQL = """CREATE TABLE web_schema_history (
    revision TEXT PRIMARY KEY NOT NULL, checksum TEXT NOT NULL,
    profile TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('observed','applied')),
    recorded_at BIGINT NOT NULL CHECK(recorded_at>=0))"""


def schema_plan(profile: str, feature: str | None = None) -> dict:
    if profile not in PROFILES or (
        feature is not None and feature not in FEATURES.get(profile, {})
    ):
        raise HistoryRejected("Unsupported schema profile or feature")
    ids = (*PROFILES[profile], *FEATURES.get(profile, {}).values())
    revisions = []
    for id in ids:
        revision = REVISIONS[id]
        revision.source()  # Verify the bundled bytes even for an offline plan.
        revisions.append({"id": id, "checksum": revision.checksum, "parents": revision.parents})
    plan = {
        "profile": profile,
        "operation": "upgrade" if feature is not None else "baseline",
        "feature": feature,
        "history_format": 1,
        "history_checksum": hashlib.sha256((HISTORY_SQL.strip() + "\n").encode()).hexdigest(),
        "known_revisions": revisions,
        "automatic_migration": False,
    }
    digest = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**plan, "plan_sha256": digest}
