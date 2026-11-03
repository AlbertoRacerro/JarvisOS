from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RoadmapItemType = Literal[
    "Task",
    "Work package",
    "Milestone",
    "Investigation",
    "Validation",
    "Decision",
    "Procurement",
    "Manufacturing",
    "Meeting/Review",
]
RoadmapStatus = Literal["Planned", "Ready", "In progress", "Blocked", "Done", "Cancelled"]
RoadmapPriority = Literal["Critical", "High", "Normal", "Opportunity"]
CalendarEventType = Literal[
    "work session",
    "call/meeting",
    "experiment/lab",
    "review",
    "reminder",
    "deadline",
    "unavailable/personal",
]


class StrictDevelopmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("title", check_fields=False)
    @classmethod
    def reject_blank_title(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("title must contain non-whitespace characters")
        return value


class RoadmapItemCreate(StrictDevelopmentModel):
    workspace_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None
    item_type: RoadmapItemType
    status: RoadmapStatus = "Planned"
    priority: RoadmapPriority = "Normal"
    window_start_date: date | None = None
    window_end_date: date | None = None
    domain: str | None = None
    owner: str | None = None
    effort_estimate: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    cannot_start_before: date | None = None
    must_finish_before: date | None = None
    done_when: str | None = None
    done_when_satisfied: Literal[False] | None = None
    created_by: str = Field(min_length=1)


class RoadmapItemUpdate(StrictDevelopmentModel):
    workspace_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    item_type: RoadmapItemType | None = None
    status: RoadmapStatus | None = None
    priority: RoadmapPriority | None = None
    window_start_date: date | None = None
    window_end_date: date | None = None
    domain: str | None = None
    owner: str | None = None
    effort_estimate: str | None = None
    tags: list[str] | None = None
    notes: str | None = None
    cannot_start_before: date | None = None
    must_finish_before: date | None = None
    done_when: str | None = None
    done_when_satisfied: Literal[False] | None = None
    actor: str = Field(min_length=1)

    @field_validator("title", "item_type", "status", "priority", mode="before")
    @classmethod
    def reject_null_required_updates(cls, value: object) -> object:
        if value is None:
            raise ValueError("required update field must not be null")
        return value

    @model_validator(mode="after")
    def reject_atomic_done_gate_clear(self) -> RoadmapItemUpdate:
        if self.status == "Done" and "done_when" in self.model_fields_set and not self.done_when:
            raise ValueError("done_when must be cleared separately before transitioning to Done")
        return self


class CalendarAllocationCreate(StrictDevelopmentModel):
    workspace_id: str = Field(min_length=1)
    roadmap_item_id: str | None = None
    title: str = Field(min_length=1)
    event_type: CalendarEventType
    start_local: datetime
    end_local: datetime
    timezone: str = Field(min_length=1)
    start_utc_offset_minutes: int | None = Field(default=None, ge=-24 * 60, le=24 * 60)
    end_utc_offset_minutes: int | None = Field(default=None, ge=-24 * 60, le=24 * 60)
    all_day: bool = False
    deadline: bool = False
    description: str | None = None
    priority: RoadmapPriority | None = None
    domain: str | None = None
    location: str | None = None
    meeting_link: str | None = None
    reminder: dict[str, object] | None = None
    tags: list[str] = Field(default_factory=list)
    created_by: str = Field(min_length=1)


class CalendarAllocationUpdate(StrictDevelopmentModel):
    workspace_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    roadmap_item_id: str | None = None
    title: str | None = Field(default=None, min_length=1)
    event_type: CalendarEventType | None = None
    start_local: datetime | None = None
    end_local: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1)
    start_utc_offset_minutes: int | None = Field(default=None, ge=-24 * 60, le=24 * 60)
    end_utc_offset_minutes: int | None = Field(default=None, ge=-24 * 60, le=24 * 60)
    all_day: bool | None = None
    deadline: bool | None = None
    description: str | None = None
    priority: RoadmapPriority | None = None
    domain: str | None = None
    location: str | None = None
    meeting_link: str | None = None
    reminder: dict[str, object] | None = None
    tags: list[str] | None = None
    actor: str = Field(min_length=1)

    @field_validator("title", "event_type", mode="before")
    @classmethod
    def reject_null_required_updates(cls, value: object) -> object:
        if value is None:
            raise ValueError("required update field must not be null")
        return value
