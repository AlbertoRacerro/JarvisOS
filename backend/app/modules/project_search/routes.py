from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Query

from app.modules.project_search.models import ProjectSearchKind, ProjectSearchResponse
from app.modules.project_search.service import PROJECT_SEARCH_KINDS, search_project

router = APIRouter(tags=["project-search"])
_ALLOWED_KINDS = set(PROJECT_SEARCH_KINDS)
_KINDS_QUERY = Query()


def _parse_kinds(raw: list[str] | None) -> list[ProjectSearchKind] | None:
    if raw is None:
        return None
    values: list[str] = []
    for item in raw:
        values.extend(part.strip() for part in item.split(",") if part.strip())
    if not values:
        raise HTTPException(status_code=422, detail="kinds must contain at least one supported result kind")
    unknown = [value for value in values if value not in _ALLOWED_KINDS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unsupported project-search kind: {unknown[0]}")
    return [cast(ProjectSearchKind, value) for value in dict.fromkeys(values)]


@router.get("/workspaces/{workspace_id}/project-search", response_model=ProjectSearchResponse)
def project_search_endpoint(
    workspace_id: str,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kinds: Annotated[list[str] | None, _KINDS_QUERY] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ProjectSearchResponse:
    query = q.strip()
    if len(query) < 2 or len(query) > 200:
        raise HTTPException(status_code=422, detail="q must contain 2 to 200 non-whitespace characters")
    parsed_kinds = _parse_kinds(kinds)
    try:
        return search_project(workspace_id, query=query, kinds=parsed_kinds, limit=limit)
    except ValueError as exc:
        if str(exc) == "Workspace not found.":
            raise HTTPException(status_code=404, detail="Workspace not found.") from exc
        raise
