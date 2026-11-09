"""Basic schema consistency only; an actual PostgreSQL test is still required."""

import sqlite3
from pathlib import Path

from web_service.school_storage import SCHOOL_SCHEMA
from web_service.test_storage import EXTRA_SCHEMA


def test_draft_covers_existing_web_tables_and_is_repeatable():
    root = Path(__file__).parents[1] / "web_service"
    expected = sqlite3.connect(":memory:")
    expected.executescript((root / "schema.sql").read_text(encoding="utf-8"))
    expected.executescript(SCHOOL_SCHEMA)
    expected.executescript(EXTRA_SCHEMA)
    draft = sqlite3.connect(":memory:")
    script = (root / "postgresql_schema_draft.sql").read_text(encoding="utf-8")
    draft.executescript(script)
    draft.executescript(script)
    tables = lambda db: {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    notification_tables = {
        "graph_subscriptions",
        "graph_notification_jobs",
        "graph_subscription_alerts",
        "graph_channel_registry",
        "graph_candidate_hints",
        "graph_notification_targets",
    }
    assert tables(draft) == tables(expected) | notification_tables
    assert draft.execute("SELECT version FROM web_schema_version").fetchall() == [(1,)]
