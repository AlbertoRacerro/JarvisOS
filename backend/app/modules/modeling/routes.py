import sqlite3

from fastapi import APIRouter, HTTPException, Query

from app.core.database import open_sqlite_connection
from app.core.errors import WORKSPACE_NOT_FOUND_MESSAGE, workspace_not_found_http_error
from app.modules.modeling.dossier_models import ModelDossierDetail, ModelDossierIndexItem
from app.modules.modeling.model_dossier import get_model_dossier, list_model_dossier_index
from app.modules.modeling.models import (
    AssumptionCreate,
    AssumptionRead,
    DecisionCreate,
    DecisionRead,
    ModelSpecCreate,
    ModelSpecRead,
    ParameterCreate,
    ParameterLifecycleCommand,
    ParameterRead,
    ParameterUpdate,
    RequirementCreate,
    RequirementProjectUpdate,
    RequirementRead,
    SimulationRunCreate,
    SimulationRunRead,
)
from app.modules.modeling.parameter_lifecycle import (
    ParameterLifecycleError,
    list_lifecycle_parameters,
    transition_parameter,
    update_parameter,
)
from app.modules.modeling.project_knowledge_owner import ProjectKnowledgeOwnerError
from app.modules.modeling.project_knowledge_owner import (
    update_requirement as update_requirement_canonical,
)
from app.modules.modeling.service import (
    create_assumption,
    create_decision,
    create_model_spec,
    create_parameter,
    create_requirement,
    create_simulation_run,
    get_model_spec,
    get_requirement,
    list_assumptions,
    list_decisions,
    list_model_specs,
    list_requirements,
    list_simulation_runs,
)
from app.modules.project_knowledge.value_history import (
    ValueComparisonRead,
    ValueHistoryError,
    ValueVersionChainRead,
    compare_value_versions,
    get_parameter_version_chain,
    get_sim_result_value_version_chain,
)

router = APIRouter(tags=["modeling"])


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ParameterLifecycleError):
        status = 409 if exc.code in {
            "parameter_stale",
            "parameter_lifecycle_dependents_require_reconciliation",
            "parameter_lifecycle_transition_invalid",
            "parameter_not_active",
        } else 404 if exc.code == "parameter_not_found" else 400
        return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})
    if isinstance(exc, ProjectKnowledgeOwnerError):
        status = 409 if exc.code == "owner_stale" else 404 if exc.code == "owner_not_found" else 422
        return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})
    if isinstance(exc, ValueError) and str(exc) == WORKSPACE_NOT_FOUND_MESSAGE:
        return workspace_not_found_http_error()
    if isinstance(exc, ValueError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, sqlite3.IntegrityError):
        return HTTPException(status_code=400, detail="Related record was not found.")
    return HTTPException(status_code=500, detail="Unexpected persistence error.")


@router.post("/workspaces/{workspace_id}/model-specs", response_model=ModelSpecRead, status_code=201)
def create_model_spec_endpoint(workspace_id: str, payload: ModelSpecCreate) -> ModelSpecRead:
    try:
        return create_model_spec(workspace_id, payload)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get("/workspaces/{workspace_id}/model-specs", response_model=list[ModelSpecRead])
def list_model_specs_endpoint(workspace_id: str) -> list[ModelSpecRead]:
    try:
        return list_model_specs(workspace_id)
    except ValueError as exc:
        raise _domain_error(exc) from exc


@router.get("/model-specs/{model_spec_id}", response_model=ModelSpecRead)
def get_model_spec_endpoint(model_spec_id: str) -> ModelSpecRead:
    model_spec = get_model_spec(model_spec_id)
    if model_spec is None:
        raise HTTPException(status_code=404, detail="Model spec not found.")
    return model_spec


@router.get("/workspaces/{workspace_id}/model-dossiers", response_model=list[ModelDossierIndexItem])
def list_model_dossiers_endpoint(workspace_id: str) -> list[ModelDossierIndexItem]:
    try:
        return list_model_dossier_index(workspace_id)
    except ValueError as exc:
        raise _domain_error(exc) from exc


@router.get("/workspaces/{workspace_id}/model-dossiers/{model_version_id}", response_model=ModelDossierDetail)
def get_model_dossier_endpoint(workspace_id: str, model_version_id: str) -> ModelDossierDetail:
    dossier = get_model_dossier(workspace_id, model_version_id)
    if dossier is None:
        raise HTTPException(status_code=404, detail="Model version dossier not found.")
    return dossier


@router.post("/workspaces/{workspace_id}/assumptions", response_model=AssumptionRead, status_code=201)
def create_assumption_endpoint(workspace_id: str, payload: AssumptionCreate) -> AssumptionRead:
    try:
        return create_assumption(workspace_id, payload)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get("/workspaces/{workspace_id}/assumptions", response_model=list[AssumptionRead])
def list_assumptions_endpoint(workspace_id: str) -> list[AssumptionRead]:
    try:
        return list_assumptions(workspace_id)
    except ValueError as exc:
        raise _domain_error(exc) from exc


@router.post("/workspaces/{workspace_id}/parameters", response_model=ParameterRead, status_code=201)
def create_parameter_endpoint(workspace_id: str, payload: ParameterCreate) -> ParameterRead:
    try:
        return create_parameter(workspace_id, payload)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get("/workspaces/{workspace_id}/parameters", response_model=list[ParameterRead])
def list_parameters_endpoint(workspace_id: str, include_noncurrent: bool = Query(default=False)) -> list[ParameterRead]:
    try:
        return list_lifecycle_parameters(workspace_id, include_noncurrent=include_noncurrent)
    except ValueError as exc:
        raise _domain_error(exc) from exc


@router.patch("/parameters/{parameter_id}", response_model=ParameterRead)
def update_parameter_endpoint(parameter_id: str, payload: ParameterUpdate) -> ParameterRead:
    try:
        return update_parameter(parameter_id, payload)
    except (ParameterLifecycleError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.post("/parameters/{parameter_id}/lifecycle", response_model=ParameterRead)
def transition_parameter_endpoint(parameter_id: str, payload: ParameterLifecycleCommand) -> ParameterRead:
    try:
        return transition_parameter(parameter_id, payload)
    except (ParameterLifecycleError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/parameters/{parameter_id}/history",
    response_model=ValueVersionChainRead,
)
@router.get(
    "/parameters/{parameter_id}/history",
    response_model=ValueVersionChainRead,
)
def get_parameter_history_endpoint(
    parameter_id: str,
    workspace_id: str = "main",
) -> ValueVersionChainRead:
    with open_sqlite_connection() as connection:
        try:
            return get_parameter_version_chain(connection, workspace_id, parameter_id)
        except ValueHistoryError as exc:
            status_code = 404 if "not_found" in exc.code else 400
            raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get(
    "/workspaces/{workspace_id}/parameters/compare",
    response_model=ValueComparisonRead,
)
def compare_parameters_endpoint(
    workspace_id: str,
    version_a: str,
    version_b: str,
) -> ValueComparisonRead:
    with open_sqlite_connection() as connection:
        try:
            return compare_value_versions(
                connection,
                workspace_id=workspace_id,
                value_kind="parameter",
                parameter_a_id=version_a,
                parameter_b_id=version_b,
            )
        except ValueHistoryError as exc:
            status_code = 404 if "not_found" in exc.code else 400
            raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get(
    "/workspaces/{workspace_id}/values/compare",
    response_model=ValueComparisonRead,
)
def compare_values_get_endpoint(
    workspace_id: str,
    value_kind: str | None = None,
    value_id: str | None = None,
    version_a: str | None = None,
    version_b: str | None = None,
    parameter_a_id: str | None = None,
    parameter_b_id: str | None = None,
) -> ValueComparisonRead:
    with open_sqlite_connection() as connection:
        try:
            return compare_value_versions(
                connection,
                workspace_id=workspace_id,
                value_kind=value_kind,
                value_id=value_id,
                version_a=version_a,
                version_b=version_b,
                parameter_a_id=parameter_a_id,
                parameter_b_id=parameter_b_id,
            )
        except ValueHistoryError as exc:
            status_code = 404 if "not_found" in exc.code else 400
            raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get(
    "/workspaces/{workspace_id}/result-values/{value_id}/history",
    response_model=ValueVersionChainRead,
)
@router.get(
    "/result-values/{value_id}/history",
    response_model=ValueVersionChainRead,
)
def get_result_value_history_endpoint(
    value_id: str,
    workspace_id: str = "main",
) -> ValueVersionChainRead:
    with open_sqlite_connection() as connection:
        try:
            return get_sim_result_value_version_chain(connection, workspace_id, value_id)
        except ValueHistoryError as exc:
            status_code = 404 if "not_found" in exc.code else 400
            raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get(
    "/workspaces/{workspace_id}/result-values/{value_id}/compare",
    response_model=ValueComparisonRead,
)
def compare_result_values_get_endpoint(
    workspace_id: str,
    value_id: str,
    version_a: str,
    version_b: str,
) -> ValueComparisonRead:
    with open_sqlite_connection() as connection:
        try:
            return compare_value_versions(
                connection,
                workspace_id=workspace_id,
                value_kind="simulation_result_value",
                value_id=value_id,
                version_a=version_a,
                version_b=version_b,
            )
        except ValueHistoryError as exc:
            status_code = 404 if "not_found" in exc.code else 400
            raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc





@router.post("/workspaces/{workspace_id}/requirements", response_model=RequirementRead, status_code=201)
def create_requirement_endpoint(workspace_id: str, payload: RequirementCreate) -> RequirementRead:
    try:
        return create_requirement(workspace_id, payload)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get("/workspaces/{workspace_id}/requirements", response_model=list[RequirementRead])
def list_requirements_endpoint(workspace_id: str) -> list[RequirementRead]:
    try:
        return list_requirements(workspace_id)
    except ValueError as exc:
        raise _domain_error(exc) from exc


@router.get("/requirements/{requirement_id}", response_model=RequirementRead)
def get_requirement_endpoint(requirement_id: str) -> RequirementRead:
    requirement = get_requirement(requirement_id)
    if requirement is None:
        raise HTTPException(status_code=404, detail="Requirement not found.")
    return requirement


@router.patch("/requirements/{requirement_id}", response_model=RequirementRead)
def update_requirement_endpoint(requirement_id: str, payload: RequirementProjectUpdate) -> RequirementRead:
    try:
        return update_requirement_canonical(requirement_id, payload)
    except (ProjectKnowledgeOwnerError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.post("/workspaces/{workspace_id}/simulation-runs", response_model=SimulationRunRead, status_code=201)
def create_simulation_run_endpoint(workspace_id: str, payload: SimulationRunCreate) -> SimulationRunRead:
    try:
        return create_simulation_run(workspace_id, payload)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get("/workspaces/{workspace_id}/simulation-runs", response_model=list[SimulationRunRead])
def list_simulation_runs_endpoint(workspace_id: str) -> list[SimulationRunRead]:
    try:
        return list_simulation_runs(workspace_id)
    except ValueError as exc:
        raise _domain_error(exc) from exc


@router.post("/workspaces/{workspace_id}/decisions", response_model=DecisionRead, status_code=201)
def create_decision_endpoint(workspace_id: str, payload: DecisionCreate) -> DecisionRead:
    try:
        return create_decision(workspace_id, payload)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get("/workspaces/{workspace_id}/decisions", response_model=list[DecisionRead])
def list_decisions_endpoint(workspace_id: str) -> list[DecisionRead]:
    try:
        return list_decisions(workspace_id)
    except ValueError as exc:
        raise _domain_error(exc) from exc
