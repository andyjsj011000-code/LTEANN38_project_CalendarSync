"""Content-free collection validation and the existing synchronization response."""

from typing import Literal, NotRequired, TypedDict, cast

from .collection_types import CollectionCounter, CollectionReport

COUNTS: tuple[CollectionCounter, ...] = (
    "channels_total",
    "channels_scanned",
    "messages_seen",
    "messages_analyzed",
    "messages_cached",
    "messages_failed",
)


class IngestionSummary(TypedDict):
    channels_total: int
    # An unknown final scan scope must not claim a count after an ACL change.
    channels_scanned: NotRequired[int]
    messages_seen: int
    messages_analyzed: int
    messages_cached: int
    messages_failed: int
    partial: bool
    state: Literal["partial", "completed"]


class BusyResult(TypedDict):
    state: Literal["busy"]


def checked_report(value: object) -> CollectionReport:
    if (
        not isinstance(value, dict)
        or type(value.get("partial")) is not bool
        or any(type(value.get(key)) is not int or value[key] < 0 for key in COUNTS)
    ):
        raise ValueError("Invalid ingestion report")
    return cast(CollectionReport, value)


def summarize_report(report: CollectionReport) -> IngestionSummary:
    """Project an already validated report; do not add another validation step."""
    return {
        "channels_total": report["channels_total"],
        "channels_scanned": report["channels_scanned"],
        "messages_seen": report["messages_seen"],
        "messages_analyzed": report["messages_analyzed"],
        "messages_cached": report["messages_cached"],
        "messages_failed": report["messages_failed"],
        "partial": report["partial"],
        "state": "partial" if report["partial"] else "completed",
    }
