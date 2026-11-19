"""Workspace-scoped BLUECAD candidate API routes."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.bluecad.freecad_bridge import FreecadBridge, FreecadBridgeError, validate_request
from app.modules.bluecad.freecad_references import (
    MAX_FILE_BYTES,
    FreecadReferenceAdd,
    FreecadReferenceError,
    FreecadReferenceMove,
    add_freecad_reference,
    list_freecad_references,
    move_freecad_reference,
    reference_artifact,
    remove_freecad_reference,
    upload_freecad_file,
)
from app.modules.bluecad.ledger import archive_candidate, get_candidate, list_candidates, mark_promoted
from app.modules.bluecad.loop import _external_blocked_reason, create_bluecad_candidate
from app.modules.bluecad.models import BluecadCandidateCreate, BluecadCandidateRead
from app.modules.bluecad.print_handoff import (
    PrintHandoffCapability,
    PrintHandoffError,
    PrintHandoffEventRead,
    open_in_slicer,
)
from app.modules.bluecad.print_handoff import (
    capability as print_handoff_capability,
)
from app.modules.bluecad.read_model import BluecadCandidateAggregateRead, get_bluecad_candidate_aggregate
from app.modules.bluecad.studio_references import (
    ReferenceCreate,
    ReferenceError,
    ReferenceMove,
    add_reference,
    list_references,
    move_reference,
    reference_glb,
    remove_reference,
)
from app.modules.bluecad.template import BluecadTemplateCreate, TemplateError, create_template_candidate
from app.modules.events.service import log_event
from app.modules.modeling.models import DecisionCreate
from app.modules.modeling.service import create_decision

router = APIRouter(prefix="/workspaces/{workspace_id}/bluecad", tags=["bluecad"])


class FreecadEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: str
    object: str
    property: str
    value: Any


class FreecadApplyRequest(FreecadEditRequest):
    old_value: Any


def _freecad_error(exc: FreecadBridgeError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


def _require_workspace(workspace_id: str) -> None:
    with open_sqlite_connection() as connection:
        if connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail={"error": "Workspace not found."})


def _freecad_bridge() -> FreecadBridge:
    return FreecadBridge()


@router.get("/freecad/status")
def freecad_status_endpoint(workspace_id: str) -> dict[str, Any]:
    _require_workspace(workspace_id)
    bridge = _freecad_bridge()
    configuration = bridge.configuration()
    if not configuration["configured"]:
        return {"configured": False, "connected": False, "code": configuration["code"]}
    try:
        bridge.request("ping")
    except FreecadBridgeError as exc:
        return {"configured": True, "connected": False, "code": exc.code}
    return {"configured": True, "connected": True, "code": None}


@router.get("/freecad/documents")
def freecad_documents_endpoint(workspace_id: str) -> list[str]:
    _require_workspace(workspace_id)
    try:
        result = _freecad_bridge().request("list_documents")
    except FreecadBridgeError as exc:
        raise _freecad_error(exc) from exc
    if not isinstance(result, list) or not all(isinstance(item, str) for item in result):
        raise HTTPException(
            status_code=502,
            detail={"code": "FREECAD_BRIDGE_PROTOCOL_ERROR", "message": "Elenco documenti non valido."},
        )
    return result


@router.get("/freecad/documents/{doc}/objects")
def freecad_objects_endpoint(workspace_id: str, doc: str) -> list[dict[str, Any]]:
    _require_workspace(workspace_id)
    try:
        result = _freecad_bridge().request("get_objects", {"doc_name": doc})
    except FreecadBridgeError as exc:
        raise _freecad_error(exc) from exc
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise HTTPException(
            status_code=502,
            detail={"code": "FREECAD_BRIDGE_PROTOCOL_ERROR", "message": "Elenco oggetti non valido."},
        )
    return result


@router.post("/freecad/edit/preview")
def freecad_edit_preview_endpoint(workspace_id: str, payload: FreecadEditRequest) -> dict[str, Any]:
    _require_workspace(workspace_id)
    try:
        old_value = validate_request(
            _freecad_bridge(), payload.document, payload.object, payload.property, payload.value
        )
    except FreecadBridgeError as exc:
        raise _freecad_error(exc) from exc
    return {
        "document": payload.document, "object": payload.object, "property": payload.property,
        "old_value": old_value, "new_value": payload.value,
    }


@router.post("/freecad/edit/apply")
def freecad_edit_apply_endpoint(workspace_id: str, payload: FreecadApplyRequest) -> dict[str, Any]:
    _require_workspace(workspace_id)
    bridge = _freecad_bridge()
    try:
        live_old = validate_request(
            bridge, payload.document, payload.object, payload.property, payload.value
        )
        if live_old != payload.old_value:
            raise FreecadBridgeError("FREECAD_STALE_PREVIEW", "Il valore in FreeCAD è cambiato dopo l'anteprima: rifai l'anteprima.", 409)
        outcome = bridge.request(
            "edit_object",
            {
                "doc_name": payload.document,
                "obj_name": payload.object,
                "properties": {"Properties": {payload.property: payload.value}},
            },
        )
    except FreecadBridgeError as exc:
        raise _freecad_error(exc) from exc
    success = isinstance(outcome, dict) and outcome.get("success") is True
    # Only the typed outcome leaves the bridge: add-on error text can carry local file names.
    mutation = outcome.get("mutation_outcome") if isinstance(outcome, dict) else None
    outcome = {"success": success, "mutation_outcome": mutation if isinstance(mutation, str) else None}
    result: dict[str, Any] = {
        "document": payload.document, "object": payload.object, "property": payload.property,
        "old_value": payload.old_value, "new_value": payload.value, "outcome": outcome,
        "document_unchanged": not success,
    }
    if not success:
        result["message"] = "Il documento non è stato modificato."
    with open_sqlite_connection() as connection:
        log_event(
            connection,
            event_type="FreecadPropertyEditApplied",
            actor="local-user",
            target_type="FreeCADObject",
            target_id=payload.object,
            workspace_id=workspace_id,
            payload={
                "document": payload.document,
                "object": payload.object,
                "property": payload.property,
                "old": payload.old_value,
                "new": payload.value,
                "outcome": outcome,
            },
        )
        connection.commit()
    return result


def _reference_error(exc: ReferenceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


def _freecad_reference_error(exc: FreecadReferenceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


@router.post("/freecad/files", status_code=201)
async def upload_freecad_file_endpoint(workspace_id: str, request: Request) -> dict[str, Any]:
    _require_workspace(workspace_id)
    file_name = request.headers.get("x-filename", "")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FILE_BYTES:
        raise _freecad_reference_error(
            FreecadReferenceError("FREECAD_FILE_TOO_LARGE", "Il file supera il limite di 64 MiB.", 413)
        )
    body = await request.body()
    if len(body) > MAX_FILE_BYTES:
        raise _freecad_reference_error(
            FreecadReferenceError("FREECAD_FILE_TOO_LARGE", "Il file supera il limite di 64 MiB.", 413)
        )
    try:
        return upload_freecad_file(workspace_id, file_name, body)
    except FreecadReferenceError as exc:
        raise _freecad_reference_error(exc) from exc


@router.get("/candidates/{candidate_id}/freecad-references")
def list_freecad_references_endpoint(workspace_id: str, candidate_id: str) -> list[dict]:
    try:
        return list_freecad_references(workspace_id, candidate_id)
    except FreecadReferenceError as exc:
        raise _freecad_reference_error(exc) from exc


@router.post("/candidates/{candidate_id}/freecad-references", status_code=201)
def add_freecad_reference_endpoint(workspace_id: str, candidate_id: str, data: FreecadReferenceAdd) -> dict:
    try:
        return add_freecad_reference(workspace_id, candidate_id, data)
    except FreecadReferenceError as exc:
        raise _freecad_reference_error(exc) from exc


@router.put("/candidates/{candidate_id}/freecad-references/{reference_id}")
def move_freecad_reference_endpoint(workspace_id: str, candidate_id: str, reference_id: str,
                                    data: FreecadReferenceMove) -> dict:
    try:
        return move_freecad_reference(workspace_id, candidate_id, reference_id, data)
    except FreecadReferenceError as exc:
        raise _freecad_reference_error(exc) from exc


@router.delete("/candidates/{candidate_id}/freecad-references/{reference_id}", status_code=204, response_model=None)
def remove_freecad_reference_endpoint(workspace_id: str, candidate_id: str, reference_id: str,
                                      expected_revision: int = Query(ge=1)) -> None:
    try:
        remove_freecad_reference(workspace_id, candidate_id, reference_id, expected_revision)
    except FreecadReferenceError as exc:
        raise _freecad_reference_error(exc) from exc


def _freecad_artifact_response(workspace_id: str, candidate_id: str, reference_id: str,
                               artifact_name: str, media_type: str) -> Response:
    try:
        data = reference_artifact(workspace_id, candidate_id, reference_id, artifact_name)
    except FreecadReferenceError as exc:
        raise _freecad_reference_error(exc) from exc
    return Response(
        data,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{artifact_name}"',
        },
    )


@router.get("/candidates/{candidate_id}/freecad-references/{reference_id}/model.glb")
def freecad_reference_glb_endpoint(workspace_id: str, candidate_id: str, reference_id: str) -> Response:
    return _freecad_artifact_response(workspace_id, candidate_id, reference_id, "model.glb", "model/gltf-binary")


@router.get("/candidates/{candidate_id}/freecad-references/{reference_id}/model.step")
def freecad_reference_step_endpoint(workspace_id: str, candidate_id: str, reference_id: str) -> Response:
    return _freecad_artifact_response(workspace_id, candidate_id, reference_id, "model.step", "model/step")


@router.get("/candidates/{candidate_id}/freecad-references/{reference_id}/model.stl")
def freecad_reference_stl_endpoint(workspace_id: str, candidate_id: str, reference_id: str) -> Response:
    return _freecad_artifact_response(workspace_id, candidate_id, reference_id, "model.stl", "model/stl")


@router.get("/candidates/{candidate_id}/freecad-references/{reference_id}/model.3mf")
def freecad_reference_3mf_endpoint(workspace_id: str, candidate_id: str, reference_id: str) -> Response:
    return _freecad_artifact_response(workspace_id, candidate_id, reference_id, "model.3mf", "model/3mf")


@router.get("/candidates/{candidate_id}/studio-references")
def list_studio_references_endpoint(workspace_id: str, candidate_id: str) -> list[dict]:
    try:
        return list_references(workspace_id, candidate_id)
    except ReferenceError as exc:
        raise _reference_error(exc) from exc


@router.post("/candidates/{candidate_id}/studio-references", status_code=201)
def add_studio_reference_endpoint(workspace_id: str, candidate_id: str, data: ReferenceCreate) -> dict:
    try:
        return add_reference(workspace_id, candidate_id, data)
    except ReferenceError as exc:
        raise _reference_error(exc) from exc


@router.put("/candidates/{candidate_id}/studio-references/{reference_id}")
def move_studio_reference_endpoint(workspace_id: str, candidate_id: str, reference_id: str,
                                   data: ReferenceMove) -> dict:
    try:
        return move_reference(workspace_id, candidate_id, reference_id, data)
    except ReferenceError as exc:
        raise _reference_error(exc) from exc


@router.get("/candidates/{candidate_id}/studio-references/{reference_id}/model.glb")
def studio_reference_glb_endpoint(workspace_id: str, candidate_id: str, reference_id: str) -> Response:
    try:
        data = reference_glb(workspace_id, candidate_id, reference_id)
    except ReferenceError as exc:
        raise _reference_error(exc) from exc
    # Untrusted geometry bytes: fixed type, never sniffed (as the Studio artifact route).
    return Response(data, media_type="model/gltf-binary",
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                             "Content-Disposition": 'inline; filename="model.glb"'})


@router.delete("/candidates/{candidate_id}/studio-references/{reference_id}", status_code=204, response_model=None)
def remove_studio_reference_endpoint(workspace_id: str, candidate_id: str, reference_id: str,
                                     expected_revision: int = Query(ge=1)) -> None:
    try:
        remove_reference(workspace_id, candidate_id, reference_id, expected_revision)
    except ReferenceError as exc:
        raise _reference_error(exc) from exc


_EXPORT_MEDIA_TYPES = {
    "bluecad_stl": ("model/stl", ".stl"),
    "bluecad_step": ("model/step", ".step"),
    "bluecad_3mf": ("model/3mf", ".3mf"),
}
_CANDIDATE_SOURCE_REF = re.compile(r"^bluecad_candidate:([0-9a-f-]{36}):attempt:(\d+)$")


def _bluecad_artifact_path(workspace_id: str, artifact_id: str) -> tuple[Path, str, str]:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT stored_path, artifact_type, mime_type, source_ref
            FROM artifacts
            WHERE id = ? AND workspace_id = ?
            """,
            (artifact_id, workspace_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "BLUECAD artifact not found."})

    artifact_type = str(row["artifact_type"] or "")
    if not artifact_type.startswith("bluecad_"):
        raise HTTPException(status_code=404, detail={"error": "BLUECAD artifact not found."})

    try:
        stored_path = Path(str(row["stored_path"])).resolve()
        data_root = build_paths().data_root.resolve()
        stored_path.relative_to(data_root)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail={"error": "BLUECAD artifact not found."}) from None

    if not stored_path.exists() or not stored_path.is_file():
        raise HTTPException(status_code=404, detail={"error": "BLUECAD artifact not found."})

    media_type = str(row["mime_type"] or "application/octet-stream")
    download_name = stored_path.name
    if artifact_type == "bluecad_glb":
        media_type = "model/gltf-binary"
    elif artifact_type in _EXPORT_MEDIA_TYPES:
        media_type, suffix = _EXPORT_MEDIA_TYPES[artifact_type]
        match = _CANDIDATE_SOURCE_REF.match(str(row["source_ref"] or ""))
        if match:
            download_name = f"bluecad-{match.group(1)[:8]}-attempt{match.group(2)}{suffix}"
    return stored_path, media_type, download_name


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail={"error": str(exc)})
    if isinstance(exc, sqlite3.IntegrityError):
        return HTTPException(status_code=400, detail={"error": "Related record was not found."})
    return HTTPException(status_code=500, detail={"error": "Unexpected BLUECAD persistence error."})


@router.post("/candidates", response_model=BluecadCandidateRead, status_code=201)
def create_candidate_endpoint(workspace_id: str, payload: BluecadCandidateCreate) -> BluecadCandidateRead:
    try:
        return create_bluecad_candidate(workspace_id, payload)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.post("/candidates/from-template", response_model=BluecadCandidateRead, status_code=201)
def create_template_candidate_endpoint(workspace_id: str, payload: BluecadTemplateCreate) -> BluecadCandidateRead:
    try:
        return create_template_candidate(workspace_id, payload)
    except TemplateError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/generation-availability")
def generation_availability_endpoint(workspace_id: str) -> dict[str, object]:
    """Report whether the AI loop's first external tier may run; makes no AI call."""
    with open_sqlite_connection() as connection:
        workspace_exists = connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if workspace_exists is None:
        raise HTTPException(status_code=404, detail={"error": "Workspace not found."})
    blocked_reason = _external_blocked_reason()
    return {
        "route_class": "external:cheap",
        "external_calls_allowed": blocked_reason is None,
        "blocking_reason": blocked_reason,
    }


@router.get("/candidates", response_model=list[BluecadCandidateRead])
def list_candidates_endpoint(workspace_id: str) -> list[BluecadCandidateRead]:
    try:
        return list_candidates(workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc


@router.get("/candidates/{candidate_id}/aggregate", response_model=BluecadCandidateAggregateRead)
def get_candidate_aggregate_endpoint(workspace_id: str, candidate_id: str) -> BluecadCandidateAggregateRead:
    candidate = get_bluecad_candidate_aggregate(workspace_id, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail={"error": "BLUECAD candidate not found."})
    return candidate


@router.get("/candidates/{candidate_id}", response_model=BluecadCandidateRead)
def get_candidate_endpoint(workspace_id: str, candidate_id: str) -> BluecadCandidateRead:
    candidate = get_candidate(workspace_id, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail={"error": "BLUECAD candidate not found."})
    return candidate


@router.post("/candidates/{candidate_id}/promote", response_model=BluecadCandidateRead)
def promote_candidate_endpoint(workspace_id: str, candidate_id: str) -> BluecadCandidateRead:
    candidate = get_candidate(workspace_id, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail={"error": "BLUECAD candidate not found."})
    if candidate.status != "valid":
        raise HTTPException(
            status_code=409,
            detail={"error": "Only valid BLUECAD candidates may be promoted.", "status": candidate.status},
        )
    decision = create_decision(
        workspace_id,
        DecisionCreate(
            title=f"Promote BLUECAD candidate {candidate.id}",
            decision_text="Promote validated BLUECAD GeometrySpec candidate.",
            rationale=f"BLUECAD candidate {candidate.id}; spec_artifact_id={candidate.spec_artifact_id}; report_artifact_id={candidate.report_artifact_id}; glb_artifact_id={candidate.glb_artifact_id}",
            notes="Created by human-triggered BLUECAD promote endpoint.",
        ),
    )
    return mark_promoted(workspace_id, candidate_id, decision.id)


@router.post("/candidates/{candidate_id}/archive", response_model=BluecadCandidateRead)
def archive_candidate_endpoint(workspace_id: str, candidate_id: str) -> BluecadCandidateRead:
    try:
        candidate = archive_candidate(workspace_id, candidate_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": str(exc)},
        ) from exc
    if candidate is None:
        raise HTTPException(status_code=404, detail={"error": "BLUECAD candidate not found."})
    return candidate


@router.get("/artifacts/{artifact_id}/content")
def get_bluecad_artifact_content(workspace_id: str, artifact_id: str) -> FileResponse:
    stored_path, media_type, download_name = _bluecad_artifact_path(workspace_id, artifact_id)
    return FileResponse(stored_path, media_type=media_type, filename=download_name)


@router.get("/print-handoff/capability", response_model=PrintHandoffCapability)
def get_print_handoff_capability(workspace_id: str) -> PrintHandoffCapability:
    return print_handoff_capability()


@router.post("/artifacts/{artifact_id}/open-in-slicer", response_model=PrintHandoffEventRead)
def open_bluecad_artifact_in_slicer(workspace_id: str, artifact_id: str) -> PrintHandoffEventRead:
    try:
        return open_in_slicer(workspace_id, artifact_id)
    except PrintHandoffError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
