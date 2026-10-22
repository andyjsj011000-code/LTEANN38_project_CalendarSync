from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskInput(Input):
    title: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=10000)
    start_date: date
    end_date: date | None = None
    time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    display_mode: Literal["daily", "selected", "final"] = "daily"
    selected_dates: list[date] = Field(default_factory=list, max_length=3660)

    @model_validator(mode="after")
    def normalize_dates(self):
        self.end_date = self.end_date or self.start_date
        if self.end_date < self.start_date:
            raise ValueError("End date must not precede start date")
        if any(not self.start_date <= day <= self.end_date for day in self.selected_dates):
            raise ValueError("Selected dates must be inside the task period")
        if self.display_mode == "selected":
            self.selected_dates = sorted(set(self.selected_dates) | {self.end_date})
        else:
            self.selected_dates = []
        return self


class TaskUpdate(TaskInput):
    version: int = Field(ge=1)


class TaskState(Input):
    version: int = Field(ge=1)
    completed: bool | None = None
    hidden: bool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if self.completed is None and self.hidden is None:
            raise ValueError("Supply completed or hidden")
        return self


class EventState(Input):
    completed: bool | None = None
    hidden: bool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if self.completed is None and self.hidden is None:
            raise ValueError("Supply completed or hidden")
        return self


class Preferences(Input):
    morning_time: str = Field(default="07:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    morning_enabled: bool = True
    teams_chat_enabled: bool = False
    calendar_sync_enabled: bool = False
    disabled_kinds: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def normalize_kinds(self):
        if any(not 1 <= len(k) <= 50 for k in self.disabled_kinds):
            raise ValueError("Invalid kind")
        self.disabled_kinds = sorted(set(self.disabled_kinds))
        return self


class ChannelPreference(Input):
    enabled: bool
