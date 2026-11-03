from fastapi import APIRouter, HTTPException

from app.modules.relay_gateway.service import (
    ContextReleaseCreate,
    ContextReleaseRead,
    RelayEscalationApproval,
    RelayEscalationDraftRead,
    RelayEscalationDraftRequest,
    RelayGatewayError,
    RelayRunRead,
    RelayRunRequest,
    RelayStatusRead,
    create_context_release,
    draft_relay_escalation,
    escalate_with_relay,
    get_relay_run,
    list_context_releases,
    list_relay_runs,
    relay_status,
    revoke_context_release,
    submit_relay_run,
)

router = APIRouter(prefix="/ai", tags=["relay-gateway"])


@router.get("/relay/status", response_model=RelayStatusRead)
def read_relay_status() -> RelayStatusRead:
    return relay_status()


@router.post("/threads/{thread_id}/relay-runs", response_model=RelayRunRead)
def submit_thread_relay_run(thread_id: str, workspace_id: str, payload: RelayRunRequest) -> RelayRunRead:
    try:
        return submit_relay_run(workspace_id, thread_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RelayGatewayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/threads/{thread_id}/relay-runs", response_model=list[RelayRunRead])
def read_thread_relay_runs(thread_id: str, workspace_id: str) -> list[RelayRunRead]:
    return list_relay_runs(workspace_id, thread_id)


@router.get("/threads/{thread_id}/relay-runs/{run_id}", response_model=RelayRunRead)
def read_thread_relay_run(thread_id: str, run_id: str, workspace_id: str) -> RelayRunRead:
    try:
        return get_relay_run(workspace_id, thread_id, run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# Spec 161: operator-facing reasons for refused Relay escalations, in plain language.
_RELAY_ESCALATION_MESSAGES = {
    "text_digest_mismatch": "The text changed after it was shown for approval. Review it again before sending.",
    "source_turn_not_finished": "This answer is still running. Escalate it once it has finished.",
    "relay_run_in_progress": "A Relay run is already in progress for this conversation. Wait for it to finish.",
    "relay_escalation_digest_conflict": "This answer already has a Relay run with different text. Review the existing run before sending another request.",
    "relay_gateway_disabled": "Relay is turned off on this machine, so nothing was sent.",
    "relay_agent_binary_missing": "The Relay agent program is not installed on this machine, so nothing was sent.",
    "relay_agent_login_missing": "The Relay agent is not signed in on this machine, so nothing was sent.",
    "secret_detected": "This text looks like it contains a secret, so it is never sent to a cloud model.",
    "image_not_in_source_turn": "One of the screenshots does not belong to this answer, so nothing was sent. Review it again.",
    "too_many_images": "At most two screenshots can be sent.",
}


def _relay_escalation_error(exc: RelayGatewayError) -> HTTPException:
    code = str(exc)
    message = _RELAY_ESCALATION_MESSAGES.get(code, "This text cannot be sent through Relay as it is. Edit it and review it again.")
    return HTTPException(status_code=409, detail={"code": code, "message": message})


@router.post("/threads/{thread_id}/interactions/{interaction_id}/relay-escalation-draft",
             response_model=RelayEscalationDraftRead)
def draft_thread_relay_escalation(thread_id: str, interaction_id: str, workspace_id: str,
                                  payload: RelayEscalationDraftRequest | None = None) -> RelayEscalationDraftRead:
    """Side-effect free: the exact text, screening result, Relay agent and model."""
    try:
        return draft_relay_escalation(workspace_id, thread_id, interaction_id,
                                      payload.text if payload is not None else None,
                                      payload.surface_context if payload is not None else None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RelayGatewayError as exc:
        raise _relay_escalation_error(exc) from exc


@router.post("/threads/{thread_id}/interactions/{interaction_id}/relay-escalate", response_model=RelayRunRead)
def escalate_thread_interaction_with_relay(thread_id: str, interaction_id: str, workspace_id: str,
                                           payload: RelayEscalationApproval) -> RelayRunRead:
    try:
        return escalate_with_relay(workspace_id, thread_id, interaction_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RelayGatewayError as exc:
        raise _relay_escalation_error(exc) from exc


@router.post("/relay/context-releases", response_model=ContextReleaseRead, status_code=201)
def release_relay_context(workspace_id: str, payload: ContextReleaseCreate) -> ContextReleaseRead:
    try:
        return create_context_release(workspace_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RelayGatewayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/relay/context-releases", response_model=list[ContextReleaseRead])
def read_relay_context_releases(workspace_id: str) -> list[ContextReleaseRead]:
    return list_context_releases(workspace_id)


@router.post("/relay/context-releases/{release_id}/revoke", response_model=ContextReleaseRead)
def revoke_relay_context(release_id: str, workspace_id: str, reason: str) -> ContextReleaseRead:
    try:
        return revoke_context_release(workspace_id, release_id, reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
