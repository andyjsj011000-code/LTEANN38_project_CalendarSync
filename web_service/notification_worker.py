"""Process a queued channel with one explicitly authorised delegated connection.

The caller selects a currently authorised connection and schedules retry/backoff.
This worker does not select subscribers or provision/renew Graph subscriptions.
"""

from .connections import acquire_snapshot
from .school_graph import GraphFailure


def process_claimed(job, device, db, tokens, graph, source, access_seconds):
    lease, current = None, None
    try:
        if job.get("requires_reconciliation"):
            return "requires_reconciliation"
        current = db.device(device["id_hash"])
        if (
            not current
            or current["tenant_id"] != job["tenant_id"]
            or current["user_id"] != device["user_id"]
        ):
            return "reauth_required"
        lease = db.begin_sync(current["user_id"], 0)
        if not lease:
            return "busy"
        token, current = acquire_snapshot(tokens, current, db)
        channels = graph.channels(token, current["object_id"], current["tenant_id"])
        selected = [
            channel
            for channel in channels
            if channel["id"] == job["channel_id"] and channel["team_id"] == job["team_id"]
        ]
        if len(selected) != 1:
            db.fail_sync(current["user_id"], "verification_failed", lease)
            return "channel_not_authorized"
        arguments = {"selected_channels": selected}
        if "selected_messages" in job:
            arguments["selected_messages"] = job["selected_messages"]
        events = source.collect(
            token, channels, current["tenant_id"], db, current["user_id"], **arguments
        )
        # commit_snapshot must recheck that the device and access lease remain valid.
        db.commit_snapshot(current, channels, events, access_seconds, lease)
        return "processed"
    except GraphFailure:
        if current and lease:
            db.fail_sync(current["user_id"], "verification_failed", lease)
        return "graph_unavailable"
    except Exception:
        if current and lease:
            db.fail_sync(current["user_id"], "source_unavailable", lease)
        return "processing_failed"


def process_notification(inbox, device, db, tokens, graph, source, access_seconds):
    job = inbox.claim(seconds=300)
    if not job:
        return "empty"
    result = "processing_failed"
    try:
        result = process_claimed(job, device, db, tokens, graph, source, access_seconds)
        return result
    finally:
        # Match the runtime's bounded retry policy so an unavailable Graph
        # endpoint cannot cause the same notification to spin continuously.
        inbox.finish(job, result == "processed", retry_delay=0 if result == "processed" else 60)
