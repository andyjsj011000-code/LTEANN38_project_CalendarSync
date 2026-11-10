"""Synchronization workflow, independent of HTTP routes and the server adapter."""

from typing import cast

from .collection_types import DeviceIdentity, DeviceSnapshot, PreparedCollection
from .connection_snapshot import acquire_snapshot
from .oracle_source import OracleSource
from .school_graph import GraphFailure
from .shared_collection import collect_shared
from .sync_diagnostics import SyncDiagnostics
from .sync_ports import (
    EventSource,
    IngestionTrackingStore,
    SnapshotOptions,
    SyncContext,
    SyncResult,
    is_collection_source,
)
from .sync_results import checked_report, summarize_report


# 장치 자격증명과 채널 권한을 확인한 뒤 원본 조회·수집과 스냅샷 반영을 조율한다.
# 공유 수집은 별도 흐름에 맡기고, 동시 실행 제어와 실패·진행 상태 정리는 여기서 연결한다.
def run_sync(
    integration: SyncContext,
    device: DeviceSnapshot,
    ingest: bool = False,
    force: bool = False,
    *,
    prepare_collection: bool = False,
    prepared: PreparedCollection | None = None,
) -> SyncResult:
    if ingest and integration.shared_ingestion_enabled:
        return collect_shared(integration, device, prepared, force=force)
    diagnostics = SyncDiagnostics()
    diagnostics.progress("sync", "started")
    diagnostics.progress("lease", "started")
    user = device["user_id"]
    tracked = bool(
        ingest
        and is_collection_source(integration.source)
        and getattr(integration.db, "planner_enhancements_enabled", False)
    )
    attempt = (
        cast(IngestionTrackingStore, integration.db).claim_ingestion(device) if tracked else None
    )
    if tracked and not attempt:
        diagnostics.progress("sync", "skipped")
        return {"state": "busy"}
    attempt_closed = False

    def end_tracking(error_code=None, queued=False):
        nonlocal attempt_closed
        if attempt and not attempt_closed:
            try:
                cast(IngestionTrackingStore, integration.db).end_ingestion(
                    device, attempt, error_code=error_code, queued=queued
                )
            except Exception:
                pass
            attempt_closed = True

    def collection_progress(report):
        try:
            cast(IngestionTrackingStore, integration.db).update_ingestion(device, attempt, report)
        except Exception:
            pass

    lease = integration.db.begin_sync(
        user,
        0 if ingest or force else integration.settings.access_seconds,
        **({"preserve_access": True} if integration.shared_ingestion_enabled else {}),
    )
    diagnostics.progress("lease", "acquired" if lease else "skipped")
    if not lease:
        end_tracking(queued=True)
        diagnostics.progress("sync", "skipped")
        return {"state": "busy"} if ingest else None

    def fail_current(state):
        return integration.db.fail_sync(user, state, lease) if lease else False

    try:
        ingestion = None
        report = None
        reset_channels = False
        collected_scope = None
        collected_checked = None
        collected_scanned = None
        diagnostics.progress("token", "started")
        token, device = acquire_snapshot(integration.tokens, device, integration.db)
        diagnostics.progress("token", "completed")
        diagnostics.progress("channels", "started")
        channels = integration.graph.channels(token, device["object_id"], device["tenant_id"])
        verified_at = int(integration.db.clock())
        diagnostics.progress("channels", "completed")
        if not integration.source.configured:
            diagnostics.progress("source", "not_configured")
            diagnostics.progress("failure", "started")
            diagnostics.failure(fail_current("source_not_configured"))
            end_tracking("source_not_configured")
            return
        diagnostics.progress("source", "started")
        if is_collection_source(integration.source):
            if ingest:
                collected_scope = {ch["id"] for ch in channels}
                options = (
                    {"on_progress": collection_progress}
                    if attempt and getattr(integration.source, "supports_progress", False)
                    else {}
                )
                events = integration.source.collect(
                    token, channels, device["tenant_id"], integration.db, user, **options
                )
                report = getattr(events, "report", None)
                if tracked:
                    collected_checked = getattr(events, "checked_channels", None)
                    collected_scanned = getattr(events, "scanned_channels", None)
                    for scope in (collected_checked, collected_scanned):
                        if scope is not None and (
                            not isinstance(scope, (set, frozenset)) or not scope <= collected_scope
                        ):
                            raise ValueError("Invalid ingestion channel scope")
                if report is not None:
                    # Only content-free counts cross the API boundary. An
                    # incomplete pass must remain eligible for the worker.
                    ingestion = summarize_report(checked_report(report))
                if tracked:
                    # The legacy, non-shared path still owns its initial lease.
                    token, device = acquire_snapshot(integration.tokens, device, integration.db)
                    channels = integration.graph.channels(
                        token, device["object_id"], device["tenant_id"]
                    )
                    events = integration.db.catalog_events([ch["id"] for ch in channels])
                    final_scope = {ch["id"] for ch in channels}
                    if tracked and final_scope != collected_scope:
                        reset_channels = True
                        was_partial = bool(report and report.get("partial"))
                        if collected_checked is None and not was_partial:
                            collected_checked = collected_scope
                        report = {
                            **(report or {}),
                            "channels_total": len(final_scope),
                            "partial": was_partial or bool(final_scope - collected_scope),
                        }
                        for key, scope in (
                            ("channels_checked", collected_checked),
                            ("channels_scanned", collected_scanned),
                        ):
                            if scope is None:
                                report.pop(key, None)
                            else:
                                report[key] = len(final_scope & scope)
                        if ingestion is not None:
                            for key in ("channels_total", "channels_scanned"):
                                if key in report:
                                    ingestion[key] = report[key]
                                else:
                                    ingestion.pop(key, None)
                            ingestion.update(
                                partial=report["partial"],
                                state="partial" if report["partial"] else "completed",
                            )
            else:
                events = integration.db.catalog_events([ch["id"] for ch in channels])
        elif isinstance(integration.source, OracleSource):
            known = integration.db.known_oracle_events(user, device["tenant_id"], channels)
            events = integration.source.events(channels, device["tenant_id"], known_events=known)
        else:
            events = cast(EventSource, integration.source).events(channels, device["tenant_id"])
        diagnostics.progress("source", "completed")
        diagnostics.progress("commit", "started")
        context: SnapshotOptions = (
            {
                "ingestion": {
                    "attempt": attempt,
                    "report": report or {"partial": False},
                    "reset_channels": reset_channels,
                }
            }
            if tracked
            else {}
        )
        recorded = integration.db.commit_snapshot(
            device, channels, events, integration.settings.access_seconds, lease, **context
        )
        diagnostics.progress("commit", "completed")
        if tracked:
            attempt_closed = True
            if not recorded:
                return {"state": "busy"}
        if integration.shared_ingestion_enabled and not ingest:
            # Internal-only handoff. It contains no token and never enters
            # the API response; queued work revalidates the live device.
            identity: DeviceIdentity = {
                "id_hash": device["id_hash"],
                "user_id": device["user_id"],
                "tenant_id": device["tenant_id"],
                "object_id": device["object_id"],
                "version": device["version"],
            }
            prepared = cast(
                PreparedCollection,
                {
                    "device": identity,
                    "channels": [dict(ch) for ch in channels],
                    "valid_until": verified_at + integration.settings.access_seconds,
                },
            )
            # Bounded in-memory route hints; the database proof is checked on
            # every reuse. Restart, logout, expiry and scope changes invalidate it.
            if len(integration._collection_proofs) >= 128:
                integration._collection_proofs.clear()
            integration._collection_proofs[device["id_hash"]] = prepared
            if prepare_collection:
                return prepared
        if (
            ingest
            and is_collection_source(integration.source)
            and not tracked
            and not (ingestion and ingestion["partial"])
        ):
            # Advance the schedule only after the snapshot was committed;
            # failed commits and partial collection need a prompt retry.
            diagnostics.progress("ingest_mark", "started")
            integration.db.mark_ingested(device["id_hash"])
            diagnostics.progress("ingest_mark", "completed")
        if ingest:
            return ingestion
    except GraphFailure as error:
        diagnostics.progress("sync", "graph_failure")
        diagnostics.progress("failure", "started")
        diagnostics.failure(fail_current(error.state))
        end_tracking(error.state)
        if error.state in {"reauth_required", "account_not_allowed"}:
            integration.db.fail_connection(device["id_hash"])
    except Exception as error:
        diagnostics.exception(error)
        diagnostics.progress("sync", "source_unavailable")
        diagnostics.progress("failure", "started")
        diagnostics.failure(fail_current("source_unavailable"))
        integration.db.audit("sync_failed", user)
        end_tracking("source_unavailable")
    finally:
        end_tracking("interrupted")
        diagnostics.progress("sync", "finished")
