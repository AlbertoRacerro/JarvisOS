"""HTTP routes for workspace memory search and document folder management (spec 300).

Provides:
- GET /workspaces/{workspace_id}/memory/search?q=
- GET /workspaces/{workspace_id}/memory/documents
- PUT /workspaces/{workspace_id}/memory/documents
- POST /workspaces/{workspace_id}/memory/documents/refresh
- DELETE /workspaces/{workspace_id}/memory/documents
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.core.errors import workspace_not_found_http_error
from app.core.paths import build_paths
from app.modules.ai import document_intake, retrieval_query
from app.modules.ai.retrieval_query import WORKSPACE_SCOPED_OWNERS
from app.modules.engineering.bench import _strip_paths
from app.modules.workspaces.service import get_workspace

router = APIRouter(prefix="/workspaces/{workspace_id}/memory", tags=["ai-memory"])


def _folder_invalid() -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "folder_invalid"})


def _repo_root() -> Path:
    """The checkout the backend runs from: `app` lives in `<root>/backend/app`."""
    import app

    return Path(app.__file__).resolve().parents[2]


def _overlaps(candidate: Path, protected: Path) -> bool:
    """True when either folder contains the other; compared by device and inode, not by spelling."""
    try:
        protected_stat = os.stat(protected)
    except OSError:
        return False
    candidate_stat = os.stat(candidate)
    for parent in (candidate, *candidate.parents):
        if os.path.samestat(os.stat(parent), protected_stat):
            return True
    resolved = protected.resolve()
    return any(os.path.samestat(os.stat(parent), candidate_stat) for parent in (resolved, *resolved.parents))


def _validate_and_register_folder(workspace_id: str, folder: object) -> str:
    if not isinstance(folder, str) or not folder.strip() or "\x00" in folder:
        raise _folder_invalid()
    raw_path = Path(folder)
    if not raw_path.is_absolute():
        raise _folder_invalid()
    try:
        if not stat.S_ISDIR(os.lstat(raw_path).st_mode):
            raise _folder_invalid()
        resolved_path = raw_path.resolve(strict=True)
        if resolved_path == Path(resolved_path.anchor) or not resolved_path.name:
            raise _folder_invalid()
        if _overlaps(resolved_path, build_paths().data_root) or _overlaps(resolved_path, _repo_root()):
            raise _folder_invalid()
        document_intake.register_folder(workspace_id, resolved_path)
    except (OSError, RuntimeError, ValueError):
        raise _folder_invalid() from None
    return resolved_path.name


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
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "query_invalid"}) from None

    items: list[dict[str, Any]] = []
    for entry in raw_result.get("evidence", []):
        if entry.get("validation") != "current":
            continue
        source_ref = entry.get("source_ref", {})
        authority_owner = source_ref.get("authority_owner", "")
        object_type = source_ref.get("object_type", "")
        excerpt = entry.get("excerpt", "")

        # Record excerpts open with their readable one-line summary: it becomes the title and leaves the excerpt.
        title = ""
        lines = excerpt.strip().splitlines()
        if lines and authority_owner != "workspace_documents":
            title = lines[0].strip()[:160]
            rest = "\n".join(lines[1:]).strip()
            if rest:
                excerpt = rest

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
            frag = (loc.get("fragment") or "") if isinstance(loc, dict) else ""
            if relative_path and frag.startswith(f"{relative_path}#"):
                heading = frag[len(relative_path) + 1:] or None
            item_dict["title"] = (heading or relative_path or "")[:160]
            if relative_path is not None:
                item_dict["relative_path"] = relative_path
            if heading is not None:
                item_dict["heading"] = heading

        # Record excerpts may carry server paths. A document's path fields are relative labels and stay intact.
        if authority_owner == "workspace_documents":
            item_dict["excerpt"] = _strip_paths(item_dict["excerpt"])
        else:
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
async def put_documents_folder(workspace_id: str, request: Request) -> dict[str, Any]:
    # The body is read by hand: a framework validation error would echo the submitted path.
    try:
        body = json.loads(await request.body())
    except (ValueError, RecursionError):
        raise _folder_invalid() from None
    folder = body.get("folder") if isinstance(body, dict) else None

    def register() -> str:
        if get_workspace(workspace_id) is None:
            raise workspace_not_found_http_error()
        return _validate_and_register_folder(workspace_id, folder)

    folder_name = await run_in_threadpool(register)
    return {"registered": True, "folder_name": folder_name}


@router.post("/documents/refresh")
def refresh_documents_folder(workspace_id: str) -> dict[str, Any]:
    if get_workspace(workspace_id) is None:
        raise workspace_not_found_http_error()
    if document_intake.registered_folder(workspace_id) is None:
        raise HTTPException(status_code=409, detail={"code": "folder_not_registered"})
    report = document_intake.refresh_folder(workspace_id)
    # Skip reasons carry folder-relative labels only.
    return report


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
