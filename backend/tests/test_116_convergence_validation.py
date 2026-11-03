from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.modules.development.models import (
    CalendarAllocationCreate,
    CalendarAllocationUpdate,
    RoadmapItemCreate,
    RoadmapItemUpdate,
)


@pytest.mark.parametrize("field", ["title", "item_type", "status", "priority"])
def test_roadmap_required_update_fields_reject_explicit_null(field: str) -> None:
    with pytest.raises(ValidationError):
        RoadmapItemUpdate(
            workspace_id="workspace",
            expected_revision=1,
            actor="tester",
            **{field: None},
        )


@pytest.mark.parametrize("field", ["title", "event_type"])
def test_calendar_required_update_fields_reject_explicit_null(field: str) -> None:
    with pytest.raises(ValidationError):
        CalendarAllocationUpdate(
            workspace_id="workspace",
            expected_revision=1,
            actor="tester",
            **{field: None},
        )


def test_roadmap_done_transition_cannot_atomically_clear_gate() -> None:
    with pytest.raises(ValidationError):
        RoadmapItemUpdate(
            workspace_id="workspace",
            expected_revision=1,
            status="Done",
            done_when=None,
            actor="tester",
        )


def test_roadmap_gate_can_be_cleared_in_separate_update() -> None:
    update = RoadmapItemUpdate(
        workspace_id="workspace",
        expected_revision=1,
        done_when=None,
        actor="tester",
    )
    assert update.status is None
    assert "done_when" in update.model_fields_set


@pytest.mark.parametrize("title", [" ", "\t\n"])
def test_roadmap_titles_reject_whitespace_only(title: str) -> None:
    with pytest.raises(ValidationError):
        RoadmapItemCreate(
            workspace_id="workspace",
            title=title,
            item_type="Task",
            created_by="tester",
        )
    with pytest.raises(ValidationError):
        RoadmapItemUpdate(
            workspace_id="workspace",
            expected_revision=1,
            title=title,
            actor="tester",
        )


@pytest.mark.parametrize("title", [" ", "\t\n"])
def test_calendar_titles_reject_whitespace_only(title: str) -> None:
    with pytest.raises(ValidationError):
        CalendarAllocationCreate(
            workspace_id="workspace",
            title=title,
            event_type="work session",
            start_local=datetime(2026, 9, 14, 9, 0),
            end_local=datetime(2026, 9, 14, 10, 0),
            timezone="Europe/Rome",
            created_by="tester",
        )
    with pytest.raises(ValidationError):
        CalendarAllocationUpdate(
            workspace_id="workspace",
            expected_revision=1,
            title=title,
            actor="tester",
        )
