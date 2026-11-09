"""Storage operations consumed by the personal-task and settings routes.

Base and beta SQLite stores need no planner methods. The existing feature flag
still selects trash support; its separate protocol describes that capability.
"""

from typing import Protocol

from .personal_types import (
    PersonalAgenda,
    PersonalTask,
    PreferenceSnapshot,
    PreferenceValues,
    TaskChanges,
    TaskPayload,
)


class PersonalTaskStore(Protocol):
    def clock(self) -> float: ...

    def add_task(self, user_id: str, payload: TaskPayload) -> PersonalTask: ...

    def task(self, user_id: str, item_id: str) -> PersonalTask: ...

    def update_task(
        self, user_id: str, item_id: str, payload: TaskPayload, version: int
    ) -> PersonalTask: ...

    def task_state(
        self, user_id: str, item_id: str, values: TaskChanges, version: int
    ) -> PersonalTask: ...

    def delete_task(self, user_id: str, item_id: str, version: int) -> None: ...

    def personal_agenda(self, user_id: str, tenant_id: str, day: str) -> PersonalAgenda: ...


class TaskTrashStore(Protocol):
    def trash_task(self, user_id: str, task_id: str, version: int) -> object: ...


class PreferenceStore(Protocol):
    def preferences(self, user_id: str) -> PreferenceSnapshot: ...

    def save_preferences(self, user_id: str, values: PreferenceValues) -> PreferenceValues: ...

    def channel_preference(
        self, user_id: str, tenant_id: str, channel_id: str, enabled: bool
    ) -> None: ...
