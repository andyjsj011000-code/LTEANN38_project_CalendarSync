"""Offline plans by default; existing targets are changed only by explicit actions."""

import argparse
import json
import os
from pathlib import Path

from .postgres_connection import PostgresConnectionFactory
from .schema_catalog import FEATURES, PROFILES, HistoryRejected, schema_plan
from .schema_history import PostgresHistory, SQLiteHistory


def main():
    parser = argparse.ArgumentParser(description="Inspect or explicitly track a web schema")
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--plan", action="store_true", help="Offline plan only (the default)")
    actions.add_argument("--check", action="store_true", help="Read-only structure/history check")
    actions.add_argument(
        "--baseline", action="store_true", help="Record the observed existing schema"
    )
    features = sorted({name for profile_features in FEATURES.values() for name in profile_features})
    actions.add_argument("--upgrade", choices=features)
    parser.add_argument("--feature", choices=features, help="Feature for an offline plan")
    parser.add_argument("--expected-sha256", help="Required reviewed plan hash for a write")
    parser.add_argument("--database", type=Path, help="Existing SQLite database file")
    parser.add_argument("--schema", help="Dedicated PostgreSQL namespace")
    parser.add_argument("--tenant", help="Expected school tenant for PostgreSQL")
    args = parser.parse_args()
    offline = not (args.check or args.baseline or args.upgrade)
    if args.feature is not None and not offline:
        parser.error("--feature is for offline plans; use --upgrade for an explicit upgrade")
    try:
        if offline:
            result = schema_plan(args.profile, args.feature)
        else:
            if args.profile.startswith("sqlite-"):
                if args.database is None or args.schema is not None or args.tenant is not None:
                    parser.error(
                        "SQLite actions require --database and no PostgreSQL target options"
                    )
                history = SQLiteHistory(args.database, args.profile)
            else:
                if args.database is not None or not args.schema or not args.tenant:
                    parser.error("PostgreSQL actions require --schema and --tenant")
                factory = PostgresConnectionFactory(
                    os.environ.get("DATABASE_URL", ""),
                    args.schema,
                    sslmode=os.environ.get("WEB_DATABASE_SSLMODE") or None,
                    sslrootcert=os.environ.get("WEB_DATABASE_SSLROOTCERT") or None,
                )
                history = PostgresHistory(factory, args.tenant)
            if args.check:
                result = history.check()
            else:
                if not args.expected_sha256:
                    parser.error("Explicit writes require --expected-sha256 from the reviewed plan")
                result = (
                    history.upgrade(args.upgrade, expected_sha256=args.expected_sha256)
                    if args.upgrade is not None
                    else history.baseline(expected_sha256=args.expected_sha256)
                )
    except HistoryRejected as error:
        parser.exit(1, str(error) + "\n")
    except (RuntimeError, ValueError):
        parser.exit(
            1, "Schema history operation failed; check the explicit target and reviewed plan.\n"
        )
    print(json.dumps(result, sort_keys=True))
    if args.check and not result["tracked"]:
        parser.exit(2)


if __name__ == "__main__":
    main()
