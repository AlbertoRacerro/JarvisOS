"""Operator-facing Studio authoring routes."""

import hashlib
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.database import open_sqlite_connection
from app.modules.runner.origin import require_runner_origin
from app.modules.runner.safety import run_root
from app.modules.studio import demonstrator, procedure_service, service, workshop_draft
from app.modules.studio.derive import derive_documentation
from app.modules.studio.geometry import MEDIA_TYPES, GeometryCreate, GeometryTestRequest
from app.modules.studio.models import StudioCreate, StudioError, StudioUpdate, TestRequest
from app.modules.studio.procedure import NOTICE, ProcedureCreate, ProcedureTestRequest
from app.modules.studio.unit_models import StudioUnitCreate, UnitTestRequest

router = APIRouter(tags=["studio"])


class DocumentationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(max_length=65536)
    entry: Literal["evaluate", "build"] = "evaluate"

_CATEGORIES = [
    {"id": "calculation", "label": "Calculation", "available": True, "reason": None},
    {"id": "process_unit", "label": "Process unit", "available": True, "reason": None},
    {"id": "dynamic_model", "label": "Dynamic model", "available": False, "reason": "Time integration is not available in this slice."},
    {"id": "procedure", "label": "Procedure", "available": True, "reason": None, "notice": NOTICE},
    {"id": "geometry", "label": "Geometry", "available": True, "reason": None},
    {"id": "optimization", "label": "Optimization", "available": False, "reason": "Optimization orchestration is not available."},
    {"id": "cfd_fem", "label": "CFD/FEM", "available": False, "reason": "A governed solver integration is not available."},
]


_CONFLICTS = {
    "studio_publish_report_required", "studio_no_published_version", "studio_artifact_hash_mismatch",
    "studio_draft_open", "studio_revision_required", "studio_procedure_link_modified",
    "studio_procedure_link_missing", "studio_procedure_schedule_exists", "studio_procedure_scenario_unlinked",
    "studio_procedure_scenario_schedule_conflict", "studio_procedure_scenario_exists", "revision_conflict",
}


def _call(function, *args):
    try:
        return function(*args)
    except StudioError as exc:
        code = exc.status or (404 if exc.code.endswith("not_found") else 409 if exc.code in _CONFLICTS else 422)
        detail = {"code": exc.code, "message": exc.message, **({"detail": exc.detail} if exc.detail else {})}
        raise HTTPException(status_code=code, detail=detail) from exc


@router.get("/studio/categories")
def categories() -> list[dict]:
    return _CATEGORIES


@router.post("/studio/example-workspace", dependencies=[Depends(require_runner_origin)])
def example_workspace() -> dict:
    return _call(demonstrator.ensure_example)


@router.post("/studio/documentation/preview")
def preview_documentation(data: DocumentationPreview) -> dict:
    """Parse unsaved source for the editor; no execution or persistence."""
    return derive_documentation(data.source, entry=data.entry)


@router.post("/workspaces/{workspace_id}/studio/workshop-draft")
def workshop_draft_proposal(workspace_id: str, data: workshop_draft.WorkshopDraftRequest) -> dict:
    """Proposal-only draft from the answers so far; stateless, never persisted, never executed."""
    return _call(workshop_draft.draft, workspace_id, data)


@router.post("/workspaces/{workspace_id}/studio/models", status_code=201)
def create_model(workspace_id: str, data: StudioUnitCreate | GeometryCreate | ProcedureCreate | StudioCreate) -> dict:
    return _call(service.create, workspace_id, data)


@router.get("/workspaces/{workspace_id}/studio/models")
def list_models(workspace_id: str) -> list[dict]:
    return _call(service.list_models, workspace_id)


@router.get("/workspaces/{workspace_id}/studio/models/{model_spec_id}")
def get_model(workspace_id: str, model_spec_id: str) -> dict:
    return _call(service.get, workspace_id, model_spec_id)


@router.put("/workspaces/{workspace_id}/studio/models/{model_spec_id}")
def save_model(workspace_id: str, model_spec_id: str, data: StudioUpdate) -> dict:
    return _call(service.update, workspace_id, model_spec_id, data)


@router.post("/workspaces/{workspace_id}/studio/models/{model_spec_id}/test", dependencies=[Depends(require_runner_origin)])
def test_model(workspace_id: str, model_spec_id: str,
               data: UnitTestRequest | GeometryTestRequest | ProcedureTestRequest | TestRequest) -> dict:
    return _call(service.test, workspace_id, model_spec_id, data)


@router.post("/workspaces/{workspace_id}/studio/models/{model_spec_id}/publish", dependencies=[Depends(require_runner_origin)])
def publish_model(workspace_id: str, model_spec_id: str) -> dict:
    return _call(service.publish, workspace_id, model_spec_id)


@router.post("/workspaces/{workspace_id}/studio/models/{model_spec_id}/revise")
def revise_model(workspace_id: str, model_spec_id: str) -> dict:
    return _call(service.revise, workspace_id, model_spec_id)


@router.get("/workspaces/{workspace_id}/studio/library")
def library(workspace_id: str) -> list[dict]:
    return _call(service.library, workspace_id)


def _untrusted_file(path: Path, name: str) -> FileResponse:
    # Untrusted geometry bytes: fixed type, never sniffed; STEP and STL are downloads only.
    disposition = "inline" if name == "model.glb" else "attachment"
    return FileResponse(path, media_type=MEDIA_TYPES[name], filename=name, content_disposition_type=disposition,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.get("/workspaces/{workspace_id}/studio/models/{model_spec_id}/previews/{preview_id}/{name}")
def geometry_preview(workspace_id: str, model_spec_id: str, preview_id: str, name: str) -> FileResponse:
    return _untrusted_file(_call(service.geometry_preview, workspace_id, model_spec_id, preview_id, name), name)


@router.get("/workspaces/{workspace_id}/studio/geometry-runs/{simulation_run_id}/{name}")
def geometry_run_artifact(workspace_id: str, simulation_run_id: str, name: str) -> FileResponse:
    """A registered artifact of a geometry generation, re-verified against its recorded hash."""
    with open_sqlite_connection() as connection:
        row = connection.execute("""SELECT stored_path, sha256 FROM artifacts WHERE workspace_id = ? AND source_ref = ?
            AND filename = ? AND artifact_type = 'studio_geometry'""",
                                 (workspace_id, f"simulation_run:{simulation_run_id}", name)).fetchone()
    path = Path(row["stored_path"]) if row else None
    if (path is None or name not in MEDIA_TYPES or path != run_root(workspace_id, simulation_run_id) / "geometry" / name
            or path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]):
        raise HTTPException(status_code=404, detail={"code": "studio_geometry_artifact_not_found",
                                                     "message": "Geometry artifact not found or invalid."})
    return _untrusted_file(path, name)

@router.get("/workspaces/{workspace_id}/studio/procedure/bench/drafts")
def bench_drafts(workspace_id: str) -> list[dict]:
    return _call(procedure_service.bench_drafts, workspace_id)


@router.post("/workspaces/{workspace_id}/studio/procedure/bench/drafts/{draft_id}/scenarios", status_code=201)
def bench_create_scenario(workspace_id: str, draft_id: str, data: procedure_service.ScenarioRequest) -> dict:
    return _call(procedure_service.create_scenario, workspace_id, draft_id, data)


@router.post("/workspaces/{workspace_id}/studio/procedure/apply", dependencies=[Depends(require_runner_origin)])
def apply_procedure(workspace_id: str, data: procedure_service.ApplyRequest) -> dict:
    return _call(procedure_service.apply, workspace_id, data)


@router.get("/workspaces/{workspace_id}/studio/procedure/links")
def procedure_links(workspace_id: str, draft_id: str | None = None) -> list[dict]:
    return _call(procedure_service.list_links, workspace_id, draft_id)


@router.get("/workspaces/{workspace_id}/studio/procedure/links/{link_id}")
def procedure_link(workspace_id: str, link_id: str) -> dict:
    return _call(procedure_service.get_link, workspace_id, link_id)


@router.post("/workspaces/{workspace_id}/studio/procedure/links/{link_id}/update",
             dependencies=[Depends(require_runner_origin)])
def update_procedure_link(workspace_id: str, link_id: str, data: procedure_service.UpdateRequest) -> dict:
    return _call(procedure_service.update, workspace_id, link_id, data)


@router.post("/workspaces/{workspace_id}/studio/procedure/links/{link_id}/run", status_code=202,
             dependencies=[Depends(require_runner_origin)])
def run_procedure_link(workspace_id: str, link_id: str) -> dict:
    return _call(procedure_service.start_run, workspace_id, link_id)


@router.get("/workspaces/{workspace_id}/studio/procedure/drafts/{draft_id}/runs")
def procedure_runs(workspace_id: str, draft_id: str) -> list[dict]:
    return _call(procedure_service.list_runs, workspace_id, draft_id)


@router.get("/workspaces/{workspace_id}/studio/procedure/drafts/{draft_id}/runs/{job_id}")
def procedure_run(workspace_id: str, draft_id: str, job_id: str) -> dict:
    return _call(procedure_service.get_run, workspace_id, draft_id, job_id)


@router.get("/workspaces/{workspace_id}/studio/procedure/drafts/{draft_id}/runs/{job_id}/results")
def procedure_run_results(workspace_id: str, draft_id: str, job_id: str) -> dict:
    return _call(procedure_service.run_results, workspace_id, draft_id, job_id)
