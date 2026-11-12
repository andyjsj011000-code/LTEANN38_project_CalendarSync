"""Calendar display rules, independent of storage, HTTP and provider access.

Callers supply authorized rows. Personal tasks are already selected by date in
the repository; shared events must be filtered after applying personal edits.
These rules do not mutate their inputs.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import timedelta, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit

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


def _comparison_text(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())


def _same_reposted_schedule(first: Item, second: Item) -> bool:
    left, right = first["title"], second["title"]
    # Preserve grade numbers and ordinals even when punctuation separates them.
    if re.findall(r"\d+", unicodedata.normalize("NFKC", left)) != re.findall(
        r"\d+", unicodedata.normalize("NFKC", right)
    ):
        return False
    left, right = _comparison_text(left), _comparison_text(right)
    if not left or not right:
        return False
    distinctive = min(len(left), len(right)) >= 10
    if distinctive and left == right:
        return True
    if left != right and (not distinctive or SequenceMatcher(None, left, right).ratio() < 0.88):
        return False
    left_notes = _comparison_text((first.get("notes") or "")[:1000])
    right_notes = _comparison_text((second.get("notes") or "")[:1000])
    return (
        min(len(left_notes), len(right_notes)) >= 12
        and SequenceMatcher(None, left_notes, right_notes).ratio() >= 0.8
    )


def _teams_post_path(item: Item) -> str | None:
    source_url = item.get("source_url")
    if item["source"] != "teams" or not item.get("channel_id") or not isinstance(source_url, str):
        return None
    try:
        url = urlsplit(source_url)
    except ValueError:
        return None
    if url.scheme == "https" and url.netloc == "teams.microsoft.com":
        if re.fullmatch(r"/l/message/[^/]+/[^/]+/?", url.path):
            return url.path.rstrip("/")
    return None


def merge_teams_reposts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine visible Teams rows from different posts without changing stored identities."""
    result = []
    buckets: dict[
        tuple[str, str, str | None, str, bool],
        list[tuple[dict[str, Any], list[tuple[str, Item]]]],
    ] = {}
    for item in items:
        post = _teams_post_path(item)
        if not post:
            result.append(item)
            continue
        slot = (item["start_date"], item["end_date"], item.get("time"), item["kind"], item["cancelled"])
        groups = buckets.setdefault(slot, [])
        for representative, members in groups:
            if all(post != path and _same_reposted_schedule(item, other) for path, other in members):
                members.append((post, item))
                representative["related_sources"] = [
                    {
                        "id": member["id"],
                        "url": member["source_url"],
                        "channel": member["channel"],
                        "channel_id": member["channel_id"],
                    }
                    for _, member in members
                ]
                representative["completed"] = any(member["completed"] for _, member in members)
                break
        else:
            representative = dict(item)
            groups.append((representative, [(post, item)]))
            result.append(representative)
    return result


# 날짜·종류·채널 설정으로 표시할 일정을 고르되 숨김 복원 목록에는 비활성 설정을 적용하지 않는다.
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


# Teams 권한 확인 상태와 무관하게 사용자 자신의 오늘 할 일을 골라 시간순으로 정렬한다.
def personal_agenda_result(items: Iterable[PersonalTask], day: str) -> PersonalAgenda:
    """Personal-only path: a pending Teams proof must not hide a user's tasks."""
    visible = [item for item in items if not item["hidden"] and personal_task_appears(item, day)]
    return {
        "date": day,
        "personal": sorted(visible, key=lambda item: (item.get("time") or "99:99", item["id"])),
    }
