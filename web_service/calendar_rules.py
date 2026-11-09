"""Calendar display rules, independent of storage, HTTP and provider access.

Callers supply authorized rows. Personal tasks are already selected by date in
the repository; shared events must be filtered after applying personal edits.
Every function returns the existing API shapes without mutating its inputs.
"""

from collections.abc import Iterable, Mapping
from datetime import timedelta, timezone
from typing import Any

from .personal_types import PersonalAgenda, PersonalTask

KST = timezone(timedelta(hours=9))

Item = Mapping[str, Any]


def personal_task_appears(item: Item, day: str) -> bool:
    """Today-list rule for an in-range task. Its final day always appears."""
    return (
        item["end_date"] == day
        or item["display_mode"] == "daily"
        or (item["display_mode"] == "selected" and day in item["selected_dates"])
    )


def calendar_sort_key(item: Item) -> tuple[str, str, str]:
    # Missing times sort last. Completion never changes an item's position.
    return item.get("time") or "99:99", item["source"], item["id"]


def calendar_result(
    personal: list[Item],
    shared: Iterable[Item],
    preferences: Mapping[str, Any],
    start: str,
    end: str,
    hidden_only: bool = False,
) -> list[dict[str, Any]]:
    """Apply date overlap, channel/kind preferences, hidden mode and ordering.

    Hidden mode is a restore list: disabled channels and kinds do not hide its
    entries. Cancelled and completed items remain visible in either mode.
    """
    teams = [item for item in shared if item["start_date"] <= end and item["end_date"] >= start]
    result = []
    for item in personal + teams:
        if hidden_only:
            if not item["hidden"]:
                continue
        elif item["hidden"] or (
            item["source"] == "teams"
            and (not item["channel_enabled"] or item["kind"] in preferences["disabled_kinds"])
        ):
            continue
        result.append({key: value for key, value in item.items() if key != "channel_enabled"})
    return sorted(result, key=calendar_sort_key)


def agenda_result(items: Iterable[Item], day: str) -> dict[str, Any]:
    """Group an already ordered calendar day without moving overdue tasks."""
    items = list(items)
    return {
        "date": day,
        "teams": [item for item in items if item["source"] == "teams"],
        "personal": [
            item
            for item in items
            if item["source"] == "personal" and personal_task_appears(item, day)
        ],
    }


def personal_agenda_result(items: Iterable[PersonalTask], day: str) -> PersonalAgenda:
    """Personal-only path: a pending Teams proof must not hide a user's tasks."""
    visible = [item for item in items if not item["hidden"] and personal_task_appears(item, day)]
    return {
        "date": day,
        "personal": sorted(visible, key=lambda item: (item.get("time") or "99:99", item["id"])),
    }
