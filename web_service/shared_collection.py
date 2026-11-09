"""Collect into the shared catalog without owning or renewing permission proofs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .collection_types import (
    CollectionCounter,
    CollectionReport,
    DeviceSnapshot,
    PreparedCollection,
)
from .connections import acquire_snapshot
from .school_graph import GraphFailure
from .storage import PermissionPending

if TYPE_CHECKING:
    from .server import ServerIntegration

COUNTS: tuple[CollectionCounter, ...] = (
    "channels_total",
    "channels_scanned",
    "messages_seen",
    "messages_analyzed",
    "messages_cached",
    "messages_failed",
)


def collect_shared(
    integration: ServerIntegration,
    device: DeviceSnapshot,
    prepared: PreparedCollection | None = None,
    *,
    force: bool = False,
):
    db = integration.db
    attempt = None
    closed = False
    try:
        current_proof = None if force else integration.collection_proof(device)
        # The permission operation has its own lease and commits before AI work.
        # Reuse an explicitly handed-off proof only for this live identity.
        if not (
            prepared
            and prepared["valid_until"] > db.clock()
            and all(
                prepared["device"][key] == device[key]
                for key in ("id_hash", "user_id", "tenant_id", "object_id", "version")
            )
            and current_proof is not None
            and prepared["channels"] == current_proof["channels"]
        ):
            prepared = current_proof or integration.run_sync(
                device, force=True, prepare_collection=True
            )
        if not prepared:
            return (
                {"state": "busy"}
                if db.sync_status(device["user_id"])["state"] == "checking"
                else None
            )
        token, device = acquire_snapshot(integration.tokens, device, db)
        tracked = bool(getattr(db, "planner_enhancements_enabled", False))
        attempt = db.claim_ingestion(device) if tracked else None
        if tracked and not attempt:
            return {"state": "busy"}
        channels = prepared["channels"]

        def progress(report: CollectionReport) -> None:
            if attempt:
                db.update_ingestion(device, attempt, report)

        options = (
            {"on_progress": progress}
            if attempt and getattr(integration.source, "supports_progress", False)
            else {}
        )
        events = None
        try:
            events = integration.source.collect(
                token, channels, device["tenant_id"], db, device["user_id"], **options
            )
            report = getattr(events, "report", None)
        except GraphFailure as error:
            if error.state not in {"verification_failed", "source_unavailable"}:
                raise
            # An interrupted post fetch cannot revoke independently verified ACLs.
            report = {**dict.fromkeys(COUNTS, 0), "channels_total": len(channels), "partial": True}
        if report is None:
            report = {**dict.fromkeys(COUNTS, 0), "channels_total": len(channels), "partial": False}
        if (
            not isinstance(report, dict)
            or type(report.get("partial")) is not bool
            or any(type(report.get(key)) is not int or report[key] < 0 for key in COUNTS)
        ):
            raise ValueError("Invalid ingestion report")
        scope = {channel["id"] for channel in channels}
        for name in ("checked_channels", "scanned_channels"):
            value = getattr(events, name, None)
            if value is not None and (
                not isinstance(value, (set, frozenset)) or not value <= scope
            ):
                raise ValueError("Invalid ingestion channel scope")
        try:
            # Publish upserts/tombstones only under the currently valid ACL.
            # Expiry leaves the catalog intact for the independent permission read.
            db.materialize_cached_events(device)
        except PermissionPending:
            pass
        if not db.finish_shared_collection(device, report, attempt=attempt):
            return {"state": "busy"}
        closed = True
        return {
            **{key: report[key] for key in COUNTS},
            "partial": report["partial"],
            "state": "partial" if report["partial"] else "completed",
        }
    except GraphFailure as error:
        if error.state in {"reauth_required", "account_not_allowed"}:
            db.fail_connection(device["id_hash"])
        if attempt:
            try:
                db.end_ingestion(device, attempt, error_code=error.state)
                closed = True
            except Exception:
                pass  # Revocation may already have invalidated the attempt's device.
    except Exception:
        # Fixed labels only: exception strings may contain source content/tokens.
        logging.getLogger("uvicorn.error").info("CalendarSync collection outcome=interrupted")
    finally:
        if attempt and not closed:
            try:
                db.end_ingestion(device, attempt, error_code="interrupted")
            except Exception:
                pass
    return None
