from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import WORKSPACE_NOT_FOUND_CODE, workspace_not_found_http_error
from app.modules.development.links import add_object_link, list_object_links, remove_object_link
from app.modules.development.models import (
    CalendarAllocationCreate,
    CalendarAllocationUpdate,
    RoadmapItemCreate,
    RoadmapItemUpdate,
)
from app.modules.development.service import (
    DevelopmentError,
    add_dependency,
    create_calendar_allocation,
    create_roadmap_item,
    delete_calendar_allocation,
    delete_roadmap_item,
    get_calendar_allocation,
    get_roadmap_item,
    list_calendar_allocations,
    list_dependencies,
    list_roadmap_items,
    remove_dependency,
    update_calendar_allocation,
    update_roadmap_item,
)

router = APIRouter(prefix="/development", tags=["development"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DependencyMutation(StrictBody):
    workspace_id: str = Field(min_length=1)
    depends_on_item_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)


class ObjectLinkMutation(StrictBody):
    workspace_id: str = Field(min_length=1)
    ref_type: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)


class DeleteMutation(StrictBody):
    workspace_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    actor: str = Field(min_length=1)


def _http_error(exc: DevelopmentError) -> HTTPException:
    if exc.code == WORKSPACE_NOT_FOUND_CODE:
        return workspace_not_found_http_error()
    missing = {
        "roadmap_item_not_found",
        "calendar_allocation_not_found",
        "roadmap_dependency_not_found",
        "roadmap_link_not_found",
        "roadmap_link_target_not_found",
    }
    conflict = {
        "roadmap_item_stale",
        "calendar_allocation_stale",
        "roadmap_dependency_self",
        "roadmap_dependency_cycle",
        "roadmap_dependency_exists",
        "roadmap_link_exists",
        "roadmap_done_gate_unsatisfied",
        "roadmap_item_delete_blocked",
    }
    status_code = 404 if exc.code in missing else 409 if exc.code in conflict else 400
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


@router.post("/roadmap/items", status_code=201)
def create_roadmap_item_endpoint(payload: RoadmapItemCreate) -> dict[str, object]:
    try:
        return create_roadmap_item(payload)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.get("/roadmap/items")
def list_roadmap_items_endpoint(workspace_id: str = Query(min_length=1)) -> list[dict[str, object]]:
    try:
        return list_roadmap_items(workspace_id)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.get("/roadmap/items/{item_id}")
def get_roadmap_item_endpoint(item_id: str, workspace_id: str = Query(min_length=1)) -> dict[str, object]:
    try:
        return get_roadmap_item(workspace_id, item_id)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.put("/roadmap/items/{item_id}")
def update_roadmap_item_endpoint(item_id: str, payload: RoadmapItemUpdate) -> dict[str, object]:
    try:
        return update_roadmap_item(item_id, payload)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.delete("/roadmap/items/{item_id}", status_code=204)
def delete_roadmap_item_endpoint(item_id: str, payload: DeleteMutation) -> Response:
    try:
        delete_roadmap_item(payload.workspace_id, item_id, payload.expected_revision, payload.actor)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.get("/roadmap/items/{item_id}/dependencies")
def list_dependencies_endpoint(
    item_id: str, workspace_id: str = Query(min_length=1)
) -> list[dict[str, str]]:
    try:
        return list_dependencies(workspace_id, item_id)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.post("/roadmap/items/{item_id}/dependencies", status_code=204)
def add_dependency_endpoint(item_id: str, payload: DependencyMutation) -> Response:
    try:
        add_dependency(payload.workspace_id, item_id, payload.depends_on_item_id, payload.actor)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.delete("/roadmap/items/{item_id}/dependencies/{depends_on_item_id}", status_code=204)
def remove_dependency_endpoint(
    item_id: str,
    depends_on_item_id: str,
    workspace_id: str = Query(min_length=1),
    actor: str = Query(min_length=1),
) -> Response:
    try:
        remove_dependency(workspace_id, item_id, depends_on_item_id, actor)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.get("/roadmap/items/{item_id}/links")
def list_object_links_endpoint(
    item_id: str, workspace_id: str = Query(min_length=1)
) -> list[dict[str, str]]:
    try:
        return list_object_links(workspace_id, item_id)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.post("/roadmap/items/{item_id}/links", status_code=201)
def add_object_link_endpoint(item_id: str, payload: ObjectLinkMutation) -> dict[str, str]:
    try:
        return add_object_link(
            payload.workspace_id,
            item_id,
            payload.ref_type,
            payload.ref_id,
            payload.actor,
        )
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.delete("/roadmap/items/{item_id}/links/{link_id}", status_code=204)
def remove_object_link_endpoint(
    item_id: str,
    link_id: str,
    workspace_id: str = Query(min_length=1),
    actor: str = Query(min_length=1),
) -> Response:
    try:
        remove_object_link(workspace_id, item_id, link_id, actor)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.post("/calendar/allocations", status_code=201)
def create_calendar_allocation_endpoint(payload: CalendarAllocationCreate) -> dict[str, object]:
    try:
        return create_calendar_allocation(payload)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.get("/calendar/allocations")
def list_calendar_allocations_endpoint(workspace_id: str = Query(min_length=1)) -> list[dict[str, object]]:
    try:
        return list_calendar_allocations(workspace_id)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.get("/calendar/allocations/{allocation_id}")
def get_calendar_allocation_endpoint(
    allocation_id: str, workspace_id: str = Query(min_length=1)
) -> dict[str, object]:
    try:
        return get_calendar_allocation(workspace_id, allocation_id)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.put("/calendar/allocations/{allocation_id}")
def update_calendar_allocation_endpoint(
    allocation_id: str, payload: CalendarAllocationUpdate
) -> dict[str, object]:
    try:
        return update_calendar_allocation(allocation_id, payload)
    except DevelopmentError as exc:
        raise _http_error(exc) from exc


@router.delete("/calendar/allocations/{allocation_id}", status_code=204)
def delete_calendar_allocation_endpoint(allocation_id: str, payload: DeleteMutation) -> Response:
    try:
        delete_calendar_allocation(
            payload.workspace_id, allocation_id, payload.expected_revision, payload.actor
        )
    except DevelopmentError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)
