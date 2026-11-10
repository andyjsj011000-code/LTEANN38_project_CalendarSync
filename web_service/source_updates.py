"""Pure source-version, personal-edit and snapshot rules; no database imports.

Repositories own authorization, locks and writes. These rules operate only on
plain values and preserve source fingerprints already stored in the database.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any

EDITABLE_EVENT_FIELDS = frozenset(
    {"title", "notes", "time", "source_url", "start_date", "end_date", "kind", "cancelled"}
)
SOURCE_EVENT_FIELDS = EDITABLE_EVENT_FIELDS | {"source_version"}
_SOURCE_HASH_FIELDS = (
    "title",
    "notes",
    "time",
    "source_url",
    "start_date",
    "end_date",
    "kind",
    "cancelled",
    "source_version",
)


def source_hash(fields: Mapping[str, Any]) -> str:
    """Canonical comparison shared by publishing, review and retention."""
    payload = json.dumps(
        {key: fields.get(key) for key in _SOURCE_HASH_FIELDS}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_event_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    """Fields stored for a collected event, with explicit cancellation state."""
    fields = {key: event.get(key) for key in _SOURCE_HASH_FIELDS if key != "cancelled"}
    fields["cancelled"] = bool(event.get("cancelled", False))
    return fields


def source_display_fields(values: Any) -> dict[str, Any]:
    """Strip identities and private metadata from a stored source payload."""
    if not isinstance(values, dict):
        raise ValueError("Invalid event payload")
    return {key: value for key, value in values.items() if key in SOURCE_EVENT_FIELDS}


# 개인 편집은 표시 가능한 필드에만 덮어써 원본 식별자·상태·버전이 바뀌지 않게 한다.
def apply_personal_override(item: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Personal display edits never replace identities, state or source versions."""
    return {
        **item,
        **{key: value for key, value in override.items() if key in EDITABLE_EVENT_FIELDS},
    }


def validate_override_request(values: Any) -> None:
    if not isinstance(values, dict) or not values or set(values) - EDITABLE_EVENT_FIELDS:
        raise ValueError("Only event display fields may be overridden")


def validate_event_override(fields: Mapping[str, Any]) -> None:
    """Check the complete effective event after merging personal edits."""
    if (
        not isinstance(fields.get("title"), str)
        or not 1 <= len(fields["title"]) <= 500
        or not isinstance(fields.get("notes", ""), str)
        or len(fields.get("notes", "")) > 10000
    ):
        raise ValueError("Invalid event text")
    start, end = date.fromisoformat(fields["start_date"]), date.fromisoformat(fields["end_date"])
    if start > end:
        raise ValueError("Invalid event period")
    when = fields.get("time")
    if when is not None and (
        not isinstance(when, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", when) is None
    ):
        raise ValueError("Invalid event time")
    if (
        not isinstance(fields.get("kind"), str)
        or not fields["kind"]
        or not isinstance(fields.get("cancelled", False), bool)
    ):
        raise ValueError("Invalid event kind or cancellation")


# 원본의 식별자·기간·채널 소속을 검사하고 잠금 순서를 일정하게 정렬한다. 접근 권한 검사는 저장소가 맡는다.
def validate_source_snapshot(
    channels: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], list[tuple[str, Mapping[str, Any]]]]:
    """Validate source identities and dates, then keep deterministic lock order.

    This validates content, not access. The repository must still check the
    tenant, live permission proof and device generation under its own locks.
    """
    channels, events = list(channels), list(events)
    channel_ids = set()
    for channel in channels:
        if (
            not isinstance(channel.get("id"), str)
            or not channel["id"]
            or not isinstance(channel.get("name"), str)
            or channel["id"] in channel_ids
        ):
            raise ValueError("Invalid or duplicate snapshot channel")
        channel_ids.add(channel["id"])
    event_ids = set()
    result = []
    for event in events:
        if event.get("channel_id") not in channel_ids:
            raise ValueError("Source channel mismatch")
        if not isinstance(event.get("id"), str) or not event["id"]:
            raise ValueError("Event identity is required")
        event_id = event["id"] if event["id"].startswith("direct:") else "oracle:" + event["id"]
        if event_id in event_ids:
            raise ValueError("Duplicate snapshot event")
        event_ids.add(event_id)
        start, end = date.fromisoformat(event["start_date"]), date.fromisoformat(event["end_date"])
        if start > end or not isinstance(event.get("kind"), str) or not event["kind"]:
            raise ValueError("Invalid event dates or kind")
        result.append((event_id, event))
    return sorted(channels, key=lambda row: row["id"]), sorted(result, key=lambda row: row[0])


# 개인 편집이 없으면 원본을 자동 반영하고, 편집 충돌이 있더라도 취소 상태는 원본에 맞춘다.
def automatic_source_update(
    current: Mapping[str, Any] | None,
    seen_hash: str,
    incoming: Mapping[str, Any],
    fingerprint: Callable[[Mapping[str, Any]], str],
) -> tuple[dict[str, Any] | None, bool]:
    if current is not None and fingerprint(current) == seen_hash:
        return {**incoming, "_source_cancelled": incoming["cancelled"]}, True
    if current is not None and (incoming["cancelled"] or current.get("_source_cancelled")):
        # Cancellation/deletion is authoritative even for a locally edited item.
        # Keep a review for the other conflicting fields and preserve local state.
        return {
            **current,
            "cancelled": incoming["cancelled"],
            "_source_cancelled": incoming["cancelled"],
        }, False
    return None, False
