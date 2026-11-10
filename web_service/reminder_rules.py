"""Existing reminder time rules, independent of storage, HTTP and delivery."""

from collections.abc import Collection, Iterable
from datetime import datetime, timedelta

from .calendar_rules import KST


# 마감 시각의 상대 오프셋과 직접 지정한 시간을 모으되 입력 순서·중복은 발송 계층이 처리하도록 남긴다.
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


# 기존에 저장된 과거 예약은 제거하되 새로 입력한 과거 시각이나 허용 범위 밖의 예약은 거부한다.
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
