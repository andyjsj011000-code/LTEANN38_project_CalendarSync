"""Collect into the shared catalog without owning or renewing permission proofs."""

from __future__ import annotations

import logging
from typing import cast

from .collection_types import (
    CollectionReport,
    DeviceSnapshot,
    PreparedCollection,
)
from .connection_snapshot import acquire_snapshot
from .school_graph import GraphFailure
from .storage import PermissionPending
from .sync_ports import CollectionSource, IngestionTrackingStore, SharedCollectionStore, SyncContext
from .sync_results import COUNTS as COUNTS
from .sync_results import BusyResult, IngestionSummary, checked_report, summarize_report


# 유효한 권한 증명과 현재 장치 정보로 공유 카탈로그를 수집하고 진행·완료 상태를 기록한다.
# 수집 결과를 사용자에게 반영할 때 기존 접근 권한을 다시 확인하며 그 유효기간은 늘리지 않는다.
def collect_shared(
    integration: SyncContext,
    device: DeviceSnapshot,
    prepared: PreparedCollection | None = None,
    *,
    force: bool = False,
) -> IngestionSummary | BusyResult | None:
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
            prepared = current_proof or cast(
                PreparedCollection | None,
                integration.run_sync(device, force=True, prepare_collection=True),
            )
        if not prepared:
            return (
                {"state": "busy"}
                if cast(SharedCollectionStore, db).sync_status(device["user_id"])["state"]
                == "checking"
                else None
            )
        token, device = acquire_snapshot(integration.tokens, device, db)
        tracked = bool(getattr(db, "planner_enhancements_enabled", False))
        attempt = cast(IngestionTrackingStore, db).claim_ingestion(device) if tracked else None
        if tracked and not attempt:
            return {"state": "busy"}
        channels = prepared["channels"]

        def progress(report: CollectionReport) -> None:
            if attempt:
                cast(IngestionTrackingStore, db).update_ingestion(device, attempt, report)

        options = (
            {"on_progress": progress}
            if attempt and getattr(integration.source, "supports_progress", False)
            else {}
        )
        events = None
        try:
            events = cast(CollectionSource, integration.source).collect(
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
        validated = checked_report(report)
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
            cast(SharedCollectionStore, db).materialize_cached_events(device)
        except PermissionPending:
            pass
        if not cast(SharedCollectionStore, db).finish_shared_collection(
            device, report, attempt=attempt
        ):
            return {"state": "busy"}
        closed = True
        return summarize_report(validated)
    except GraphFailure as error:
        if error.state in {"reauth_required", "account_not_allowed"}:
            db.fail_connection(device["id_hash"])
        if attempt:
            try:
                cast(IngestionTrackingStore, db).end_ingestion(
                    device, attempt, error_code=error.state
                )
                closed = True
            except Exception:
                pass  # Revocation may already have invalidated the attempt's device.
    except Exception:
        # Fixed labels only: exception strings may contain source content/tokens.
        logging.getLogger("uvicorn.error").info("CalendarSync collection outcome=interrupted")
    finally:
        if attempt and not closed:
            try:
                cast(IngestionTrackingStore, db).end_ingestion(
                    device, attempt, error_code="interrupted"
                )
            except Exception:
                pass
    return None
