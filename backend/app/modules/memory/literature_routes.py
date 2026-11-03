from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.modules.memory.literature_models import (
    LiteratureEntryCreate,
    LiteratureEntryRead,
    LiteratureSourceCreate,
    LiteratureSourcePage,
    LiteratureSourceRead,
)
from app.modules.memory.literature_service import (
    LiteratureError,
    create_literature_entry,
    create_literature_source,
    get_literature_source,
    list_literature_sources,
    resolve_literature_content,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/literature", tags=["literature"])


def _literature_error(exc: LiteratureError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/sources", response_model=LiteratureSourcePage)
def list_literature_sources_endpoint(
    workspace_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> LiteratureSourcePage:
    try:
        return list_literature_sources(workspace_id, offset=offset, limit=limit)
    except LiteratureError as exc:
        raise _literature_error(exc) from exc


@router.post("/sources", response_model=LiteratureSourceRead, status_code=201)
def create_literature_source_endpoint(
    workspace_id: str,
    payload: LiteratureSourceCreate,
) -> LiteratureSourceRead:
    try:
        return create_literature_source(workspace_id, payload)
    except LiteratureError as exc:
        raise _literature_error(exc) from exc


@router.get("/sources/{source_id}", response_model=LiteratureSourceRead)
def get_literature_source_endpoint(workspace_id: str, source_id: str) -> LiteratureSourceRead:
    try:
        return get_literature_source(workspace_id, source_id)
    except LiteratureError as exc:
        raise _literature_error(exc) from exc


@router.post("/sources/{source_id}/entries", response_model=LiteratureEntryRead, status_code=201)
def create_literature_entry_endpoint(
    workspace_id: str,
    source_id: str,
    payload: LiteratureEntryCreate,
) -> LiteratureEntryRead:
    try:
        return create_literature_entry(workspace_id, source_id, payload)
    except LiteratureError as exc:
        raise _literature_error(exc) from exc


@router.get("/sources/{source_id}/content")
def get_literature_source_content_endpoint(workspace_id: str, source_id: str) -> FileResponse:
    try:
        content = resolve_literature_content(workspace_id, source_id)
    except LiteratureError as exc:
        raise _literature_error(exc) from exc
    return FileResponse(
        content.path,
        media_type=content.media_type,
        filename=content.filename,
        content_disposition_type="inline",
    )
