"""HTTP routes for workspace memory search and document folder management (spec 300).

Provides:
- GET /workspaces/{workspace_id}/memory/search?q=
- GET /workspaces/{workspace_id}/memory/documents
- PUT /workspaces/{workspace_id}/memory/documents
- POST /workspaces/{workspace_id}/memory/documents/refresh
- DELETE /workspaces/{workspace_id}/memory/documents
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core.errors import workspace_not_found_http_error
from app.core.paths import build_paths
from app.modules.ai import document_intake, retrieval_query
from app.modules.ai.retrieval_query import WORKSPACE_SCOPED_OWNERS
from app.modules.engineering.bench import _strip_paths
from app.modules.workspaces.service import get_workspace

router = APIRouter(prefix="/workspaces/{workspace_id}/memory", tags=["ai-memory"])


class RegisterFolderPayload(BaseModel):
    folder: str


def _repo_root() -> Path:
    import app

    app_file = Path(app.__file__).resolve()
    for parent in app_file.parents:
        if (parent / ".git").exists():
            return parent
    return app_file.parents[1]


def _validate_and_register_folder(workspace_id: str, folder_str: str) -> str:
    if not isinstance(folder_str, str) or not folder_str.strip():
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    raw_path = Path(folder_str)
    if not raw_path.is_absolute():
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    # Check existence
    if not raw_path.exists():
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    # Check symlink
    if raw_path.is_symlink():
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    # Check file (must be directory)
    if not raw_path.is_dir():
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    try:
        resolved_path = raw_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"}) from None

    if resolved_path.is_symlink() or not resolved_path.is_dir():
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    # Check data root
    try:
        data_root = build_paths().data_root.resolve(strict=True)
    except (OSError, RuntimeError):
        data_root = build_paths().data_root.resolve()

    if resolved_path == data_root or data_root in resolved_path.parents:
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    # Check repository root
    try:
        repo_root = _repo_root().resolve(strict=True)
    except (OSError, RuntimeError):
        repo_root = _repo_root().resolve()

    if resolved_path == repo_root or repo_root in resolved_path.parents:
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"})

    try:
        document_intake.register_folder(workspace_id, raw_path)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "folder_invalid"}) from None

    return raw_path.name


@router.get("/search")
def search_memory(
    workspace_id: str,
    q: str = Query(..., min_length=1),
) -> list[dict[str, Any]]:
    if get_workspace(workspace_id) is None:
        raise workspace_not_found_http_error()

    if not q or not q.strip() or len(q) > 2000:
        raise HTTPException(
            status_code=422,
            detail="query must be nonempty and at most 2000 characters",
        )

    try:
        raw_result = retrieval_query.query_context(
            q,
            workspace_id=workspace_id,
            source_scope=tuple(sorted(WORKSPACE_SCOPED_OWNERS)),
            limit=12,
            token_budget=2048,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    items: list[dict[str, Any]] = []
    for entry in raw_result.get("evidence", []):
        if entry.get("validation") != "current":
            continue
        source_ref = entry.get("source_ref", {})
        authority_owner = source_ref.get("authority_owner", "")
        object_type = source_ref.get("object_type", "")
        excerpt = entry.get("excerpt", "")

        # Title: first non-empty line of excerpt, trimmed to 160 chars
        title = ""
        for line in excerpt.splitlines():
            line_clean = line.strip()
            if line_clean:
                title = line_clean[:160]
                break

        # Location: line range / fragment
        loc = source_ref.get("location")
        loc_str = None
        if isinstance(loc, dict):
            frag = loc.get("fragment")
            start = loc.get("start_line")
            end = loc.get("end_line")
            if frag:
                loc_str = frag
            elif start is not None and end is not None and start != end:
                loc_str = f"L{start}-L{end}"
            elif start is not None:
                loc_str = f"L{start}"
        elif isinstance(loc, str):
            loc_str = loc

        item_dict: dict[str, Any] = {
            "owner": authority_owner,
            "authority_owner": authority_owner,
            "object_type": object_type,
            "title": title,
            "location": loc_str,
            "excerpt": excerpt,
        }

        # For workspace_documents, also give relative path and heading if present
        if authority_owner == "workspace_documents":
            relative_path = None
            heading = None
            obj_id = source_ref.get("object_id") or ""
            match = re.fullmatch(r"(.+)::hsha256:[0-9a-f]{64}::d\d+::c\d+", obj_id)
            if match:
                relative_path = match.group(1)
            if isinstance(loc, dict):
                frag = loc.get("fragment") or ""
                if "#" in frag:
                    p_part, h_part = frag.split("#", 1)
                    if not relative_path and p_part:
                        relative_path = p_part
                    if h_part:
                        heading = h_part
                elif frag and not heading:
                    heading = frag
            if relative_path is not None:
                item_dict["relative_path"] = relative_path
            if heading is not None:
                item_dict["heading"] = heading

        # Do not return digests, object ids for other owners, or anything containing an absolute path
        item_dict = _strip_paths(item_dict)
        items.append(item_dict)

    return items


@router.get("/documents")
def get_documents_folder(workspace_id: str) -> dict[str, Any]:
    if get_workspace(workspace_id) is None:
        raise workspace_not_found_http_error()
    folder = document_intake.registered_folder(workspace_id)
    return {
        "registered": folder is not None,
        "folder_name": folder.name if folder is not None else None,
    }


@router.put("/documents")
def put_documents_folder(
    workspace_id: str,
    payload: RegisterFolderPayload,
) -> dict[str, Any]:
    if get_workspace(workspace_id) is None:
        raise workspace_not_found_http_error()
    folder_name = _validate_and_register_folder(workspace_id, payload.folder)
    return {
        "registered": True,
        "folder_name": folder_name,
    }


@router.post("/documents/refresh")
def refresh_documents_folder(workspace_id: str) -> dict[str, Any]:
    if get_workspace(workspace_id) is None:
        raise workspace_not_found_http_error()
    if document_intake.registered_folder(workspace_id) is None:
        raise HTTPException(status_code=409, detail={"code": "folder_not_registered"})
    report = document_intake.refresh_folder(workspace_id)
    return _strip_paths(report)


@router.delete("/documents")
def remove_documents_folder(workspace_id: str) -> dict[str, Any]:
    if get_workspace(workspace_id) is None:
        raise workspace_not_found_http_error()
    document_intake.remove_folder(workspace_id)
    document_intake.refresh_folder(workspace_id)
    return {
        "registered": False,
        "folder_name": None,
    }
