"""Reminder boundary cases without providers, databases or a web server."""

from datetime import datetime, timedelta, timezone

import pytest

from web_service.calendar_rules import KST
from web_service.reminder_rules import future_custom_times, reminder_times, within_reminder_window


@pytest.mark.parametrize("seconds,expected", [(-1, False), (0, True), (599, True), (600, False)])
def test_due_window_keeps_its_inclusive_start_and_exclusive_end(seconds, expected):
    due = datetime(2026, 10, 22, 23, 59, tzinfo=KST)
    assert within_reminder_window(due + timedelta(seconds=seconds), due) is expected


def test_times_keep_custom_timezone_order_and_duplicate_delivery_candidates():
    deadline = datetime(2026, 10, 22, 23, 59, tzinfo=KST)
    values = reminder_times(deadline, [-60, -60], ["2026-10-22T14:59:00Z", "invalid"])
    assert values == [deadline - timedelta(hours=1), deadline - timedelta(hours=1), deadline]
    assert all(value.tzinfo == KST for value in values)


def test_ancient_deadline_overflow_keeps_other_candidates():
    deadline = datetime(1, 1, 1, tzinfo=KST)
    assert reminder_times(deadline, [-60, 0], []) == [deadline]


def test_saved_expired_custom_time_is_removed_without_rejecting_other_changes():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    old, future = (now - timedelta(seconds=1)).isoformat(), (now + timedelta(days=366)).isoformat()
    assert future_custom_times([old, future], {old}, now) == [future]


@pytest.mark.parametrize("offset", [-1, 0, 366 * 86400 + 1])
def test_new_custom_time_must_be_in_the_existing_future_range(offset):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="within the next year"):
        future_custom_times([(now + timedelta(seconds=offset)).isoformat()], set(), now)
