"""Opt-in retention for shared Teams ingestion; no schema or connection setup.

Retired cache entries keep source identities and versions, but no notice text.
Writers must lock source-cache rows before event rows, in identity order. The
shared snapshot writer must reread these rows while locked before publishing;
otherwise an already-read catalog could resurrect an event after retirement.
"""

import re
from datetime import date, datetime, timedelta, timezone

from .source_updates import source_event_fields, source_hash
from .storage import digest

_MARKER_FIELDS = frozenset({"id", "key", "source_version", "source_hash", "end_date"})


def decode_cache_payload(payload, channel):
    """Read old event lists or the compact envelope, without granting access."""
    if isinstance(payload, list):
        events, retired = payload, []
    elif isinstance(payload, dict) and set(payload) == {"events", "retired"}:
        events, retired = payload["events"], payload["retired"]
    else:
        raise ValueError("Invalid source cache payload")
    if not isinstance(events, list) or any(
        not isinstance(event, dict) or event.get("channel_id") != channel for event in events
    ):
        raise ValueError("Source cache channel mismatch")
    if not isinstance(retired, list):
        raise ValueError("Invalid retired source markers")
    active_ids = {event.get("id") for event in events if isinstance(event.get("id"), str)}
    seen = set()
    for marker in retired:
        if (
            not isinstance(marker, dict)
            or set(marker) - _MARKER_FIELDS
            or not isinstance(marker.get("id"), str)
            or not 1 <= len(marker["id"]) <= 200
            or marker["id"] in seen
            or marker["id"] in active_ids
            or not isinstance(marker.get("end_date"), str)
        ):
            raise ValueError("Invalid retired source marker")
        try:
            if date.fromisoformat(marker["end_date"]).isoformat() != marker["end_date"]:
                raise ValueError()
        except ValueError:
            raise ValueError("Invalid retired source date") from None
        key = marker.get("key")
        if key is not None and (
            not isinstance(key, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key) is None
        ):
            raise ValueError("Invalid retired source key")
        version = marker.get("source_version")
        if version is not None and (not isinstance(version, str) or len(version) > 200):
            raise ValueError("Invalid retired source version")
        source_hash = marker.get("source_hash")
        if source_hash is not None and (
            not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
        ):
            raise ValueError("Invalid retired source hash")
        seen.add(marker["id"])
    return {"events": events, "retired": retired}


def retired_event_marker(event):
    """Keep stable extraction keys and canonical hashes, without source content."""
    fields = source_event_fields(event)
    marker = {key: event.get(key) for key in ("id", "key", "source_version", "end_date")}
    marker["source_hash"] = source_hash(fields)
    # Use the same validation for generated and already stored markers.
    decode_cache_payload({"events": [], "retired": [marker]}, event.get("channel_id"))
    return marker


class PostgresRetention:
    def prune_expired_team_events(self, retention_days=7, batch_size=100):
        """Retire bounded cache batches after seven full days, using Seoul dates.

        Personal tasks are never read or modified. A recent/future date in any
        user's override, a pending change, or an outstanding explicit reminder
        protects the shared event.
        The process-local cursor rotates through retained markers and protected
        posts; a restart only restarts scanning, never weakens the date checks.
        """
        if (
            type(retention_days) is not int
            or not 0 <= retention_days <= 3650
            or type(batch_size) is not int
            or not 1 <= batch_size <= 1000
        ):
            raise ValueError("Invalid retention bounds")
        result = {
            "cache_rows_scanned": 0,
            "events_deleted": 0,
            "cached_events_retired": 0,
            "events_preserved": 0,
        }
        if not getattr(self, "shared_ingestion_enabled", False):
            return result
        today = datetime.fromtimestamp(self.clock(), timezone(timedelta(hours=9))).date()
        reminder_cutoff = datetime.fromtimestamp(self.clock(), timezone.utc) - timedelta(
            seconds=600
        )
        cutoff = (today - timedelta(days=retention_days)).isoformat()
        cursor = getattr(self, "_retention_cache_cursor", "")
        next_cursor = cursor
        with self.connect() as db:
            rows = db.execute(
                """SELECT cache_key,channel_id,message_id,version,payload_blob
                FROM web_source_cache WHERE cache_key>%s ORDER BY cache_key
                LIMIT %s FOR UPDATE SKIP LOCKED""",
                (cursor, batch_size),
            ).fetchall()
            if not rows and cursor:
                rows = db.execute(
                    """SELECT cache_key,channel_id,message_id,version,payload_blob
                    FROM web_source_cache ORDER BY cache_key
                    LIMIT %s FOR UPDATE SKIP LOCKED""",
                    (batch_size,),
                ).fetchall()
            if not rows:
                self._retention_cache_cursor = ""
                return result
            decoded = []
            for row in rows:
                if row[0] != digest(row[1] + "|" + row[2]):
                    raise ValueError("Source cache identity mismatch")
                decoded.append((row, decode_cache_payload(self.unseal(row[4]), row[1])))
            result["cache_rows_scanned"] = len(rows)
            # Cache locks above use cache_key order. Event locks must likewise
            # be globally ordered even when one notice contains many events.
            candidates = {}
            for row, payload in decoded:
                for event in payload["events"]:
                    end = event.get("end_date")
                    if end is None:
                        continue  # A legacy row without a date is not proof of expiry.
                    if not isinstance(end, str) or date.fromisoformat(end).isoformat() != end:
                        raise ValueError("Invalid cached event date")
                    if end >= cutoff:
                        continue
                    identity = event.get("id")
                    if not isinstance(identity, str) or not identity.startswith("direct:"):
                        continue
                    if identity in candidates:
                        raise ValueError("Duplicate cached event identity")
                    candidates[identity] = (row[1], event)
            removable = set()
            for identity, (channel, event) in sorted(candidates.items()):
                if len(removable) >= batch_size:
                    break
                current = db.execute(
                    """SELECT channel_id,end_date,payload_blob
                    FROM web_events WHERE id=%s FOR UPDATE SKIP LOCKED""",
                    (identity,),
                ).fetchone()
                if current is None:
                    # SKIP LOCKED and a truly absent row differ: never retire
                    # data while another writer is extending the event's date.
                    if db.execute("SELECT 1 FROM web_events WHERE id=%s", (identity,)).fetchone():
                        result["events_preserved"] += 1
                        continue
                else:
                    if current[0] != channel:
                        raise ValueError("Retained event channel mismatch")
                    if db.execute(
                        "SELECT 1 FROM web_changes WHERE event_id=%s AND status='pending' LIMIT 1",
                        (identity,),
                    ).fetchone():
                        result["events_preserved"] += 1
                        continue
                    # Maintenance runs before dispatch. Keep future reminders
                    # and the ten-minute delivery window so a reminder due now
                    # cannot delete itself before being sent.
                    outstanding_reminder = False
                    rules = db.execute(
                        "SELECT payload_blob FROM web_reminder_rules WHERE source='teams' AND item_id=%s",
                        (identity,),
                    ).fetchall()
                    for (rule,) in rules:
                        try:
                            values = self.unseal(rule)
                            if not isinstance(values, dict) or not isinstance(
                                values.get("custom_times", []), list
                            ):
                                raise ValueError("Invalid custom reminder rules")
                            for value in values.get("custom_times", []):
                                if not isinstance(value, str):
                                    raise ValueError("Invalid custom reminder time")
                                due = datetime.fromisoformat(value.replace("Z", "+00:00"))
                                if due.tzinfo is None or due.utcoffset() is None:
                                    raise ValueError("Custom reminder time must have a timezone")
                                if due > reminder_cutoff:
                                    outstanding_reminder = True
                        except Exception:
                            # Invalid rule data is not evidence that an explicit
                            # scheduled action can safely be removed.
                            outstanding_reminder = True
                        if outstanding_reminder:
                            break
                    if outstanding_reminder:
                        result["events_preserved"] += 1
                        continue
                    payloads = [self.unseal(current[2])]
                    payloads.extend(
                        self.unseal(row[0])
                        for row in db.execute(
                            """SELECT override_blob
                        FROM web_event_states WHERE event_id=%s AND override_blob IS NOT NULL""",
                            (identity,),
                        ).fetchall()
                    )
                    ends = [current[1]]
                    for value in payloads:
                        if not isinstance(value, dict) or not isinstance(
                            value.get("end_date"), str
                        ):
                            raise ValueError("Invalid retained event payload")
                        ends.append(value["end_date"])
                    for end in ends:
                        if not isinstance(end, str) or date.fromisoformat(end).isoformat() != end:
                            raise ValueError("Invalid retained event date")
                    if any(end >= cutoff for end in ends):
                        result["events_preserved"] += 1
                        continue
                # Construct the marker before deleting any related records.
                retired_event_marker(event)
                removable.add(identity)
            for row, payload in decoded:
                expired = [event for event in payload["events"] if event.get("id") in removable]
                if expired:
                    retired = {marker["id"]: marker for marker in payload["retired"]}
                    retired.update((event["id"], retired_event_marker(event)) for event in expired)
                    replacement = {
                        "events": [
                            event for event in payload["events"] if event.get("id") not in removable
                        ],
                        "retired": [retired[key] for key in sorted(retired)],
                    }
                    db.execute(
                        "UPDATE web_source_cache SET payload_blob=%s WHERE cache_key=%s",
                        (self.seal(replacement), row[0]),
                    )
                    result["cached_events_retired"] += len(expired)
                next_cursor = row[0]
            for identity in sorted(removable):
                # No cascading FKs are assumed. Generic rules need an explicit
                # source predicate so a coincident personal ID is untouched.
                db.execute(
                    "DELETE FROM web_reminder_rules WHERE source='teams' AND item_id=%s",
                    (identity,),
                )
                db.execute("DELETE FROM web_changes WHERE event_id=%s", (identity,))
                db.execute("DELETE FROM web_user_source_versions WHERE event_id=%s", (identity,))
                db.execute("DELETE FROM web_event_states WHERE event_id=%s", (identity,))
                db.execute("DELETE FROM web_event_sources WHERE event_id=%s", (identity,))
                result["events_deleted"] += db.execute(
                    "DELETE FROM web_events WHERE id=%s", (identity,)
                ).rowcount
        self._retention_cache_cursor = next_cursor
        return result
