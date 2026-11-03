import json
import sqlite3

from fastapi import APIRouter, HTTPException

from app.core.database import open_sqlite_connection
from app.modules.ai.context_builder import ContextSelectionSpec, build_workspace_context_bundle
from app.modules.ai.egress_persistence import EgressStateError
from app.modules.ai.egress_service import EgressContractError
from app.modules.ai.escalations import confirm_escalation
from app.modules.ai.gateway import AIGateway
from app.modules.ai.jarvis_context_routes import router as jarvis_context_router
from app.modules.ai.models import (
    AISettingsRead,
    AISettingsUpdate,
    AIStatusRead,
    AITaskRunRequest,
    AITaskRunResponse,
    ContextPackPreviewRequest,
    ContextPackPreviewResponse,
    EscalationConfirmRequest,
    EscalationConfirmResponse,
    ModelingDraftRequest,
    ModelingDraftResponse,
    ProviderSmokeRequest,
    ProviderSmokeResponse,
    SmokeConsoleRequest,
    SmokeConsoleResponse,
    SmokeTestRequest,
    SmokeTestResponse,
    SupervisorPublicTestRequest,
    SupervisorPublicTestResponse,
)
from app.modules.ai.settings import (
    ProviderSettingsRead,
    ensure_ai_settings,
    get_provider_settings,
    project_canonical_ai_status,
    update_ai_settings,
)
from app.modules.ai.thread_routes import router as thread_router

router = APIRouter(prefix="/ai", tags=["ai"])
router.include_router(thread_router)
router.include_router(jarvis_context_router)


@router.get("/settings", response_model=AISettingsRead)
def read_ai_settings() -> AISettingsRead:
    return ensure_ai_settings()


@router.put("/settings", response_model=AISettingsRead)
def update_ai_settings_endpoint(payload: AISettingsUpdate) -> AISettingsRead:
    return update_ai_settings(payload)


@router.get("/status", response_model=AIStatusRead)
def read_ai_status() -> AIStatusRead:
    ensure_ai_settings()
    status = project_canonical_ai_status(AIGateway().status())
    update: dict[str, object] = {}
    if status.budget_status == "within_budget":
        update["budget_status"] = "available"
    if (
        status.provider_id == "scaleway"
        and status.blocking_reason == "provider_credentials_missing"
    ):
        update["blocking_reason"] = "scaleway_api_key_missing"
    return status.model_copy(update=update) if update else status


@router.get("/provider-settings", response_model=ProviderSettingsRead)
def read_ai_provider_settings() -> ProviderSettingsRead:
    ensure_ai_settings()
    return get_provider_settings(AIGateway().status())


@router.post("/modeling/draft", response_model=ModelingDraftResponse)
def create_modeling_draft(payload: ModelingDraftRequest) -> ModelingDraftResponse:
    ensure_ai_settings()
    return AIGateway().create_modeling_draft(payload)


@router.post("/tasks/run", response_model=AITaskRunResponse)
def run_ai_task_endpoint(payload: AITaskRunRequest) -> AITaskRunResponse:
    ensure_ai_settings()
    return _attach_egress_metadata(AIGateway().run_task(payload))


@router.post("/context/packs/preview", response_model=ContextPackPreviewResponse)
def preview_context_pack(payload: ContextPackPreviewRequest) -> ContextPackPreviewResponse:
    selection = ContextSelectionSpec(**payload.selection.model_dump())
    try:
        bundle = build_workspace_context_bundle(
            payload.workspace_id, budget_chars=payload.budget_chars, selection=selection
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail="Related record was not found.") from exc
    char_count = len(json.dumps(bundle.blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return ContextPackPreviewResponse(
        blocks=bundle.blocks,
        context_digest=bundle.context_digest,
        context_sources_manifest=bundle.sources,
        char_count=char_count,
        estimated_token_count=char_count // 4,
        included_count=bundle.included_count,
        dropped_count=bundle.dropped_count,
        budget_chars=bundle.budget_chars,
    )


@router.post("/tasks/escalations/confirm", response_model=EscalationConfirmResponse)
def confirm_ai_task_escalation(payload: EscalationConfirmRequest) -> EscalationConfirmResponse:
    ensure_ai_settings()
    with open_sqlite_connection() as connection:
        governed = connection.execute(
            "SELECT 1 FROM cloud_escalations WHERE ticket_id = ?", (payload.ticket_id,)
        ).fetchone()
    if governed is not None:
        raise HTTPException(status_code=409, detail="confirm this ticket through its thread escalation")
    try:
        return confirm_escalation(payload)
    except EgressStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EgressContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/smoke-tests/run", response_model=SmokeTestResponse)
def run_ai_smoke_tests(payload: SmokeTestRequest) -> SmokeTestResponse:
    ensure_ai_settings()
    return AIGateway().run_smoke_tests(payload)


@router.post("/smoke-console/run", response_model=SmokeConsoleResponse)
def run_ai_smoke_console(payload: SmokeConsoleRequest) -> SmokeConsoleResponse:
    ensure_ai_settings()
    return AIGateway().run_smoke_console(payload)


@router.post("/provider-smoke/run", response_model=ProviderSmokeResponse)
def run_ai_provider_smoke(payload: ProviderSmokeRequest) -> ProviderSmokeResponse:
    ensure_ai_settings()
    return AIGateway().run_provider_smoke(payload)


@router.post("/supervisor/public-test", response_model=SupervisorPublicTestResponse)
def run_ai_supervisor_public_test(payload: SupervisorPublicTestRequest) -> SupervisorPublicTestResponse:
    ensure_ai_settings()
    return AIGateway().run_supervisor_public_test(payload)


def _attach_egress_metadata(response: AITaskRunResponse) -> AITaskRunResponse:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT route_reason_json FROM ai_jobs WHERE id = ?",
            (response.ledger_id,),
        ).fetchone()
    if row is None:
        return response
    try:
        metadata = json.loads(row["route_reason_json"])
    except (TypeError, json.JSONDecodeError):
        return response
    if not isinstance(metadata, dict):
        return response
    trigger_ids = metadata.get("egress_trigger_ids")
    if not isinstance(trigger_ids, list) or not all(isinstance(value, str) for value in trigger_ids):
        trigger_ids = []
    ticket_id = metadata.get("egress_ticket_id")
    update = {
        "egress_decision_id": _optional_text(metadata.get("egress_decision_id")),
        "egress_packet_digest": _optional_text(metadata.get("egress_packet_digest")),
        "egress_ticket_id": _optional_text(ticket_id),
        "egress_reservation_id": _optional_text(metadata.get("egress_reservation_id")),
        "egress_reason_code": _optional_text(metadata.get("egress_reason_code")),
        "egress_trigger_ids": trigger_ids,
    }
    if isinstance(ticket_id, str) and ticket_id:
        update["confirmation_payload"] = {"ticket_id": ticket_id}
    return response.model_copy(update=update)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
