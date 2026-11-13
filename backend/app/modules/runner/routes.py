from fastapi import APIRouter, Depends, HTTPException

from app.core.database import open_sqlite_connection
from app.modules.project_knowledge.value_history import (
    RunInputsManifestRead,
    ValueHistoryError,
    get_run_inputs_manifest,
)
from app.modules.runner.guarded_service import (
    create_model_implementation,
    create_runner_job,
    get_simulation_run_detail,
    list_model_implementations,
    list_run_artifacts,
    list_run_logs,
    preview_model_bindings,
)
from app.modules.runner.models import (
    BindingPreviewRequest,
    BindingPreviewResponse,
    ModelImplementationRead,
    RunArtifactRead,
    RunLogRead,
    RunnerJobCreate,
    RunnerJobCreateResponse,
    RunnerJobRunResponse,
    SimulationRunDetail,
)
from app.modules.runner.origin import require_runner_origin
from app.modules.runner.public_models import PublicModelImplementationCreate
from app.modules.runner.resilient_service import run_runner_job
from app.modules.runner.safety import RunnerSafetyError

router = APIRouter(tags=["runner"])


def _runner_error(exc: RunnerSafetyError) -> HTTPException:
    not_found = {
        "runner_workspace_not_found",
        "runner_model_spec_not_found",
        "runner_model_version_not_found",
        "runner_job_not_found",
        "runner_simulation_run_not_found",
    }
    conflicts = {"runner_job_not_queued", "runner_request_key_conflict"}
    status_code = 404 if exc.code in not_found else 409 if exc.code in conflicts else 400
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


@router.post(
    "/workspaces/{workspace_id}/model-implementations",
    response_model=ModelImplementationRead,
    status_code=201,
    dependencies=[Depends(require_runner_origin)],
)
def create_model_implementation_endpoint(
    workspace_id: str,
    payload: PublicModelImplementationCreate,
) -> ModelImplementationRead:
    try:
        return create_model_implementation(workspace_id, payload)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/model-implementations",
    response_model=list[ModelImplementationRead],
)
def list_model_implementations_endpoint(workspace_id: str) -> list[ModelImplementationRead]:
    try:
        return list_model_implementations(workspace_id)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.post(
    "/workspaces/{workspace_id}/model-implementations/{model_version_id}/binding-preview",
    response_model=BindingPreviewResponse,
)
def preview_model_bindings_endpoint(
    workspace_id: str,
    model_version_id: str,
    payload: BindingPreviewRequest,
) -> BindingPreviewResponse:
    try:
        return preview_model_bindings(workspace_id, model_version_id, payload)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.post(
    "/workspaces/{workspace_id}/runner-jobs",
    response_model=RunnerJobCreateResponse,
    status_code=201,
    dependencies=[Depends(require_runner_origin)],
)
def create_runner_job_endpoint(
    workspace_id: str,
    payload: RunnerJobCreate,
) -> RunnerJobCreateResponse:
    try:
        return create_runner_job(workspace_id, payload)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.post(
    "/runner-jobs/{runner_job_id}/run",
    response_model=RunnerJobRunResponse,
    dependencies=[Depends(require_runner_origin)],
)
def run_runner_job_endpoint(runner_job_id: str) -> RunnerJobRunResponse:
    try:
        return run_runner_job(runner_job_id)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/simulation-runs/{simulation_run_id}",
    response_model=SimulationRunDetail,
)
def get_simulation_run_endpoint(
    workspace_id: str,
    simulation_run_id: str,
) -> SimulationRunDetail:
    try:
        return get_simulation_run_detail(workspace_id, simulation_run_id)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/simulation-runs/{simulation_run_id}/logs",
    response_model=list[RunLogRead],
)
def list_run_logs_endpoint(
    workspace_id: str,
    simulation_run_id: str,
) -> list[RunLogRead]:
    try:
        return list_run_logs(workspace_id, simulation_run_id)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/simulation-runs/{simulation_run_id}/artifacts",
    response_model=list[RunArtifactRead],
)
def list_run_artifacts_endpoint(
    workspace_id: str,
    simulation_run_id: str,
) -> list[RunArtifactRead]:
    try:
        return list_run_artifacts(workspace_id, simulation_run_id)
    except RunnerSafetyError as exc:
        raise _runner_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/simulation-runs/{simulation_run_id}/manifest",
    response_model=RunInputsManifestRead,
)
@router.get(
    "/workspaces/{workspace_id}/simulation-runs/{simulation_run_id}/values-used",
    response_model=RunInputsManifestRead,
)
def get_simulation_run_manifest_endpoint(
    workspace_id: str,
    simulation_run_id: str,
) -> RunInputsManifestRead:
    with open_sqlite_connection() as connection:
        try:
            return get_run_inputs_manifest(connection, workspace_id, simulation_run_id)
        except ValueHistoryError as exc:
            status_code = 404 if "not_found" in exc.code else 400
            raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc
