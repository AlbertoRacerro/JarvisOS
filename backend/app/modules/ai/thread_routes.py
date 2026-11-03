from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query, Request

from app.modules.ai.cloud_escalation import (
    CloudEscalationError,
    CloudEscalationRead,
    CloudEscalationRequest,
    EscalationApproval,
    EscalationDraftRead,
    EscalationDraftRequest,
    confirm_cloud_escalation,
    create_cloud_escalation,
    draft_interaction_escalation,
    escalate_interaction,
    list_cloud_escalations,
)
from app.modules.ai.egress_persistence import EgressStateError
from app.modules.ai.thread_models import (
    AIConversationOptions,
    AIConversationRoute,
    AIConversationRouteAvailability,
    AIThreadCreate,
    AIThreadDetail,
    AIThreadList,
    AIThreadSubmit,
    AIThreadSubmitRead,
    AIThreadSummary,
)
from app.modules.ai.thread_service import (
    AIThreadConflictError,
    AIThreadError,
    AIThreadNotFoundError,
    create_thread,
    get_thread,
    list_threads,
    submit_interaction,
)

router = APIRouter(prefix="/threads", tags=["ai-threads"])


@router.post("/{thread_id}/cloud-escalations", response_model=CloudEscalationRead)
def submit_cloud_escalation(thread_id: str, workspace_id: str, payload: CloudEscalationRequest) -> CloudEscalationRead:
    try:
        return create_cloud_escalation(workspace_id=workspace_id, thread_id=thread_id, payload=payload)
    except (CloudEscalationError, ValueError, LookupError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# Spec 159: 156 path messages that reach the operator, restated in plain language.
_ESCALATION_MESSAGES = {
    "completed local source interaction required": (
        "source_not_eligible", "Only a finished answer from a local model in this conversation can be escalated."),
    "source must be a local model interaction": (
        "source_not_local", "Only answers produced by a local model can be escalated to the cloud."),
    "request budget exceeded": (
        "request_budget_exceeded", "This request would exceed the per-request cloud spending limit."),
    "thread budget exceeded": (
        "thread_budget_exceeded", "This conversation has reached its cloud spending limit."),
    "no eligible model within the task and request budget": (
        "no_eligible_model", "No cloud model is currently eligible for this kind of task within the budget."),
    "unqualified task family": ("unknown_task_family", "That task type is not configured for cloud escalation."),
    "derivative authority changed": (
        "derivative_changed", "The approved text changed or lost its approval before sending. Try again."),
    "cloud escalation is not awaiting confirmation": (
        "not_awaiting_confirmation", "This cloud request is not waiting for confirmation any more."),
    "approved derivative changed before confirmation": (
        "derivative_changed", "The approved text changed before confirmation, so nothing was sent."),
}


def _escalation_error(exc: Exception, status_code: int = 409) -> HTTPException:
    message = str(exc)
    code = getattr(exc, "code", None)
    if code is None:
        code, message = _ESCALATION_MESSAGES.get(message, ("cloud_escalation_refused", message))
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@router.post("/{thread_id}/interactions/{interaction_id}/escalation-draft", response_model=EscalationDraftRead)
def draft_thread_escalation(
    thread_id: str, interaction_id: str, workspace_id: str,
    payload: EscalationDraftRequest | None = None, task_family: str | None = None,
) -> EscalationDraftRead:
    """Side-effect free: the exact text, screening result, model and maximum cost."""
    try:
        return draft_interaction_escalation(
            workspace_id=workspace_id, thread_id=thread_id,
            interaction_id=interaction_id, task_family=task_family,
            text=payload.text if payload is not None else None,
        )
    except (CloudEscalationError, ValueError, LookupError) as exc:
        raise _escalation_error(exc) from exc


@router.post("/{thread_id}/interactions/{interaction_id}/escalate", response_model=CloudEscalationRead)
def escalate_thread_interaction(
    thread_id: str, interaction_id: str, workspace_id: str, payload: EscalationApproval,
) -> CloudEscalationRead:
    try:
        return escalate_interaction(
            workspace_id=workspace_id, thread_id=thread_id,
            interaction_id=interaction_id, payload=payload,
        )
    except (CloudEscalationError, EgressStateError, ValueError, LookupError) as exc:
        raise _escalation_error(exc) from exc


@router.get("/{thread_id}/cloud-escalations", response_model=list[CloudEscalationRead])
def read_cloud_escalations(thread_id: str, workspace_id: str) -> list[CloudEscalationRead]:
    return list_cloud_escalations(workspace_id=workspace_id, thread_id=thread_id)


@router.post("/{thread_id}/cloud-escalations/{escalation_id}/confirm", response_model=CloudEscalationRead)
def confirm_thread_cloud_escalation(thread_id: str, escalation_id: str, workspace_id: str) -> CloudEscalationRead:
    try:
        return confirm_cloud_escalation(workspace_id=workspace_id, thread_id=thread_id, escalation_id=escalation_id)
    except (CloudEscalationError, EgressStateError, ValueError, LookupError) as exc:
        raise _escalation_error(exc) from exc


@router.get("/conversation-options", response_model=AIConversationOptions)
def read_conversation_options(request: Request) -> AIConversationOptions:
    """Project local route availability without dispatching or exposing configuration."""
    from app.modules.ai.provider_registry import registry_bindings

    try:
        bindings = registry_bindings()
    except (OSError, ValueError):
        bindings = {}
    routes: list[AIConversationRoute] = []
    probes = {"local_ollama": _ollama_route_availability, "local_llamacpp": _llamacpp_route_availability}
    for route, label in (
        ("local:general", "Local assistant"),
        ("local:fast", "Local fast assistant"),
        ("local:coder", "Local coding assistant"),
        ("local:llamacpp", "Local llama.cpp assistant"),
        ("local:fake", "Test responder (synthetic)"),
    ):
        binding = bindings.get(route)
        if not binding or binding.requires_network or binding.execution_class not in {"local_compute", "synthetic"}:
            continue
        if binding.execution_class == "synthetic":
            availability = AIConversationRouteAvailability(
                configured=True, runtime_reachable=None, model_installed=None, model_loaded=None,
                qualified="unknown", reason_code="TEST_ONLY", message="Synthetic test responder; not a real model.",
            )
        else:
            probe = probes.get(binding.provider_id)
            try:
                availability = probe(route, binding.model_id) if probe else AIConversationRouteAvailability(
                    configured=True, runtime_reachable=None, model_installed=None, model_loaded=None,
                    qualified="unknown", reason_code="AVAILABILITY_UNSUPPORTED",
                    message="This local runtime does not report availability yet.",
                )
            except Exception:
                availability = AIConversationRouteAvailability(
                    configured=True, runtime_reachable=None, model_installed=None, model_loaded=None,
                    qualified="unknown", reason_code="AVAILABILITY_PROBE_FAILED",
                    message="Local model availability could not be checked.",
                )
        routes.append(AIConversationRoute(
            route_class=route, label=label, model_id=binding.model_id,
            execution_class=binding.execution_class, availability=availability,
        ))
    agent_availability = _hermes_agent_availability(request)
    routes.append(AIConversationRoute(
        route_class="hermes:agent", label="Jarvis agent (Hermes)", model_id="hermes:agent",
        execution_class="agent", availability=agent_availability,
    ))
    return AIConversationOptions(routes=routes, availability="configured" if routes else "unavailable")


def _hermes_agent_availability(request: Request | None) -> AIConversationRouteAvailability:
    import os
    import shutil
    from pathlib import Path

    from app.modules.agents.hermes.supervisor import UPSTREAM_REVISION

    venv = os.getenv("JARVIS_HERMES_VENV", "")
    python = Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python") if venv else None
    if python is None or not python.is_file():
        reason, message = "HERMES_VENV_MISSING", "Hermes agent runtime is not configured."
    elif os.name == "nt" and not os.getenv("JARVIS_HERMES_WSL_DISTRO"):
        reason, message = "HERMES_ISOLATION_UNAVAILABLE", "Hermes requires a configured isolated WSL runtime."
    elif os.name != "nt" and not shutil.which("bwrap"):
        reason, message = "HERMES_ISOLATION_UNAVAILABLE", "Hermes network isolation is unavailable."
    elif not _hermes_revision_qualified(python, UPSTREAM_REVISION):
        reason, message = "HERMES_REVISION_UNQUALIFIED", "The configured Hermes runtime does not match the pinned revision."
    else:
        route = os.getenv("JARVIS_HERMES_INFERENCE_ROUTE", "local:llamacpp")
        if not route.startswith("local:"):
            reason, message = "HERMES_RUNTIME_UNCONFIGURED", "Hermes requires an explicitly configured local Jarvis inference route."
        else:
            from app.modules.ai.execution import resolve_binding
            try:
                binding, _ = resolve_binding(route)
            except Exception:
                binding = None
            if binding is None:
                reason, message = "HERMES_RUNTIME_UNCONFIGURED", "The configured local Jarvis inference route is unavailable."
            else:
                probe = _llamacpp_route_availability if binding.provider_id == "local_llamacpp" else _ollama_route_availability
                try:
                    route_status = probe(route, binding.model_id)
                except Exception:
                    route_status = None
                if (route_status is None or not route_status.runtime_reachable or route_status.model_installed is False
                        or route_status.reason_code not in {None, "LLAMACPP_LOADING"}):
                    reason, message = "HERMES_RUNTIME_UNCONFIGURED", "The configured local Jarvis inference model is not ready."
                else:
                    reason, message = None, "Hermes agent is ready; its worker starts on the first turn."
    supervisor = request.app.state._state.get("hermes_supervisor") if request is not None else None
    status = supervisor.status() if supervisor is not None else {}
    state = status.get("state")
    per_thread = status.get("threads")
    worker_lost = state == "lost" or (
        isinstance(per_thread, dict)
        and any(isinstance(item, dict) and item.get("last_error") == "worker_lost"
                for item in per_thread.values())
    )
    if worker_lost and reason is None:
        reason, message = "HERMES_WORKER_LOST", "Hermes worker was lost; the next turn will recover the session."
    return AIConversationRouteAvailability(
        # A lost worker is recovered by the next turn, so it must stay submittable.
        configured=bool(python and python.is_file()), runtime_reachable=reason in {None, "HERMES_WORKER_LOST"},
        model_installed=bool(python and python.is_file()), model_loaded=state == "running",
        qualified="unknown", reason_code=reason, message=message,
    )


def _hermes_revision_qualified(python: object, revision: str) -> bool:
    # Probing spawns Python and git; cache per interpreter identity so conversation
    # options (read on every Sidecar load and submit) stay cheap.
    from pathlib import Path
    try:
        stat = Path(str(python)).stat()
    except OSError:
        return False
    return _hermes_revision_probe(str(python), stat.st_mtime_ns, revision)


@lru_cache(maxsize=4)
def _hermes_revision_probe(python: str, mtime_ns: int, revision: str) -> bool:
    del mtime_ns  # cache key only
    import subprocess
    try:
        code = ("from importlib import metadata, util; import subprocess; from pathlib import Path; "
                "s=util.find_spec('run_agent'); "
                "print(metadata.version('hermes-agent') + ':' + subprocess.run(['git','-C',"
                "str(Path(s.origin).parent),'rev-parse','HEAD'],capture_output=True,text=True,check=True)"
                ".stdout.strip())")
        result = subprocess.run([python, "-c", code],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == f"0.21.4:{revision}"


def _ollama_route_availability(route: str, model_id: str) -> AIConversationRouteAvailability:
    from app.modules.local_ai.runtime.status import get_local_ai_runtime_status

    status = get_local_ai_runtime_status()
    reachable = bool(status.get("ollama_reachable"))
    installed: bool | None = None
    loaded: bool | None = None
    if reachable and not status.get("tags_error_type"):
        installed_names = set(status.get("installed_models", []))
        installed = model_id in installed_names
        loaded_names = {
            item.get("name") for item in status.get("loaded_models", []) if isinstance(item, dict)
        }
        if not status.get("ps_error_type"):
            loaded = model_id in loaded_names
    reason = None
    message = "Local model is available."
    if not reachable:
        reason, message = "RUNTIME_UNREACHABLE", "Ollama is not reachable; start the configured local runtime."
    elif installed is False:
        reason, message = "MODEL_NOT_INSTALLED", f"{model_id} is not installed in Ollama."
    elif installed is None:
        reason, message = "MODEL_STATUS_UNKNOWN", "Ollama responds, but installed model status is unavailable."
    return AIConversationRouteAvailability(
        configured=True,
        runtime_reachable=reachable,
        model_installed=installed,
        model_loaded=loaded,
        qualified="unknown",
        reason_code=reason,
        message=message,
    )


def _llamacpp_route_availability(route: str, model_id: str) -> AIConversationRouteAvailability:
    del route, model_id
    from app.modules.local_ai.runtime.llama_cpp import get_llama_cpp_runtime_owner

    status = get_llama_cpp_runtime_owner().status()
    return AIConversationRouteAvailability(
        configured=bool(status.get("configured")),
        runtime_reachable=bool(status.get("runtime_reachable")),
        model_installed=bool(status.get("model_installed")),
        model_loaded=bool(status.get("model_loaded")),
        qualified="unknown",
        reason_code=status.get("reason_code"),
        message=str(status.get("message", "llama.cpp availability is unknown.")),
    )


@router.post("", response_model=AIThreadSummary)
def create_ai_thread(payload: AIThreadCreate) -> AIThreadSummary:
    try:
        return create_thread(payload)
    except AIThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIThreadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=AIThreadList)
def list_ai_threads(
    workspace_id: str,
    limit: int = Query(default=25, ge=1, le=50),
) -> AIThreadList:
    try:
        return list_threads(workspace_id=workspace_id, limit=limit)
    except AIThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIThreadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{thread_id}", response_model=AIThreadDetail)
def read_ai_thread(
    thread_id: str,
    workspace_id: str,
    interaction_limit: int = Query(default=50, ge=1, le=100),
) -> AIThreadDetail:
    try:
        return get_thread(
            workspace_id=workspace_id,
            thread_id=thread_id,
            interaction_limit=interaction_limit,
        )
    except AIThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIThreadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{thread_id}/interactions", response_model=AIThreadSubmitRead)
def submit_ai_thread_interaction(
    thread_id: str,
    payload: AIThreadSubmit,
    workspace_id: str,
    request: Request,
) -> AIThreadSubmitRead:
    try:
        return submit_interaction(
            workspace_id=workspace_id,
            thread_id=thread_id,
            payload=payload,
            app_state=request.app.state,
            route_availability=[route.model_dump() for route in read_conversation_options(request).routes],
        )
    except AIThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIThreadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AIThreadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
