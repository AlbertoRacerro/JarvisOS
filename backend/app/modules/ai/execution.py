"""POS-1 — minimal positive AI execution spine.

run_ai_task resolves a route_class to a provider binding, executes through the
provider-neutral adapter interface (contracts.AIProviderAdapter), and writes one
ai_jobs ledger row per attempt — success AND pre-provider failure (malformed
route, unbound route, missing config/credentials).

External network bindings additionally traverse the mandatory 059b egress boundary
before request construction or adapter invocation. Local bindings preserve the original
provider-neutral execution behavior.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai.context_builder import (
    DEFAULT_CONTEXT_BUDGET_CHARS,
    ContextBlockError,
    assemble_prompt,
    canonical_digest,
    canonicalize_blocks,
    context_sources_manifest,
)
from app.modules.ai.contracts import (
    AIProviderAdapter,
    AIRequest,
    AIResponse,
    AITaskType,
    RoutingDecision,
)
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.flow_record_capture import capture_final_flow_records
from app.modules.ai.providers.fake_adapter import FAKE_PROVIDER_ID, FakeProviderAdapter
from app.modules.ai.providers.local_llamacpp_adapter import (
    LOCAL_LLAMACPP_PROVIDER_ID,
    LocalLlamaCppAdapter,
)
from app.modules.ai.providers.local_ollama_adapter import (
    LOCAL_OLLAMA_PROVIDER_ID,
    LocalOllamaAdapter,
)
from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter
from app.modules.ai.providers.scaleway_adapter import SCALEWAY_PROVIDER_ID, ScalewayProviderAdapter
from app.modules.ai.token_flow_continuation import (
    ContinuationDecision,
    apply_continuation_lineage,
    evaluate_direct_continuation,
)
from app.modules.ai.token_flow_continuation_transaction import (
    record_continuation_attempt_evidence_in_transaction,
)
from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_flow_runtime import (
    local_exception_evidence,
    local_response_evidence,
    no_execution_evidence,
    normalize_finish_reason,
    normalize_outcome_reason,
)
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import (
    create_flow,
    get_flow,
    transition_flow_state,
    validate_existing_flow_for_execution,
)
from app.modules.ai.token_flow_terminalization import terminalize_assembled_output
from app.modules.ai.token_flow_transaction import record_attempt_evidence_in_transaction
from app.modules.events.service import utc_now
from app.modules.memory.models import MemoryProposalCreate
from app.modules.memory.service import create_proposal

ROUTE_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")


@dataclass
class AiTaskOutcome:
    status: str  # success | provider_error | route_unavailable | validation_error | config_error
    ledger_id: str
    selected_route_class: str | None
    decision: RoutingDecision
    response: AIResponse | None = None
    error_type: str | None = None
    context_digest: str | None = None
    context_sources_count: int = 0
    records_parse_error: str | None = None
    proposed_record_ids: list[str] | None = None
    egress_decision_id: str | None = None
    egress_packet_digest: str | None = None
    egress_ticket_id: str | None = None
    egress_reservation_id: str | None = None
    egress_reason_code: str | None = None
    egress_trigger_ids: tuple[str, ...] = ()
    flow_id: str | None = None


# task_kind -> default route. Cloud calls must be opted into explicitly through
# route_class so task selection cannot silently spend tokens.
TASK_KIND_DEFAULT_ROUTE: dict[str, str] = {
    "general": "local:fake",
    "test": "local:fake",
    "synthesis": "local:fake",
    "code_review": "local:fake",
    "architecture_review": "local:fake",
}


def resolve_effective_route_class(*, task_kind: str, route_class: str | None) -> str:
    """Resolve omitted routes through the execution-owned task-kind defaults."""
    if route_class:
        return route_class
    return TASK_KIND_DEFAULT_ROUTE.get(task_kind, "local:fake")


RECORD_CAPTURE_TASK_KINDS = {"decision_support"}

_TASK_KIND_TO_AI_TASK_TYPE: dict[str, AITaskType] = {
    "code_review": AITaskType.code_review,
    "architecture_review": AITaskType.code_review,
    "synthesis": AITaskType.synthesis,
    "decision_support": AITaskType.decision_support,
}


def _local_model(default: str, *env_names: str) -> str:
    for env_name in env_names:
        configured = os.getenv(env_name)
        if configured:
            return configured
    return default


def _default_bindings() -> dict[str, ProviderBinding]:
    """Load default route bindings from the provider registry config."""
    from app.modules.ai.provider_registry import registry_bindings

    return registry_bindings()


def _default_adapters() -> dict[str, AIProviderAdapter]:
    adapters: dict[str, AIProviderAdapter] = {
        FAKE_PROVIDER_ID: FakeProviderAdapter(),
        LOCAL_OLLAMA_PROVIDER_ID: LocalOllamaAdapter(),
        LOCAL_LLAMACPP_PROVIDER_ID: LocalLlamaCppAdapter(),
        SCALEWAY_PROVIDER_ID: ScalewayProviderAdapter(),
    }
    from app.modules.ai.provider_registry import load_default_provider_registry

    registry = load_default_provider_registry()
    for provider in registry.providers.values():
        if provider.enabled and provider.kind == "openai_compatible" and provider.base_url and provider.api_key_ref:
            primary_model = next(
                (model for model in registry.models.values() if model.provider_id == provider.provider_id),
                None,
            )
            if primary_model is None:
                continue
            adapters[provider.provider_id] = OpenAICompatAdapter(
                provider_id=provider.provider_id,
                model_id=primary_model.model_id,
                base_url=provider.base_url,
                api_key_ref=provider.api_key_ref,
                timeout_seconds=provider.timeout_seconds,
            )
    return adapters


def resolve_binding(
    route_class: str, bindings: dict[str, ProviderBinding] | None = None
) -> tuple[ProviderBinding | None, RoutingDecision]:
    table = bindings if bindings is not None else _default_bindings()
    if not ROUTE_CLASS_RE.match(route_class):
        return None, RoutingDecision(
            blocked=True,
            blocked_reason="route_class_malformed",
            decision_reason=f"route_class '{route_class}' is not namespace:name",
        )
    binding = table.get(route_class)
    if binding is None:
        return None, RoutingDecision(
            blocked=True,
            blocked_reason="route_unavailable",
            decision_reason=f"no binding configured for {route_class}",
            considered_models=sorted(table),
        )
    return binding, RoutingDecision(
        provider_id=binding.provider_id, model_id=binding.model_id, decision_reason=f"bound:{route_class}"
    )


def _scaleway_ready() -> bool:
    from app.modules.secrets.storage import get_effective_scaleway_api_key

    return get_effective_scaleway_api_key().key_present


def _ai_task_type_for(task_kind: str) -> AITaskType:
    return _TASK_KIND_TO_AI_TASK_TYPE.get(task_kind, AITaskType.synthesis)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _write_ai_job(
    *,
    status: str,
    task_kind: str,
    requested_route_class: str | None,
    selected_route_class: str | None,
    decision: RoutingDecision,
    prompt_digest: str | None,
    context_digest: str | None,
    context_sources: list[dict] | None,
    response: AIResponse | None,
    latency_ms: int,
    error_type: str | None,
    route_metadata: dict[str, object] | None = None,
    fallback_index: int | None = None,
    flow_id: str | None = None,
    evidence: AttemptEvidence | None = None,
    continuation_decision: ContinuationDecision | None = None,
    input_tokens_override: int | None = None,
    output_tokens_override: int | None = None,
) -> str:
    job_id = str(uuid4())
    provider_id = decision.provider_id or (response.provider_id if response is not None else None)
    model_id = decision.model_id or (response.model_id if response is not None else None)
    output_digest = (
        canonical_digest({"text": response.text}) if response is not None and response.text is not None else None
    )
    input_tokens = (
        input_tokens_override
        if input_tokens_override is not None
        else response.usage.input_tokens if response is not None else None
    )
    output_tokens = (
        output_tokens_override
        if output_tokens_override is not None
        else response.usage.output_tokens if response is not None else None
    )
    cost_estimate = response.usage.provider_cost_estimate if response is not None else None
    route_reason: dict[str, object] = {
        "decision_reason": decision.decision_reason, "blocked_reason": decision.blocked_reason,
    }
    if route_metadata:
        route_reason.update(route_metadata)
    route_reason_json = json.dumps(route_reason, sort_keys=True)
    context_sources_json = json.dumps(context_sources) if context_sources else None
    if (flow_id is None) != (evidence is None):
        raise ValueError("flow_id and attempt evidence must be supplied together")
    if continuation_decision is not None and (flow_id is None or evidence is None):
        raise ValueError("continuation decision requires flow and attempt evidence")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO ai_jobs (
                    id, created_at, status, task_kind, requested_route_class, selected_route_class,
                    provider_id, model_id, route_reason_json, prompt_digest, context_digest,
                    context_sources_json, output_digest, input_tokens, output_tokens, cost_estimate,
                    latency_ms, error_type, fallback_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    utc_now(),
                    status,
                    task_kind,
                    requested_route_class,
                    selected_route_class,
                    provider_id,
                    model_id,
                    route_reason_json,
                    prompt_digest,
                    context_digest,
                    context_sources_json,
                    output_digest,
                    input_tokens,
                    output_tokens,
                    cost_estimate,
                    latency_ms,
                    error_type,
                    fallback_index,
                ),
            )
            if flow_id is not None and evidence is not None:
                if continuation_decision is None:
                    record_attempt_evidence_in_transaction(
                        connection,
                        flow_id=flow_id,
                        attempt_id=job_id,
                        evidence=evidence,
                    )
                else:
                    record_continuation_attempt_evidence_in_transaction(
                        connection,
                        flow_id=flow_id,
                        attempt_id=job_id,
                        evidence=evidence,
                        decision=continuation_decision,
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return job_id


def _config_reason(adapter: AIProviderAdapter | None, binding: ProviderBinding, effective_max: int | None) -> str:
    if adapter is None:
        return f"no adapter registered for provider {binding.provider_id}"
    if binding.requires_network and effective_max is None:
        return "max_output_tokens required for network route"
    return f"provider {binding.provider_id} not configured (missing credentials)"


def _registry_fallback_bindings(route_class: str, primary: ProviderBinding) -> list[ProviderBinding]:
    from app.modules.ai.provider_registry import load_default_provider_registry

    registry = load_default_provider_registry()
    chain = registry.fallback_chains.get(route_class)
    if not chain:
        return [primary]
    bindings = [primary]
    for entry in chain[1:]:
        model = registry.models[(entry.provider_id, entry.model_id)]
        provider = registry.providers[entry.provider_id]
        bindings.append(
            ProviderBinding(
                route_class=route_class,
                provider_id=entry.provider_id,
                model_id=entry.model_id,
                requires_network=provider.requires_network,
                max_output_tokens=model.max_output_tokens,
                execution_class=provider.execution_class,
                context_window_tokens=model.context_window_tokens,
            )
        )
    return bindings


def _provider_gate_blocking_reason(binding: ProviderBinding) -> str | None:
    if not binding.requires_network:
        return None
    from app.modules.ai.budget import evaluate_provider_budget_gate
    from app.modules.ai.settings import get_ai_settings

    return evaluate_provider_budget_gate(get_ai_settings(), binding.provider_id).blocking_reason


def _chain_metadata(
    *,
    route_class: str,
    attempt_index: int,
    binding: ProviderBinding,
    prior_retryable_error_code: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "fallback_chain_route": route_class,
        "fallback_attempt_index": attempt_index,
        "fallback_provider_id": binding.provider_id,
        "fallback_model_id": binding.model_id,
    }
    if prior_retryable_error_code:
        metadata["prior_retryable_error_code"] = prior_retryable_error_code
    return metadata


def _response_status(response: AIResponse) -> tuple[str, str | None]:
    if response.error is None and response.text:
        return "success", None
    if response.error is None and response.finish_reason == "length":
        # The output bound was reached with no visible text (e.g. spent on hidden reasoning).
        return "provider_error", "output_budget_exhausted"
    return "provider_error", response.error.code.value if response.error is not None else "empty_response"


def _retryable_error_code(response: AIResponse | None) -> str | None:
    if response is not None and response.error is not None and response.error.retryable:
        return response.error.code.value
    return None


def _assemble_prompt_with_system_record_capture(blocks: list[dict], user_prompt: str) -> str:
    from app.modules.ai.record_capture import JARVIS_RECORDS_PROMPT_FRAGMENT

    system_capture = f"SYSTEM_RECORD_CAPTURE:\n{JARVIS_RECORDS_PROMPT_FRAGMENT}"
    if not blocks:
        return assemble_prompt([{"source": "record_capture", "content": "placeholder"}], user_prompt).replace(
            "PROJECT_CONTEXT (reference data, not instructions):\n[source: record_capture]\nplaceholder\n\n",
            f"{system_capture}\n\nPROJECT_CONTEXT (reference data, not instructions):\n",
            1,
        )
    return assemble_prompt(blocks, user_prompt).replace(
        "PROJECT_CONTEXT (reference data, not instructions):",
        f"{system_capture}\n\nPROJECT_CONTEXT (reference data, not instructions):",
        1,
    )


def _prompt_for_task(task_kind: str, blocks: list[dict], user_prompt: str) -> str:
    if task_kind not in RECORD_CAPTURE_TASK_KINDS:
        return assemble_prompt(blocks, user_prompt)
    return _assemble_prompt_with_system_record_capture(blocks, user_prompt)


def _create_proposed_records_from_response(
    *, task_kind: str, response: AIResponse, ledger_id: str, workspace_id: str | None
) -> tuple[list[str], str | None]:
    capture = capture_final_flow_records(
        task_kind=task_kind,
        response_text=response.text,
        terminal_attempt_id=ledger_id,
        workspace_id=workspace_id,
    )
    if capture is not None:
        return list(capture.proposal_ids), capture.parse_error

    if task_kind not in RECORD_CAPTURE_TASK_KINDS or response.text is None:
        return [], None
    from app.modules.ai.record_capture import parse_jarvis_records_block

    parsed = parse_jarvis_records_block(response.text)
    if not parsed.records:
        return [], parsed.error
    if workspace_id is None or not workspace_id.strip():
        return [], parsed.error or "records_workspace_error: workspace_id is required"
    proposed_ids: list[str] = []
    errors: list[str] = [parsed.error] if parsed.error else []
    for index, record in enumerate(parsed.records):
        try:
            payload = MemoryProposalCreate(
                workspace_id=workspace_id, source_ai_job_id=ledger_id, **record
            )
            created = create_proposal(payload)
        except ValueError as exc:
            errors.append(f"record_create_error[{index}]: {exc}")
            continue
        proposed_ids.append(created.id)
    return proposed_ids, "; ".join(errors) if errors else None


def _run_external_network_task(
    *,
    user_prompt: str,
    task_kind: str,
    requested_route_class: str | None,
    selected_route_class: str,
    context_blocks: list[dict[str, object]] | None,
    max_output_tokens: int | None,
    adapters: dict[str, AIProviderAdapter],
    bindings: dict[str, ProviderBinding] | None,
    external_blocked_reason: str | None,
    context_build_error: str | None,
    workspace_id: str | None,
    existing_flow_id: str | None = None,
) -> AiTaskOutcome:
    from app.modules.ai.egress_runtime import run_external_task

    external = run_external_task(
        user_prompt=user_prompt,
        task_kind=task_kind,
        selected_route_class=selected_route_class,
        requested_route_class=requested_route_class,
        context_blocks=context_blocks,
        max_output_tokens=max_output_tokens,
        adapters=adapters,
        bindings=bindings,
        workspace_id=workspace_id,
        context_build_error=context_build_error,
        external_blocked_reason=external_blocked_reason,
        task_type_for=_ai_task_type_for,
        task_prompt_for=_prompt_for_task,
        existing_flow_id=existing_flow_id,
    )
    outcome = AiTaskOutcome(
        status=external.status,
        ledger_id=external.ledger_id,
        selected_route_class=external.selected_route_class,
        decision=external.decision,
        response=external.response,
        error_type=external.error_type,
        context_digest=external.context_digest,
        context_sources_count=external.context_sources_count,
        egress_decision_id=external.egress_decision_id,
        egress_packet_digest=external.egress_packet_digest,
        egress_ticket_id=external.egress_ticket_id,
        egress_reservation_id=external.egress_reservation_id,
        egress_reason_code=external.egress_reason_code,
        egress_trigger_ids=external.egress_trigger_ids,
        flow_id=external.flow_id,
    )
    if (
        external.status == "success"
        and external.response is not None
        and normalize_finish_reason(
            external.response.finish_reason, failed=external.response.error is not None
        )
        == "stop"
    ):
        proposed_record_ids, records_parse_error = _create_proposed_records_from_response(
            task_kind=task_kind,
            response=external.response,
            ledger_id=external.ledger_id,
            workspace_id=workspace_id,
        )
        outcome.proposed_record_ids = proposed_record_ids
        outcome.records_parse_error = records_parse_error
    return outcome


def _flow_requested_route(route_class: str | None) -> str | None:
    if route_class is None or ROUTE_CLASS_RE.fullmatch(route_class):
        return route_class
    return None


def _flow_workspace_id(workspace_id: str | None) -> str | None:
    if workspace_id is None:
        return None
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
    return workspace_id if row is not None else None


def _create_local_flow(
    *,
    task_kind: str,
    requested_route_class: str | None,
    workspace_id: str | None,
    existing_flow_id: str | None = None,
) -> str:
    if existing_flow_id is not None:
        validate_existing_flow_for_execution(
            existing_flow_id,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            workspace_id=workspace_id,
        )
        return existing_flow_id

    from app.modules.ai.settings import ensure_ai_settings

    ensure_ai_settings()
    flow = create_flow(
        task_kind=task_kind,
        requested_route_class=_flow_requested_route(requested_route_class),
        workspace_id=_flow_workspace_id(workspace_id),
    )
    return str(flow["id"])


def _terminalize_local_flow(
    *,
    flow_id: str,
    status: str,
    attempt_id: str,
    reason: str | None,
    finish_reason: str | None = None,
    response_text: str | None = None,
) -> None:
    normalized_finish = normalize_finish_reason(finish_reason, failed=False)
    if status == "success" and normalized_finish == "stop":
        state = "complete"
        terminal_reason = "completed"
    elif status == "success":
        state = "partial_terminal"
        terminal_reason = (
            "output_length_limit"
            if normalized_finish == "length"
            else f"finish_{normalized_finish}"
        )
    else:
        state = "failed_terminal"
        terminal_reason = normalize_outcome_reason(reason or status)
    transition_flow_state(
        flow_id=flow_id,
        new_state=state,
        terminal_reason=terminal_reason,
        terminal_attempt_id=attempt_id,
        terminal_response_text=response_text if state == "complete" else None,
    )


_LOCAL_CONTINUATION_SENSITIVITY = "S4"


def _assembled_local_response(response: AIResponse, body_text: str) -> AIResponse:
    return response.model_copy(update={"text": body_text, "content": body_text})


def _run_local_continuations(
    *,
    flow_id: str,
    initial_attempt_id: str,
    initial_response: AIResponse,
    initial_outcome: AiTaskOutcome,
    original_prompt: str,
    task_kind: str,
    requested_route_class: str | None,
    selected_route_class: str,
    context_digest: str | None,
    context_sources: list[dict] | None,
    context_sources_count: int,
    requested_output_tokens: int | None,
    adapters: dict[str, AIProviderAdapter],
    bindings: dict[str, ProviderBinding] | None,
    workspace_id: str | None,
) -> AiTaskOutcome:
    from app.modules.ai.token_flow_local_continuation import plan_local_continuation

    current_attempt_id = initial_attempt_id
    current_response = initial_response
    current_outcome = initial_outcome
    flow_workspace_id = get_flow(flow_id)["workspace_id"]

    while True:
        continuation = evaluate_direct_continuation(
            flow_id=flow_id,
            workspace_id=flow_workspace_id,
            expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
        )
        if not continuation.eligible:
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=current_attempt_id,
                new_state="partial_terminal",
                terminal_reason=f"continuation_{continuation.reason}",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            )
            current_outcome.response = _assembled_local_response(current_response, assembled.body_text)
            return current_outcome

        if requested_output_tokens is None or requested_output_tokens <= 0:
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=current_attempt_id,
                new_state="partial_terminal",
                terminal_reason="continuation_binding_metadata_incomplete",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            )
            current_outcome.response = _assembled_local_response(current_response, assembled.body_text)
            return current_outcome

        plan = plan_local_continuation(
            decision=continuation,
            route_class=selected_route_class,
            task_type=_ai_task_type_for(task_kind),
            original_prompt=original_prompt,
            workspace_id=flow_workspace_id,
            expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            requested_output_tokens=requested_output_tokens,
            bindings=bindings,
        )
        if not plan.ready or plan.binding is None or plan.request is None:
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=current_attempt_id,
                new_state="partial_terminal",
                terminal_reason=f"continuation_{plan.reason}",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            )
            current_outcome.response = _assembled_local_response(current_response, assembled.body_text)
            return current_outcome

        binding = plan.binding
        request = plan.request
        adapter = adapters.get(binding.provider_id)
        route_decision = RoutingDecision(
            provider_id=binding.provider_id,
            model_id=binding.model_id,
            decision_reason=f"bound:{binding.route_class}",
        )
        route_metadata = {
            "continuation_flow_id": flow_id,
            "continuation_parent_attempt_id": continuation.parent_attempt_id,
            "continuation_index": continuation.next_continuation_index,
            "continuation_segment_count": request.metadata.get("continuation_segment_count"),
        }
        if adapter is None:
            evidence = apply_continuation_lineage(
                no_execution_evidence(
                    selected_route_class=binding.route_class,
                    binding=binding,
                    outcome_reason="adapter_unavailable",
                    requested_output_ceiling=requested_output_tokens,
                    effective_output_ceiling=plan.effective_output_tokens,
                    fallback_index=0,
                ),
                continuation,
            )
            ledger_id = _write_ai_job(
                status="config_error",
                task_kind=task_kind,
                requested_route_class=requested_route_class,
                selected_route_class=binding.route_class,
                decision=route_decision,
                prompt_digest=canonical_digest({"prompt": request.prompt or ""}),
                context_digest=context_digest,
                context_sources=context_sources,
                response=None,
                latency_ms=0,
                error_type="config_error",
                route_metadata=route_metadata,
                fallback_index=0,
                flow_id=flow_id,
                evidence=evidence,
                continuation_decision=continuation,
            )
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=ledger_id,
                new_state="partial_terminal",
                terminal_reason="continuation_adapter_unavailable",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            )
            return _outcome_with_flow(
                AiTaskOutcome(
                    "config_error",
                    ledger_id,
                    binding.route_class,
                    route_decision,
                    response=_assembled_local_response(current_response, assembled.body_text),
                    error_type="config_error",
                    context_digest=context_digest,
                    context_sources_count=context_sources_count,
                ),
                flow_id,
            )

        attempt_started = time.perf_counter()
        try:
            response = adapter.complete(request)
        except Exception as exc:
            evidence, usage = local_exception_evidence(
                binding=binding,
                prompt=request.prompt or "",
                selected_route_class=binding.route_class,
                requested_output_ceiling=requested_output_tokens,
                effective_output_ceiling=plan.effective_output_tokens,
                fallback_index=0,
            )
            evidence = apply_continuation_lineage(evidence, continuation)
            ledger_id = _write_ai_job(
                status="provider_error",
                task_kind=task_kind,
                requested_route_class=requested_route_class,
                selected_route_class=binding.route_class,
                decision=route_decision,
                prompt_digest=canonical_digest({"prompt": request.prompt or ""}),
                context_digest=context_digest,
                context_sources=context_sources,
                response=None,
                latency_ms=_elapsed_ms(attempt_started),
                error_type=type(exc).__name__,
                route_metadata=route_metadata,
                fallback_index=0,
                flow_id=flow_id,
                evidence=evidence,
                continuation_decision=continuation,
                input_tokens_override=usage.input_tokens,
                output_tokens_override=usage.output_tokens,
            )
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=ledger_id,
                new_state="partial_terminal",
                terminal_reason="continuation_adapter_exception",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            )
            return _outcome_with_flow(
                AiTaskOutcome(
                    "provider_error",
                    ledger_id,
                    binding.route_class,
                    route_decision,
                    response=_assembled_local_response(current_response, assembled.body_text),
                    error_type=type(exc).__name__,
                    context_digest=context_digest,
                    context_sources_count=context_sources_count,
                ),
                flow_id,
            )

        status, error_type = _response_status(response)
        evidence = apply_continuation_lineage(
            local_response_evidence(
                binding=binding,
                response=response,
                selected_route_class=binding.route_class,
                outcome_reason=error_type or status,
                requested_output_ceiling=requested_output_tokens,
                effective_output_ceiling=plan.effective_output_tokens,
                fallback_index=0,
            ),
            continuation,
        )
        ledger_response: AIResponse | None = response
        input_tokens_override = None
        output_tokens_override = None
        if error_type in {"empty_response", "output_budget_exhausted"}:
            # Preserve usage and finish metadata while preventing an empty visible
            # body from becoming an output digest that cannot own a protected segment.
            ledger_response = None
            input_tokens_override = response.usage.input_tokens
            output_tokens_override = response.usage.output_tokens
        ledger_id = _write_ai_job(
            status=status,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            selected_route_class=binding.route_class,
            decision=route_decision,
            prompt_digest=canonical_digest({"prompt": request.prompt or ""}),
            context_digest=context_digest,
            context_sources=context_sources,
            response=ledger_response,
            latency_ms=_elapsed_ms(attempt_started),
            error_type=error_type,
            route_metadata=route_metadata,
            fallback_index=0,
            flow_id=flow_id,
            evidence=evidence,
            continuation_decision=continuation,
            input_tokens_override=input_tokens_override,
            output_tokens_override=output_tokens_override,
        )
        if status != "success" or response.text is None:
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=ledger_id,
                new_state="partial_terminal",
                terminal_reason=f"continuation_{normalize_outcome_reason(error_type or status)}",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            )
            return _outcome_with_flow(
                AiTaskOutcome(
                    status,
                    ledger_id,
                    binding.route_class,
                    route_decision,
                    response=_assembled_local_response(current_response, assembled.body_text),
                    error_type=error_type,
                    context_digest=context_digest,
                    context_sources_count=context_sources_count,
                ),
                flow_id,
            )

        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=ledger_id,
            body_text=response.text,
            effective_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
            workspace_id=flow_workspace_id,
        )
        normalized_finish = normalize_finish_reason(response.finish_reason, failed=False)
        current_attempt_id = ledger_id
        current_response = response
        current_outcome = _outcome_with_flow(
            AiTaskOutcome(
                "success",
                ledger_id,
                binding.route_class,
                route_decision,
                response=response,
                context_digest=context_digest,
                context_sources_count=context_sources_count,
            ),
            flow_id,
        )
        if normalized_finish == "length":
            continue

        final_state = "complete" if normalized_finish == "stop" else "partial_terminal"
        terminal_reason = (
            "completed" if normalized_finish == "stop" else f"continuation_finish_{normalized_finish}"
        )
        _, assembled = terminalize_assembled_output(
            flow_id=flow_id,
            terminal_attempt_id=ledger_id,
            new_state=final_state,
            terminal_reason=terminal_reason,
            workspace_id=flow_workspace_id,
            expected_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
        )
        assembled_response = _assembled_local_response(response, assembled.body_text)
        current_outcome.response = assembled_response
        if normalized_finish == "stop":
            proposed_ids, parse_error = _create_proposed_records_from_response(
                task_kind=task_kind,
                response=assembled_response,
                ledger_id=ledger_id,
                workspace_id=workspace_id,
            )
            current_outcome.proposed_record_ids = proposed_ids
            current_outcome.records_parse_error = parse_error
        return current_outcome


def _outcome_with_flow(outcome: AiTaskOutcome, flow_id: str) -> AiTaskOutcome:
    outcome.flow_id = flow_id
    return outcome


def run_ai_task(
    *,
    user_prompt: str,
    task_kind: str = "general",
    route_class: str | None = None,
    context_blocks: list[dict[str, object]] | None = None,
    max_output_tokens: int | None = None,
    structured_output_schema: dict[str, object] | None = None,
    adapters: dict[str, AIProviderAdapter] | None = None,
    bindings: dict[str, ProviderBinding] | None = None,
    external_blocked_reason: str | None = None,
    context_build_error: str | None = None,
    workspace_id: str | None = None,
    existing_flow_id: str | None = None,
    images: list[str] | None = None,
) -> AiTaskOutcome:
    started = time.perf_counter()
    adapters = adapters if adapters is not None else _default_adapters()
    requested_route_class = route_class
    selected_route_class = resolve_effective_route_class(
        task_kind=task_kind,
        route_class=route_class,
    )
    prompt_digest = canonical_digest({"prompt": user_prompt})

    early_binding, _early_decision = resolve_binding(selected_route_class, bindings)
    if early_binding is not None and early_binding.requires_network:
        return _run_external_network_task(
            user_prompt=user_prompt,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            selected_route_class=selected_route_class,
            context_blocks=context_blocks,
            max_output_tokens=max_output_tokens,
            adapters=adapters,
            bindings=bindings,
            external_blocked_reason=external_blocked_reason,
            context_build_error=context_build_error,
            workspace_id=workspace_id,
            existing_flow_id=existing_flow_id,
        )

    flow_id = _create_local_flow(
        task_kind=task_kind,
        requested_route_class=requested_route_class,
        workspace_id=workspace_id,
        existing_flow_id=existing_flow_id,
    )

    if context_build_error is not None:
        bad = RoutingDecision(
            provider_id=early_binding.provider_id if early_binding is not None else None,
            model_id=early_binding.model_id if early_binding is not None else None,
            blocked=True,
            blocked_reason="context_build_error",
            decision_reason=context_build_error,
        )
        fallback_index = 0 if early_binding is not None else None
        persisted_route = selected_route_class if ROUTE_CLASS_RE.fullmatch(selected_route_class) else None
        evidence = no_execution_evidence(
            selected_route_class=persisted_route,
            binding=early_binding,
            outcome_reason="context_build_error",
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=None,
            fallback_index=fallback_index,
        )
        ledger_id = _write_ai_job(
            status="config_error",
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            selected_route_class=persisted_route,
            decision=bad,
            prompt_digest=prompt_digest,
            context_digest=None,
            context_sources=None,
            response=None,
            latency_ms=_elapsed_ms(started),
            error_type="context_build_error",
            fallback_index=fallback_index,
            flow_id=flow_id,
            evidence=evidence,
        )
        _terminalize_local_flow(
            flow_id=flow_id,
            status="config_error",
            attempt_id=ledger_id,
            reason="context_build_error",
        )
        return _outcome_with_flow(
            AiTaskOutcome(
                "config_error",
                ledger_id,
                selected_route_class,
                bad,
                error_type="context_build_error",
            ),
            flow_id,
        )

    try:
        blocks = canonicalize_blocks(context_blocks)
    except ContextBlockError as exc:
        bad = RoutingDecision(
            provider_id=early_binding.provider_id if early_binding is not None else None,
            model_id=early_binding.model_id if early_binding is not None else None,
            blocked=True,
            blocked_reason="context_malformed",
            decision_reason=str(exc),
        )
        fallback_index = 0 if early_binding is not None else None
        persisted_route = selected_route_class if ROUTE_CLASS_RE.fullmatch(selected_route_class) else None
        evidence = no_execution_evidence(
            selected_route_class=persisted_route,
            binding=early_binding,
            outcome_reason="context_malformed",
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=None,
            fallback_index=fallback_index,
        )
        ledger_id = _write_ai_job(
            status="validation_error",
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            selected_route_class=persisted_route,
            decision=bad,
            prompt_digest=prompt_digest,
            context_digest=None,
            context_sources=None,
            response=None,
            latency_ms=_elapsed_ms(started),
            error_type="context_malformed",
            fallback_index=fallback_index,
            flow_id=flow_id,
            evidence=evidence,
        )
        _terminalize_local_flow(
            flow_id=flow_id,
            status="validation_error",
            attempt_id=ledger_id,
            reason="context_malformed",
        )
        return _outcome_with_flow(
            AiTaskOutcome(
                "validation_error",
                ledger_id,
                selected_route_class,
                bad,
                error_type="context_malformed",
            ),
            flow_id,
        )

    serialized_context_len = (
        len(json.dumps(blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)) if blocks else 0
    )
    if serialized_context_len > DEFAULT_CONTEXT_BUDGET_CHARS:
        bad = RoutingDecision(
            provider_id=early_binding.provider_id if early_binding is not None else None,
            model_id=early_binding.model_id if early_binding is not None else None,
            blocked=True,
            blocked_reason="context_budget_exceeded",
            decision_reason=f"context {serialized_context_len} chars exceeds budget {DEFAULT_CONTEXT_BUDGET_CHARS}",
        )
        fallback_index = 0 if early_binding is not None else None
        persisted_route = _flow_requested_route(selected_route_class)
        evidence = no_execution_evidence(
            selected_route_class=persisted_route,
            binding=early_binding,
            outcome_reason="context_budget_exceeded",
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=None,
            fallback_index=fallback_index,
        )
        ledger_id = _write_ai_job(
            status="validation_error",
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            selected_route_class=persisted_route,
            decision=bad,
            prompt_digest=prompt_digest,
            context_digest=None,
            context_sources=None,
            response=None,
            latency_ms=_elapsed_ms(started),
            error_type="context_budget_exceeded",
            fallback_index=fallback_index,
            flow_id=flow_id,
            evidence=evidence,
        )
        _terminalize_local_flow(
            flow_id=flow_id,
            status="validation_error",
            attempt_id=ledger_id,
            reason="context_budget_exceeded",
        )
        return _outcome_with_flow(
            AiTaskOutcome(
                "validation_error",
                ledger_id,
                selected_route_class,
                bad,
                error_type="context_budget_exceeded",
            ),
            flow_id,
        )

    context_digest = canonical_digest(blocks) if blocks else None
    context_sources = context_sources_manifest(blocks) if blocks else None
    context_sources_count = len(context_sources) if context_sources else 0

    binding, decision = resolve_binding(selected_route_class, bindings)
    if binding is None:
        status = "validation_error" if decision.blocked_reason == "route_class_malformed" else "route_unavailable"
        persisted_route = selected_route_class if ROUTE_CLASS_RE.fullmatch(selected_route_class) else None
        evidence = no_execution_evidence(
            selected_route_class=persisted_route,
            binding=None,
            outcome_reason=status,
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=None,
        )
        ledger_id = _write_ai_job(
            status=status,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            selected_route_class=persisted_route,
            decision=decision,
            prompt_digest=prompt_digest,
            context_digest=context_digest,
            context_sources=context_sources,
            response=None,
            latency_ms=_elapsed_ms(started),
            error_type=status,
            flow_id=flow_id,
            evidence=evidence,
        )
        _terminalize_local_flow(flow_id=flow_id, status=status, attempt_id=ledger_id, reason=status)
        return _outcome_with_flow(
            AiTaskOutcome(
                status,
                ledger_id,
                selected_route_class,
                decision,
                error_type=status,
                context_digest=context_digest,
                context_sources_count=context_sources_count,
            ),
            flow_id,
        )

    chain_bindings = [binding] if bindings is not None else _registry_fallback_bindings(selected_route_class, binding)
    prior_retryable_error_code: str | None = None
    last_outcome: AiTaskOutcome | None = None
    for attempt_index, attempt_binding in enumerate(chain_bindings):
        adapter = adapters.get(attempt_binding.provider_id)
        attempt_max = max_output_tokens if max_output_tokens is not None else attempt_binding.max_output_tokens
        gate_reason = _provider_gate_blocking_reason(attempt_binding)
        if gate_reason is not None:
            config_decision = RoutingDecision(
                provider_id=attempt_binding.provider_id,
                model_id=attempt_binding.model_id,
                blocked=True,
                blocked_reason="config_error",
                decision_reason=f"external provider execution disabled by settings/gate: {gate_reason}",
            )
            evidence = no_execution_evidence(
                selected_route_class=selected_route_class,
                binding=attempt_binding,
                outcome_reason="config_error",
                requested_output_ceiling=max_output_tokens,
                effective_output_ceiling=attempt_max,
                fallback_index=attempt_index,
            )
            ledger_id = _write_ai_job(
                status="config_error",
                task_kind=task_kind,
                requested_route_class=requested_route_class,
                selected_route_class=selected_route_class,
                decision=config_decision,
                prompt_digest=prompt_digest,
                context_digest=context_digest,
                context_sources=context_sources,
                response=None,
                latency_ms=_elapsed_ms(started),
                error_type="config_error",
                route_metadata=_chain_metadata(
                    route_class=selected_route_class,
                    attempt_index=attempt_index,
                    binding=attempt_binding,
                    prior_retryable_error_code=prior_retryable_error_code,
                ),
                fallback_index=attempt_index,
                flow_id=flow_id,
                evidence=evidence,
            )
            _terminalize_local_flow(
                flow_id=flow_id,
                status="config_error",
                attempt_id=ledger_id,
                reason="config_error",
            )
            return _outcome_with_flow(
                AiTaskOutcome(
                    "config_error",
                    ledger_id,
                    selected_route_class,
                    config_decision,
                    error_type="config_error",
                    context_digest=context_digest,
                    context_sources_count=context_sources_count,
                ),
                flow_id,
            )

        if adapter is None:
            config_decision = RoutingDecision(
                provider_id=attempt_binding.provider_id,
                model_id=attempt_binding.model_id,
                blocked=True,
                blocked_reason="config_error",
                decision_reason=_config_reason(adapter, attempt_binding, attempt_max),
            )
            evidence = no_execution_evidence(
                selected_route_class=selected_route_class,
                binding=attempt_binding,
                outcome_reason="adapter_unavailable",
                requested_output_ceiling=max_output_tokens,
                effective_output_ceiling=attempt_max,
                fallback_index=attempt_index,
            )
            ledger_id = _write_ai_job(
                status="config_error",
                task_kind=task_kind,
                requested_route_class=requested_route_class,
                selected_route_class=selected_route_class,
                decision=config_decision,
                prompt_digest=prompt_digest,
                context_digest=context_digest,
                context_sources=context_sources,
                response=None,
                latency_ms=_elapsed_ms(started),
                error_type="config_error",
                route_metadata=_chain_metadata(
                    route_class=selected_route_class,
                    attempt_index=attempt_index,
                    binding=attempt_binding,
                    prior_retryable_error_code=prior_retryable_error_code,
                ),
                fallback_index=attempt_index,
                flow_id=flow_id,
                evidence=evidence,
            )
            _terminalize_local_flow(
                flow_id=flow_id,
                status="config_error",
                attempt_id=ledger_id,
                reason="adapter_unavailable",
            )
            return _outcome_with_flow(
                AiTaskOutcome(
                    "config_error",
                    ledger_id,
                    selected_route_class,
                    config_decision,
                    error_type="config_error",
                    context_digest=context_digest,
                    context_sources_count=context_sources_count,
                ),
                flow_id,
            )

        attempt_decision = RoutingDecision(
            provider_id=attempt_binding.provider_id,
            model_id=attempt_binding.model_id,
            decision_reason=f"bound:{selected_route_class}",
        )
        request = AIRequest(
            task_type=_ai_task_type_for(task_kind),
            prompt=_prompt_for_task(task_kind, blocks, user_prompt),
            model_preference=attempt_binding.model_id,
            max_output_tokens=attempt_max,
            structured_output_schema=structured_output_schema,
            metadata={"context_digest": context_digest, "selected_route_class": selected_route_class},
            images=list(images or []),
        )
        try:
            response = adapter.complete(request)
        except Exception as exc:
            evidence, usage = local_exception_evidence(
                binding=attempt_binding,
                prompt=request.prompt or "",
                selected_route_class=selected_route_class,
                requested_output_ceiling=max_output_tokens,
                effective_output_ceiling=attempt_max,
                fallback_index=attempt_index,
            )
            ledger_id = _write_ai_job(
                status="provider_error",
                task_kind=task_kind,
                requested_route_class=requested_route_class,
                selected_route_class=selected_route_class,
                decision=attempt_decision,
                prompt_digest=prompt_digest,
                context_digest=context_digest,
                context_sources=context_sources,
                response=None,
                latency_ms=_elapsed_ms(started),
                error_type=type(exc).__name__,
                route_metadata=_chain_metadata(
                    route_class=selected_route_class,
                    attempt_index=attempt_index,
                    binding=attempt_binding,
                    prior_retryable_error_code=prior_retryable_error_code,
                ),
                fallback_index=attempt_index,
                flow_id=flow_id,
                evidence=evidence,
                input_tokens_override=usage.input_tokens,
                output_tokens_override=usage.output_tokens,
            )
            _terminalize_local_flow(
                flow_id=flow_id,
                status="provider_error",
                attempt_id=ledger_id,
                reason=type(exc).__name__,
            )
            return _outcome_with_flow(
                AiTaskOutcome(
                    "provider_error",
                    ledger_id,
                    selected_route_class,
                    attempt_decision,
                    error_type=type(exc).__name__,
                    context_digest=context_digest,
                    context_sources_count=context_sources_count,
                ),
                flow_id,
            )

        status, error_type = _response_status(response)
        evidence = local_response_evidence(
            binding=attempt_binding,
            response=response,
            selected_route_class=selected_route_class,
            outcome_reason=error_type or status,
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=attempt_max,
            fallback_index=attempt_index,
        )
        ledger_id = _write_ai_job(
            status=status,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            selected_route_class=selected_route_class,
            decision=attempt_decision,
            prompt_digest=prompt_digest,
            context_digest=context_digest,
            context_sources=context_sources,
            response=response,
            latency_ms=_elapsed_ms(started),
            error_type=error_type,
            route_metadata=_chain_metadata(
                route_class=selected_route_class,
                attempt_index=attempt_index,
                binding=attempt_binding,
                prior_retryable_error_code=prior_retryable_error_code,
            ),
            fallback_index=attempt_index,
            flow_id=flow_id,
            evidence=evidence,
        )
        last_outcome = _outcome_with_flow(
            AiTaskOutcome(
                status,
                ledger_id,
                selected_route_class,
                attempt_decision,
                response,
                error_type=error_type,
                context_digest=context_digest,
                context_sources_count=context_sources_count,
            ),
            flow_id,
        )
        retryable_error_code = _retryable_error_code(response)
        if status == "provider_error" and retryable_error_code and attempt_index + 1 < len(chain_bindings):
            prior_retryable_error_code = retryable_error_code
            continue

        normalized_finish = normalize_finish_reason(response.finish_reason, failed=False)
        if status == "success" and normalized_finish == "length" and bool(response.text):
            store_protected_segment(
                flow_id=flow_id,
                originating_attempt_id=ledger_id,
                body_text=str(response.text),
                effective_sensitivity_level=_LOCAL_CONTINUATION_SENSITIVITY,
                workspace_id=get_flow(flow_id)["workspace_id"],
            )
            return _run_local_continuations(
                flow_id=flow_id,
                initial_attempt_id=ledger_id,
                initial_response=response,
                initial_outcome=last_outcome,
                original_prompt=request.prompt or "",
                task_kind=task_kind,
                requested_route_class=requested_route_class,
                selected_route_class=selected_route_class,
                context_digest=context_digest,
                context_sources=context_sources,
                context_sources_count=context_sources_count,
                requested_output_tokens=attempt_max,
                adapters=adapters,
                bindings=bindings,
                workspace_id=workspace_id,
            )

        _terminalize_local_flow(
            flow_id=flow_id,
            status=status,
            attempt_id=ledger_id,
            reason=error_type,
            finish_reason=response.finish_reason,
            response_text=response.text,
        )
        if status == "success" and normalized_finish == "stop":
            proposed_record_ids, records_parse_error = _create_proposed_records_from_response(
                task_kind=task_kind,
                response=response,
                ledger_id=ledger_id,
                workspace_id=workspace_id,
            )
            last_outcome.proposed_record_ids = proposed_record_ids
            last_outcome.records_parse_error = records_parse_error
        return last_outcome

    if last_outcome is not None:
        return last_outcome
    raise RuntimeError("provider execution reached unreachable empty chain")
