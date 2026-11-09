"""Personal-task and preference dictionaries at the existing JSON boundary.

Request converters produce complete payloads. Stored records may come from
older or partial writes, so their optional fields stay optional. These types
do not revalidate, fill in or strip persisted fields.
"""

from typing import Literal, NotRequired, TypedDict

DisplayMode = Literal["daily", "selected", "final"]


class TaskContent(TypedDict):
    title: str
    start_date: str
    end_date: str


class TaskPayload(TaskContent):
    notes: str
    time: str | None
    display_mode: DisplayMode
    selected_dates: list[str]


class TaskChanges(TypedDict, total=False):
    completed: bool | None
    hidden: bool | None


class PersonalTask(TaskContent):
    notes: NotRequired[str]
    time: NotRequired[str | None]
    display_mode: NotRequired[DisplayMode]
    selected_dates: NotRequired[list[str]]
    id: str
    source: Literal["personal"]
    kind: Literal["personal"]
    completed: bool
    hidden: bool
    version: int
    cancelled: bool


class PersonalAgenda(TypedDict):
    date: str
    personal: list[PersonalTask]


class PreferenceValues(TypedDict):
    morning_time: str
    morning_enabled: bool
    teams_chat_enabled: bool
    calendar_sync_enabled: bool
    disabled_kinds: list[str]


class PreferenceSnapshot(TypedDict, total=False):
    """Reads preserve the subset actually stored, including legacy rows."""

    morning_time: str
    morning_enabled: bool
    teams_chat_enabled: bool
    calendar_sync_enabled: bool
    disabled_kinds: list[str]
