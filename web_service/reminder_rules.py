"""Existing reminder time rules, independent of storage, HTTP and delivery."""

from collections.abc import Collection, Iterable
from datetime import datetime, timedelta

from .calendar_rules import KST


def reminder_times(
    deadline: datetime, offsets: Iterable[int], custom_times: Iterable[str]
) -> list[datetime]:
    """Keep input order and duplicates; delivery owns deduplication and fencing."""
    times = []
    for offset in offsets:
        try:
            times.append(deadline + timedelta(minutes=offset))
        except OverflowError:
            continue
    for value in custom_times:
        try:
            times.append(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST))
        except (OverflowError, ValueError):
            continue
    return times


def within_reminder_window(now: datetime, due: datetime) -> bool:
    return 0 <= (now - due).total_seconds() < 600


def future_custom_times(
    values: Iterable[str], existing: Collection[str], now: datetime
) -> list[str]:
    """Drop previously saved expired times; reject newly supplied expired times."""
    future = []
    for value in values:
        when = datetime.fromisoformat(value)
        if when <= now and value in existing:
            continue
        if not now < when <= now + timedelta(days=366):
            raise ValueError("Custom reminders must be within the next year")
        future.append(value)
    return future
