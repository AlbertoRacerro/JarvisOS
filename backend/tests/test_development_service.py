from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.development.models import (
    CalendarAllocationCreate,
    RoadmapItemCreate,
    RoadmapItemUpdate,
)
from app.modules.development.service import (
    DevelopmentError,
    add_dependency,
    create_calendar_allocation,
    create_roadmap_item,
    delete_roadmap_item,
    get_roadmap_item,
    list_calendar_allocations,
    list_dependencies,
    update_roadmap_item,
)
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def _initialize(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    from app.core.config import get_settings
    from app.core.database import initialize_database

    get_settings.cache_clear()
    initialize_database()


def _workspace(slug: str):
    return create_workspace(
        WorkspaceCreate(name=slug, slug=slug, description=None, status="active")
    )


def _item(workspace_id: str, title: str, *, done_when: str | None = None):
    return create_roadmap_item(
        RoadmapItemCreate(
            workspace_id=workspace_id,
            title=title,
            item_type="Task",
            window_start_date=date(2026, 9, 20),
            window_end_date=date(2026, 9, 30),
            done_when=done_when,
            created_by="tester",
        )
    )


def test_stale_update_and_done_gate_fail_without_partial_mutation(monkeypatch, tmp_path: Path) -> None:
    _initialize(monkeypatch, tmp_path)
    workspace = _workspace("development-a")
    item = _item(workspace.id, "Validate prototype", done_when="Evidence accepted")

    updated = update_roadmap_item(
        str(item["id"]),
        RoadmapItemUpdate(
            workspace_id=workspace.id,
            expected_revision=1,
            title="Validate prototype v2",
            actor="tester",
        ),
    )
    assert updated["revision"] == 2

    with pytest.raises(DevelopmentError) as stale:
        update_roadmap_item(
            str(item["id"]),
            RoadmapItemUpdate(
                workspace_id=workspace.id,
                expected_revision=1,
                notes="stale write",
                actor="tester",
            ),
        )
    assert stale.value.code == "roadmap_item_stale"

    with pytest.raises(DevelopmentError) as blocked:
        update_roadmap_item(
            str(item["id"]),
            RoadmapItemUpdate(
                workspace_id=workspace.id,
                expected_revision=2,
                status="Done",
                actor="tester",
            ),
        )
    assert blocked.value.code == "roadmap_done_gate_unsatisfied"
    current = get_roadmap_item(workspace.id, str(item["id"]))
    assert current["status"] == "Planned"
    assert current["revision"] == 2


def test_dependencies_reject_self_cycle_and_cross_workspace(monkeypatch, tmp_path: Path) -> None:
    _initialize(monkeypatch, tmp_path)
    left = _workspace("development-left")
    right = _workspace("development-right")
    a = _item(left.id, "A")
    b = _item(left.id, "B")
    foreign = _item(right.id, "Foreign")

    with pytest.raises(DevelopmentError) as self_error:
        add_dependency(left.id, str(a["id"]), str(a["id"]), "tester")
    assert self_error.value.code == "roadmap_dependency_self"

    add_dependency(left.id, str(a["id"]), str(b["id"]), "tester")
    assert [row["depends_on_item_id"] for row in list_dependencies(left.id, str(a["id"]))] == [str(b["id"])]
    with pytest.raises(DevelopmentError) as cycle:
        add_dependency(left.id, str(b["id"]), str(a["id"]), "tester")
    assert cycle.value.code == "roadmap_dependency_cycle"

    with pytest.raises(DevelopmentError) as cross_workspace:
        add_dependency(left.id, str(a["id"]), str(foreign["id"]), "tester")
    assert cross_workspace.value.code == "roadmap_item_not_found"


def test_concurrent_opposite_dependencies_cannot_commit_a_cycle(monkeypatch, tmp_path: Path) -> None:
    _initialize(monkeypatch, tmp_path)
    workspace = _workspace("development-concurrent")
    a = _item(workspace.id, "A")
    b = _item(workspace.id, "B")

    def add(left: str, right: str) -> str:
        try:
            add_dependency(workspace.id, left, right, "tester")
            return "ok"
        except DevelopmentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda edge: add(*edge), [(str(a["id"]), str(b["id"])), (str(b["id"]), str(a["id"]))]))

    assert sorted(results) == ["ok", "roadmap_dependency_cycle"]


def test_calendar_event_type_is_bounded() -> None:
    with pytest.raises(ValidationError):
        CalendarAllocationCreate(
            workspace_id="workspace",
            title="Unknown event",
            event_type="anything-goes",
            start_local=datetime(2026, 9, 24, 9, 0),
            end_local=datetime(2026, 9, 24, 10, 0),
            timezone="Europe/Rome",
            created_by="tester",
        )


def test_calendar_links_do_not_rewrite_roadmap_window_and_block_parent_delete(monkeypatch, tmp_path: Path) -> None:
    _initialize(monkeypatch, tmp_path)
    workspace = _workspace("development-calendar")
    item = _item(workspace.id, "Commissioning")

    for hour in (9, 14):
        create_calendar_allocation(
            CalendarAllocationCreate(
                workspace_id=workspace.id,
                roadmap_item_id=str(item["id"]),
                title=f"Commissioning block {hour}",
                event_type="work session",
                start_local=datetime(2026, 9, 24, hour, 0),
                end_local=datetime(2026, 9, 24, hour + 1, 0),
                timezone="Europe/Rome",
                created_by="tester",
            )
        )

    updated = update_roadmap_item(
        str(item["id"]),
        RoadmapItemUpdate(
            workspace_id=workspace.id,
            expected_revision=1,
            window_start_date=date(2026, 9, 21),
            window_end_date=date(2026, 10, 2),
            actor="tester",
        ),
    )
    allocations = list_calendar_allocations(workspace.id)
    assert len(allocations) == 2
    assert all(str(entry["start_instant"]).startswith("2026-09-24") for entry in allocations)
    assert updated["window_start_date"] == "2026-09-21"
    assert updated["window_end_date"] == "2026-10-02"

    with pytest.raises(DevelopmentError) as blocked:
        delete_roadmap_item(workspace.id, str(item["id"]), 2, "tester")
    assert blocked.value.code == "roadmap_item_delete_blocked"
