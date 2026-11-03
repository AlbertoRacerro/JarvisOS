from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.database import open_sqlite_connection
from app.modules.ai import sensitivity
from app.modules.ai.budget import evaluate_provider_budget_gate
from app.modules.ai.context_builder import (
    ContextBlockError,
    assemble_prompt,
    canonical_digest,
    canonicalize_blocks,
    context_sources_manifest,
)
from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderAdapter,
    AIRequest,
    AIResponse,
    AITaskType,
    RoutingDecision,
)
from app.modules.ai.egress_authority import authorize_manual_context, authorize_prompt
from app.modules.ai.egress_lifecycle import (
    reconcile_reserved_attempt,
    start_reserved_attempt,
)
from app.modules.ai.egress_persistence import (
    EgressContinuationAuthority,
    prepare_egress_attempt,
)
from app.modules.ai.egress_policy import (
    EXTERNAL_PROVIDER_OPERATION,
    EgressPolicyConfig,
    load_default_egress_policy,
)
from app.modules.ai.egress_service import (
    EgressContractError,
    EgressPacketMaterial,
    sha256_text,
)
from app.modules.ai.egress_spine import (
    EgressSpineStateError,
    create_queued_ai_job,
    record_prepacket_egress_decision,
)
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import (
    ProviderRegistry,
    load_default_provider_registry,
)
from app.modules.ai.settings import ensure_ai_settings, get_ai_settings
from app.modules.ai.token_flow_continuation import ContinuationDecision
from app.modules.ai.token_flow_external_transaction import finalize_external_attempt
from app.modules.ai.token_flow_runtime import normalize_finish_reason, normalize_outcome_reason
from app.modules.ai.token_flow_service import (
    create_flow,
    transition_flow_state,
    validate_existing_flow_for_execution,
)

_LOCAL_SANITIZER_ROUTE = "local:fast"
_LEVEL_RANK = {"S0": 0, "S1": 1, "S2": 2, "S3": 3, "S4": 4}
_TERMINAL_RESERVATION_STATES = frozenset({"expired", "reconciled", "released"})


@dataclass(frozen=True)
class ExternalTaskOutcome:
    status: str
    ledger_id: str
    selected_route_class: str
    decision: RoutingDecision
    response: AIResponse | None
    error_type: str | None
    context_digest: str | None
    context_sources_count: int
    retryable_error_code: str | None
    egress_decision_id: str | None
    egress_packet_digest: str | None
    egress_ticket_id: str | None
    egress_reservation_id: str | None
    egress_reason_code: str | None
    egress_trigger_ids: tuple[str, ...]
    flow_id: str


@dataclass(frozen=True)
class _ContextView:
    blocks: tuple[dict, ...]
    level: str
    digest: str | None
    included_manifest: tuple[dict, ...]
    withheld_manifest: tuple[dict, ...]
    budget_dropped_manifest: tuple[dict, ...]
    source_digests: tuple[tuple[str, str], ...]


class _PrepacketStop(Exception):
    def __init__(
        self,
        *,
        result: str,
        reason_code: str,
        prompt_level: str,
        context_level: str,
        context_digest: str | None,
        source_count: int,
        included_count: int,
        withheld_count: int,
        detail_reason: str | None,
        ai_error_type: str | None,
    ) -> None:
        super().__init__(reason_code)
        self.result = result
        self.reason_code = reason_code
        self.prompt_level = prompt_level
        self.context_level = context_level
        self.context_digest = context_digest
        self.source_count = source_count
        self.included_count = included_count
        self.withheld_count = withheld_count
        self.detail_reason = detail_reason
        self.ai_error_type = ai_error_type


def _flow_workspace_id(workspace_id: str | None) -> str | None:
    if workspace_id is None:
        return None
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
    return workspace_id if row is not None else None


def _reconcilable_start_failure_reservation(reservation_id: str) -> str | None:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT state FROM egress_budget_reservations WHERE id = ?",
            (reservation_id,),
        ).fetchone()
    if row is None or row["state"] in _TERMINAL_RESERVATION_STATES:
        return None
    return reservation_id


def _create_external_flow(
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
    ensure_ai_settings()
    flow = create_flow(
        task_kind=task_kind,
        requested_route_class=requested_route_class,
        workspace_id=_flow_workspace_id(workspace_id),
    )
    return str(flow["id"])


def _terminalize_external_flow(flow_id: str, outcome: ExternalTaskOutcome) -> None:
    if (
        outcome.status == "validation_error"
        and outcome.egress_reason_code == "confirmation_required"
        and outcome.egress_ticket_id is not None
    ):
        transition_flow_state(flow_id=flow_id, new_state="confirmation_required")
        return
    finish_reason = (
        normalize_finish_reason(outcome.response.finish_reason, failed=False)
        if outcome.status == "success" and outcome.response is not None
        else None
    )
    if outcome.status == "success" and finish_reason == "stop":
        state = "complete"
        reason = "completed"
    elif outcome.status == "success":
        state = "partial_terminal"
        reason = (
            "output_length_limit"
            if finish_reason == "length"
            else f"finish_{finish_reason}"
        )
    else:
        state = "failed_terminal"
        failure_reason = outcome.egress_reason_code
        if failure_reason in {"silent_allow", "ticket_consumed"}:
            failure_reason = None
        reason = normalize_outcome_reason(
            failure_reason or outcome.error_type or outcome.status
        )
    transition_flow_state(
        flow_id=flow_id,
        new_state=state,
        terminal_reason=reason,
        terminal_attempt_id=outcome.ledger_id,
        terminal_response_text=(
            outcome.response.text
            if state == "complete" and outcome.response is not None
            else None
        ),
    )


def run_external_task(
    *,
    user_prompt: str,
    task_kind: str,
    selected_route_class: str,
    requested_route_class: str | None,
    context_blocks: list[dict[str, object]] | None,
    max_output_tokens: int | None,
    adapters: dict[str, AIProviderAdapter],
    bindings: dict[str, ProviderBinding] | None,
    workspace_id: str | None,
    context_build_error: str | None,
    external_blocked_reason: str | None,
    task_type_for: Callable[[str], AITaskType],
    task_prompt_for: Callable[[str, list[dict], str], str] | None = None,
    registry: ProviderRegistry | None = None,
    policy: EgressPolicyConfig | None = None,
    existing_flow_id: str | None = None,
) -> ExternalTaskOutcome:
    """Execute an external route through the mandatory per-binding 059b boundary."""

    from app.modules.ai.execution import resolve_binding

    policy = policy or load_default_egress_policy()
    registry = registry or load_default_provider_registry()
    binding_table = bindings if bindings is not None else registry.bindings
    primary, _decision = resolve_binding(selected_route_class, binding_table)
    if primary is None:
        raise EgressContractError("external runtime requires a resolved route")
    flow_id = _create_external_flow(
        task_kind=task_kind,
        requested_route_class=requested_route_class,
        workspace_id=workspace_id,
        existing_flow_id=existing_flow_id,
    )
    chain = _binding_chain(
        route_class=selected_route_class,
        primary=primary,
        bindings=bindings,
        registry=registry,
    )
    prompt_builder = task_prompt_for or _default_task_prompt
    prior_retryable_error_code: str | None = None
    last_outcome: ExternalTaskOutcome | None = None

    for fallback_index, binding in enumerate(chain):
        outcome = _run_binding(
            user_prompt=user_prompt,
            task_kind=task_kind,
            selected_route_class=selected_route_class,
            requested_route_class=requested_route_class,
            context_blocks=context_blocks,
            max_output_tokens=max_output_tokens,
            adapters=adapters,
            workspace_id=workspace_id,
            context_build_error=context_build_error,
            external_blocked_reason=(
                external_blocked_reason if fallback_index == 0 else None
            ),
            task_type_for=task_type_for,
            task_prompt_for=prompt_builder,
            binding=binding,
            fallback_index=fallback_index,
            prior_retryable_error_code=prior_retryable_error_code,
            registry=registry,
            policy=policy,
            flow_id=flow_id,
        )
        last_outcome = outcome
        if (
            outcome.status == "provider_error"
            and outcome.retryable_error_code is not None
            and fallback_index + 1 < len(chain)
            and not task_kind.startswith("cloud_escalation_")
        ):
            prior_retryable_error_code = outcome.retryable_error_code
            continue
        finish_reason = (
            normalize_finish_reason(
                outcome.response.finish_reason,
                failed=outcome.response.error is not None,
            )
            if outcome.status == "success" and outcome.response is not None
            else None
        )
        if (
            outcome.status == "success"
            and outcome.response is not None
            and bool(outcome.response.text)
            and finish_reason == "length"
        ):
            if task_kind.startswith("cloud_escalation_"):
                _terminalize_external_flow(flow_id, outcome)
                return outcome
            if max_output_tokens is None:
                raise EgressSpineStateError(
                    "successful external attempt omitted output ceiling"
                )
            from app.modules.ai.token_flow_external_runtime import (
                run_silent_allow_external_continuations,
            )

            return run_silent_allow_external_continuations(
                initial_outcome=outcome,
                original_prompt=user_prompt,
                task_kind=task_kind,
                selected_route_class=selected_route_class,
                requested_route_class=requested_route_class,
                context_blocks=context_blocks,
                max_output_tokens=max_output_tokens,
                adapters=adapters,
                bindings=bindings,
                workspace_id=workspace_id,
                task_type_for=task_type_for,
                task_prompt_for=prompt_builder,
                binding=binding,
                fallback_index=fallback_index,
                registry=registry,
                policy=policy,
            )
        _terminalize_external_flow(flow_id, outcome)
        return outcome

    if last_outcome is None:
        raise EgressSpineStateError("external binding chain was empty")
    _terminalize_external_flow(flow_id, last_outcome)
    return last_outcome


def _run_binding(
    *,
    user_prompt: str,
    task_kind: str,
    selected_route_class: str,
    requested_route_class: str | None,
    context_blocks: list[dict[str, object]] | None,
    max_output_tokens: int | None,
    adapters: dict[str, AIProviderAdapter],
    workspace_id: str | None,
    context_build_error: str | None,
    external_blocked_reason: str | None,
    task_type_for: Callable[[str], AITaskType],
    task_prompt_for: Callable[[str, list[dict], str], str],
    binding: ProviderBinding,
    fallback_index: int,
    prior_retryable_error_code: str | None,
    registry: ProviderRegistry,
    policy: EgressPolicyConfig,
    flow_id: str,
    continuation_decision: ContinuationDecision | None = None,
    continuation_expected_sensitivity_level: str | None = None,
) -> ExternalTaskOutcome:
    started_at = time.perf_counter()
    raw_prompt_digest = sha256_text(user_prompt)
    ai_prompt_digest = canonical_digest({"prompt": user_prompt})
    route_metadata = _route_metadata(
        route_class=selected_route_class,
        fallback_index=fallback_index,
        binding=binding,
        prior_retryable_error_code=prior_retryable_error_code,
    )
    if continuation_decision is not None:
        route_metadata = {
            **route_metadata,
            "continuation_flow_id": flow_id,
            "continuation_parent_attempt_id": continuation_decision.parent_attempt_id,
            "continuation_index": continuation_decision.next_continuation_index,
        }

    try:
        if not binding.requires_network:
            raise _stop(
                result="deny",
                reason_code="egress_policy_error",
                prompt_level=_prompt_floor_or_unknown(user_prompt),
                detail_reason="network_binding_required",
                ai_error_type="config_error",
            )
        if max_output_tokens is None:
            raise _stop(
                result="deny",
                reason_code="max_output_tokens_required",
                prompt_level=_prompt_floor_or_unknown(user_prompt),
                detail_reason="max_output_tokens_required",
                ai_error_type="config_error",
            )
        if context_build_error is not None:
            raise _stop(
                result="pause",
                reason_code="context_build_error",
                prompt_level=_prompt_floor_or_unknown(user_prompt),
            )
        gate_reason = external_blocked_reason
        if gate_reason is None:
            gate_reason = evaluate_provider_budget_gate(
                get_ai_settings(),
                binding.provider_id,
            ).blocking_reason
        if gate_reason is not None:
            route_metadata["provider_gate_reason"] = gate_reason
            raise _stop(
                result="deny",
                reason_code="provider_gate_blocked",
                prompt_level=_prompt_floor_or_unknown(user_prompt),
                detail_reason=gate_reason,
                ai_error_type="config_error",
            )

        context = _authorize_context(
            context_blocks=context_blocks,
            workspace_id=workspace_id,
            policy=policy,
            prompt=user_prompt,
        )
        prompt = _authorize_prompt(
            user_prompt=user_prompt,
            task_kind=task_kind,
            workspace_id=workspace_id,
            has_context=bool(context.blocks),
            context=context,
            adapters=adapters,
            registry=registry,
        )
    except _PrepacketStop as stop:
        return _persist_prepacket(
            stop=stop,
            raw_prompt_digest=raw_prompt_digest,
            ai_prompt_digest=ai_prompt_digest,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            route_class=selected_route_class,
            binding=binding,
            fallback_index=fallback_index,
            workspace_id=workspace_id,
            route_metadata=route_metadata,
            started_at=started_at,
            policy=policy,
            requested_output_ceiling=max_output_tokens,
            registry=registry,
            flow_id=flow_id,
            continuation_decision=continuation_decision,
        )

    adapter = adapters.get(binding.provider_id)
    if adapter is None:
        stop = _stop(
            result="deny",
            reason_code="adapter_unavailable",
            prompt_level=prompt.prompt_level or "unknown",
            context_level=context.level,
            context_digest=context.digest,
            source_count=len(context.source_digests),
            included_count=len(context.included_manifest),
            withheld_count=len(context.withheld_manifest),
            detail_reason=f"adapter_unavailable:{binding.provider_id}",
            ai_error_type="config_error",
        )
        return _persist_prepacket(
            stop=stop,
            raw_prompt_digest=raw_prompt_digest,
            ai_prompt_digest=ai_prompt_digest,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            route_class=selected_route_class,
            binding=binding,
            fallback_index=fallback_index,
            workspace_id=workspace_id,
            route_metadata={**route_metadata, "adapter_unavailable": True},
            started_at=started_at,
            policy=policy,
            requested_output_ceiling=max_output_tokens,
            registry=registry,
            flow_id=flow_id,
            continuation_decision=continuation_decision,
        )

    attempt_max = min(max_output_tokens, binding.max_output_tokens)
    final_level = max(
        (prompt.prompt_level or "S1", context.level),
        key=_LEVEL_RANK.__getitem__,
    )
    if continuation_decision is None:
        material = EgressPacketMaterial(
            operation=EXTERNAL_PROVIDER_OPERATION,
            task_kind=task_kind,
            route_class=selected_route_class,
            provider_id=binding.provider_id,
            model_id=binding.model_id,
            fallback_index=fallback_index,
            prompt=prompt.effective_prompt or "",
            context_blocks=context.blocks,
            prompt_level=prompt.prompt_level or "S1",
            context_level=context.level,
            final_level=final_level,
            max_output_tokens=attempt_max,
            workspace_id=workspace_id,
            prompt_derivative_id=prompt.prompt_derivative_id,
            included_manifest=context.included_manifest,
            withheld_manifest=context.withheld_manifest,
            budget_dropped_manifest=context.budget_dropped_manifest,
            source_digests=context.source_digests,
        )
    else:
        if continuation_expected_sensitivity_level is None:
            raise EgressSpineStateError(
                "external continuation authority is incomplete"
            )
        from app.modules.ai.token_flow_external_continuation import (
            build_external_continuation_packet,
        )

        continuation_packet = build_external_continuation_packet(
            decision=continuation_decision,
            route_class=selected_route_class,
            task_kind=task_kind,
            original_prompt=prompt.effective_prompt or "",
            workspace_id=_flow_workspace_id(workspace_id),
            prompt_level=prompt.prompt_level or "S1",
            expected_sensitivity_level=continuation_expected_sensitivity_level,
            requested_output_tokens=attempt_max,
            fallback_index=fallback_index,
            prompt_derivative_id=prompt.prompt_derivative_id,
            context_blocks=context.blocks,
            context_level=context.level,
            included_manifest=context.included_manifest,
            withheld_manifest=context.withheld_manifest,
            budget_dropped_manifest=context.budget_dropped_manifest,
            source_digests=context.source_digests,
            bindings={selected_route_class: binding},
            registry=registry,
            policy=policy,
        )
        material = continuation_packet.material
        ai_prompt_digest = canonical_digest({"prompt": material.prompt})
    continuation_authority = None
    if continuation_decision is not None:
        if (
            continuation_decision.parent_attempt_id is None
            or continuation_decision.parent_flow_attempt_index is None
            or continuation_decision.next_continuation_index is None
            or continuation_decision.protected_segment_index is None
            or continuation_expected_sensitivity_level is None
        ):
            raise EgressSpineStateError(
                "external continuation decision omitted persisted authority"
            )
        continuation_authority = EgressContinuationAuthority(
            flow_id=flow_id,
            parent_attempt_id=continuation_decision.parent_attempt_id,
            parent_flow_attempt_index=continuation_decision.parent_flow_attempt_index,
            next_continuation_index=continuation_decision.next_continuation_index,
            protected_segment_index=continuation_decision.protected_segment_index,
            expected_sensitivity_level=continuation_expected_sensitivity_level,
        )
    try:
        preparation = prepare_egress_attempt(
            material,
            registry=registry,
            policy=policy,
            continuation_authority=continuation_authority,
        )
    except ValueError as exc:
        stop = _stop(
            result="deny",
            reason_code="egress_policy_error",
            prompt_level=prompt.prompt_level or "unknown",
            context_level=context.level,
            context_digest=context.digest,
            source_count=len(context.source_digests),
            included_count=len(context.included_manifest),
            withheld_count=len(context.withheld_manifest),
            detail_reason=type(exc).__name__,
            ai_error_type="config_error",
        )
        return _persist_prepacket(
            stop=stop,
            raw_prompt_digest=raw_prompt_digest,
            ai_prompt_digest=ai_prompt_digest,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            route_class=selected_route_class,
            binding=binding,
            fallback_index=fallback_index,
            workspace_id=workspace_id,
            route_metadata=route_metadata,
            started_at=started_at,
            policy=policy,
            requested_output_ceiling=max_output_tokens,
            registry=registry,
            flow_id=flow_id,
            continuation_decision=continuation_decision,
        )

    route_metadata = {
        **route_metadata,
        "egress_decision_id": preparation.decision_id,
        "egress_packet_digest": preparation.packet_digest,
        "egress_reason_code": preparation.reason_code,
        "egress_ticket_id": preparation.ticket_id,
        "egress_trigger_ids": list(preparation.trigger_ids),
    }
    if preparation.result != "allow":
        status = "validation_error" if preparation.result == "pause" else "config_error"
        ledger_id = _terminal_job(
            flow_id=flow_id,
            task_kind=task_kind,
            requested_route_class=requested_route_class,
            route_class=selected_route_class,
            binding=binding,
            prompt_digest=ai_prompt_digest,
            context=context,
            route_metadata=route_metadata,
            status=status,
            error_type=preparation.reason_code,
            started_at=started_at,
            decision_reason=f"egress:{preparation.reason_code}",
            blocked_reason=preparation.reason_code,
            fallback_index=fallback_index,
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=attempt_max,
            registry=registry,
            continuation_decision=continuation_decision,
        )
        return _outcome(
            status=status,
            ledger_id=ledger_id,
            flow_id=flow_id,
            route_class=selected_route_class,
            binding=binding,
            response=None,
            error_type=preparation.reason_code,
            context=context,
            egress_decision_id=preparation.decision_id,
            packet_digest=preparation.packet_digest,
            ticket_id=preparation.ticket_id,
            reservation_id=None,
            reason_code=preparation.reason_code,
            trigger_ids=preparation.trigger_ids,
            blocked=True,
        )

    reservation_id = preparation.reservation_id
    if reservation_id is None:
        raise EgressSpineStateError("silent allow did not create a reservation")
    queued = create_queued_ai_job(
        task_kind=task_kind,
        requested_route_class=requested_route_class,
        selected_route_class=selected_route_class,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        decision_reason=f"bound:{selected_route_class}",
        prompt_digest=ai_prompt_digest,
        context_digest=context.digest,
        context_sources=(
            context_sources_manifest(list(context.blocks)) if context.blocks else None
        ),
        route_metadata=route_metadata,
    )

    try:
        reserved = start_reserved_attempt(
            reservation_id,
            ai_job_id=queued.ai_job_id,
        )
        packet = _load_packet(reserved.packet_json)
        request = AIRequest(
            task_type=task_type_for(task_kind),
            prompt=task_prompt_for(
                task_kind,
                packet["context_blocks"],
                packet["prompt"],
            ),
            model_preference=reserved.model_id,
            max_output_tokens=reserved.max_output_tokens,
            metadata={
                "egress_decision_id": reserved.decision_id,
                "egress_packet_digest": reserved.packet_digest,
                "selected_route_class": reserved.route_class,
            },
        )
    except Exception as exc:
        finalize_external_attempt(
            flow_id=flow_id,
            ai_job_id=queued.ai_job_id,
            binding=binding,
            fallback_index=fallback_index,
            status="config_error",
            response=None,
            latency_ms=_elapsed_ms(started_at),
            error_type=type(exc).__name__,
            adapter_invoked=False,
            dispatch_state=AIExternalDispatchState.not_started,
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=attempt_max,
            outcome_reason="egress_start_failed",
            reservation_id=_reconcilable_start_failure_reservation(reservation_id),
            registry=registry,
            continuation_decision=continuation_decision,
        )
        return _outcome(
            status="config_error",
            ledger_id=queued.ai_job_id,
            flow_id=flow_id,
            route_class=selected_route_class,
            binding=binding,
            response=None,
            error_type=type(exc).__name__,
            context=context,
            egress_decision_id=preparation.decision_id,
            packet_digest=preparation.packet_digest,
            ticket_id=None,
            reservation_id=reservation_id,
            reason_code="egress_start_failed",
            trigger_ids=(),
            blocked=True,
        )

    try:
        response = adapter.complete(request)
    except Exception as exc:
        finalize_external_attempt(
            flow_id=flow_id,
            ai_job_id=queued.ai_job_id,
            binding=binding,
            fallback_index=fallback_index,
            status="provider_error",
            response=None,
            latency_ms=_elapsed_ms(started_at),
            error_type=type(exc).__name__,
            adapter_invoked=True,
            dispatch_state=AIExternalDispatchState.unknown,
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=reserved.max_output_tokens,
            outcome_reason=type(exc).__name__,
            reservation_id=reservation_id,
            registry=registry,
            continuation_decision=continuation_decision,
        )
        return _outcome(
            status="provider_error",
            ledger_id=queued.ai_job_id,
            flow_id=flow_id,
            route_class=selected_route_class,
            binding=binding,
            response=None,
            error_type=type(exc).__name__,
            context=context,
            egress_decision_id=preparation.decision_id,
            packet_digest=preparation.packet_digest,
            ticket_id=None,
            reservation_id=reservation_id,
            reason_code=preparation.reason_code,
            trigger_ids=(),
            blocked=False,
        )

    status, error_type = _response_status(response)
    binding_mismatch = (
        (response.provider_id, response.model_id)
        != (binding.provider_id, binding.model_id)
        or (response.usage.provider_id, response.usage.model_id)
        != (binding.provider_id, binding.model_id)
    )
    dispatch_invalid = response.external_dispatch_state not in {
        AIExternalDispatchState.not_started,
        AIExternalDispatchState.started,
        AIExternalDispatchState.unknown,
    } or (
        status == "success"
        and response.external_dispatch_state is AIExternalDispatchState.not_started
    )
    if binding_mismatch or dispatch_invalid:
        reason_code = (
            "response_binding_mismatch" if binding_mismatch else "response_dispatch_invalid"
        )
        error_name = "EgressSpineStateError" if binding_mismatch else "EgressContractError"
        finalize_external_attempt(
            flow_id=flow_id,
            ai_job_id=queued.ai_job_id,
            binding=binding,
            fallback_index=fallback_index,
            status="provider_error",
            response=None,
            latency_ms=_elapsed_ms(started_at),
            error_type=error_name,
            adapter_invoked=True,
            dispatch_state=AIExternalDispatchState.unknown,
            requested_output_ceiling=max_output_tokens,
            effective_output_ceiling=reserved.max_output_tokens,
            outcome_reason=reason_code,
            reservation_id=reservation_id,
            registry=registry,
            continuation_decision=continuation_decision,
        )
        return _outcome(
            status="provider_error",
            ledger_id=queued.ai_job_id,
            flow_id=flow_id,
            route_class=selected_route_class,
            binding=binding,
            response=None,
            error_type=error_name,
            context=context,
            egress_decision_id=preparation.decision_id,
            packet_digest=preparation.packet_digest,
            ticket_id=None,
            reservation_id=reservation_id,
            reason_code=reason_code,
            trigger_ids=(),
            blocked=False,
        )

    finalize_external_attempt(
        flow_id=flow_id,
        ai_job_id=queued.ai_job_id,
        binding=binding,
        fallback_index=fallback_index,
        status=status,
        response=response,
        latency_ms=_elapsed_ms(started_at),
        error_type=error_type,
        adapter_invoked=True,
        dispatch_state=response.external_dispatch_state,
        requested_output_ceiling=max_output_tokens,
        effective_output_ceiling=reserved.max_output_tokens,
        outcome_reason=error_type or status,
        reservation_id=reservation_id,
        registry=registry,
        continuation_decision=continuation_decision,
    )
    retryable = (
        response.error.code.value
        if response.error is not None and response.error.retryable
        else None
    )
    return _outcome(
        status=status,
        ledger_id=queued.ai_job_id,
        flow_id=flow_id,
        route_class=selected_route_class,
        binding=binding,
        response=response,
        error_type=error_type,
        context=context,
        egress_decision_id=preparation.decision_id,
        packet_digest=preparation.packet_digest,
        ticket_id=None,
        reservation_id=reservation_id,
        reason_code=preparation.reason_code,
        trigger_ids=(),
        blocked=False,
        retryable_error_code=retryable,
    )


def _authorize_context(
    *,
    context_blocks: list[dict[str, object]] | None,
    workspace_id: str | None,
    policy: EgressPolicyConfig,
    prompt: str,
) -> _ContextView:
    try:
        blocks = canonicalize_blocks(context_blocks)
    except ContextBlockError as exc:
        raise _stop(
            result="pause",
            reason_code="context_malformed",
            prompt_level=_prompt_floor_or_unknown(prompt),
        ) from exc
    if not blocks:
        return _ContextView((), "S0", None, (), (), (), ())
    raw_digest = canonical_digest(blocks)
    if workspace_id is None:
        raise _stop(
            result="pause",
            reason_code="manual_context_not_authorized",
            prompt_level=_prompt_floor_or_unknown(prompt),
            context_digest=raw_digest,
            source_count=len(blocks),
            withheld_count=len(blocks),
        )
    from app.modules.bluecad.evidence_egress import has_active_evidence_lineage

    try:
        authority = authorize_manual_context(
            workspace_id=workspace_id,
            raw_blocks=blocks,
            budget_chars=policy.max_context_chars,
        )
    except sensitivity.SensitivityPolicyError as exc:
        if has_active_evidence_lineage():
            raise _stop(
                result="deny",
                reason_code="bluecad_evidence_authority_changed",
                prompt_level=_prompt_floor_or_unknown(prompt),
                context_digest=raw_digest,
                source_count=len(blocks),
                withheld_count=len(blocks),
                detail_reason="bluecad_evidence_authority_changed",
                ai_error_type="sensitivity_policy_error",
            ) from exc
        raise _stop(
            result="pause",
            reason_code="manual_context_not_authorized",
            prompt_level=_prompt_floor_or_unknown(prompt),
            context_digest=raw_digest,
            source_count=len(blocks),
            withheld_count=len(blocks),
        ) from exc
    except ValueError as exc:
        raise _stop(
            result="pause",
            reason_code="manual_context_not_authorized",
            prompt_level=_prompt_floor_or_unknown(prompt),
            context_digest=raw_digest,
            source_count=len(blocks),
            withheld_count=len(blocks),
        ) from exc
    if authority.result != "eligible":
        if has_active_evidence_lineage():
            raise _stop(
                result="deny",
                reason_code="bluecad_evidence_authority_changed",
                prompt_level=_prompt_floor_or_unknown(prompt),
                context_digest=raw_digest,
                source_count=len(blocks),
                included_count=len(authority.included_manifest),
                withheld_count=len(authority.withheld_manifest),
                detail_reason="bluecad_evidence_authority_changed",
                ai_error_type="sensitivity_policy_error",
            )
        raise _stop(
            result="pause",
            reason_code="manual_context_not_authorized",
            prompt_level=_prompt_floor_or_unknown(prompt),
            context_digest=raw_digest,
            source_count=len(blocks),
            included_count=len(authority.included_manifest),
            withheld_count=len(authority.withheld_manifest),
        )
    return _ContextView(
        blocks=authority.blocks,
        level=authority.context_level or "S0",
        digest=authority.context_digest,
        included_manifest=authority.included_manifest,
        withheld_manifest=authority.withheld_manifest,
        budget_dropped_manifest=authority.budget_dropped_manifest,
        source_digests=authority.source_digests,
    )


def _authorize_prompt(
    *,
    user_prompt: str,
    task_kind: str,
    workspace_id: str | None,
    has_context: bool,
    context: _ContextView,
    adapters: dict[str, AIProviderAdapter],
    registry: ProviderRegistry,
):
    floor = sensitivity.deterministic_floor(user_prompt)
    if has_context and floor is None:
        raise _stop(
            result="pause",
            reason_code="prompt_classification_required",
            prompt_level="unknown",
            context_level=context.level,
            context_digest=context.digest,
            source_count=len(context.source_digests),
            included_count=len(context.included_manifest),
            withheld_count=len(context.withheld_manifest),
        )
    try:
        authority = authorize_prompt(
            raw_prompt=user_prompt,
            task_kind=task_kind,
            policy_mode=get_ai_settings().policy_mode,
            workspace_id=workspace_id,
            local_sanitizer_route=_LOCAL_SANITIZER_ROUTE,
            adapters=adapters,
            registry=registry,
        )
    except ValueError as exc:
        raise _stop(
            result="pause",
            reason_code="prompt_sanitization_required",
            prompt_level=floor or "unknown",
            context_level=context.level,
            context_digest=context.digest,
            source_count=len(context.source_digests),
            included_count=len(context.included_manifest),
            withheld_count=len(context.withheld_manifest),
        ) from exc
    if authority.result != "eligible":
        raise _stop(
            result="deny" if authority.result == "deny" else "pause",
            reason_code=authority.reason_code,
            prompt_level=authority.prompt_level or "unknown",
            context_level=context.level,
            context_digest=context.digest,
            source_count=len(context.source_digests),
            included_count=len(context.included_manifest),
            withheld_count=len(context.withheld_manifest),
        )
    from app.modules.bluecad.evidence_egress import (
        validate_authorized_structural_prompt_authority,
    )

    try:
        validate_authorized_structural_prompt_authority(authority)
    except sensitivity.SensitivityPolicyError as exc:
        raise _stop(
            result="deny",
            reason_code="bluecad_structural_prompt_authority_changed",
            prompt_level=authority.prompt_level or "unknown",
            context_level=context.level,
            context_digest=context.digest,
            source_count=len(context.source_digests),
            included_count=len(context.included_manifest),
            withheld_count=len(context.withheld_manifest),
            detail_reason="bluecad_structural_prompt_authority_changed",
            ai_error_type="sensitivity_policy_error",
        ) from exc
    return authority


def _persist_prepacket(
    *,
    stop: _PrepacketStop,
    raw_prompt_digest: str,
    ai_prompt_digest: str,
    task_kind: str,
    requested_route_class: str | None,
    route_class: str,
    binding: ProviderBinding,
    fallback_index: int,
    workspace_id: str | None,
    route_metadata: dict[str, object],
    started_at: float,
    policy: EgressPolicyConfig,
    requested_output_ceiling: int | None,
    registry: ProviderRegistry,
    flow_id: str,
    continuation_decision: ContinuationDecision | None = None,
) -> ExternalTaskOutcome:
    final_level = _prepacket_final_level(stop.prompt_level, stop.context_level)
    recorded = record_prepacket_egress_decision(
        result=stop.result,
        reason_code=stop.reason_code,
        route_class=route_class,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        fallback_index=fallback_index,
        prompt_digest=raw_prompt_digest,
        context_digest=stop.context_digest,
        prompt_level=stop.prompt_level,
        context_level=stop.context_level,
        final_level=final_level,
        source_count=stop.source_count,
        included_count=stop.included_count,
        withheld_count=stop.withheld_count,
        workspace_id=workspace_id,
        policy=policy,
    )
    status = "validation_error" if stop.result == "pause" else "config_error"
    detail_reason = stop.detail_reason or stop.reason_code
    error_type = stop.ai_error_type or stop.reason_code
    context = _ContextView(
        (),
        stop.context_level,
        stop.context_digest,
        (),
        (),
        (),
        (),
    )
    ledger_id = _terminal_job(
        flow_id=flow_id,
        task_kind=task_kind,
        requested_route_class=requested_route_class,
        route_class=route_class,
        binding=binding,
        prompt_digest=ai_prompt_digest,
        context=context,
        route_metadata={
            **route_metadata,
            "egress_decision_id": recorded.decision_id,
            "egress_reason_code": recorded.reason_code,
            "egress_safe_input_digest": recorded.safe_input_digest,
        },
        status=status,
        error_type=error_type,
        started_at=started_at,
        decision_reason=f"egress:{detail_reason}",
        blocked_reason=detail_reason,
        fallback_index=fallback_index,
        requested_output_ceiling=requested_output_ceiling,
        effective_output_ceiling=None,
        registry=registry,
        continuation_decision=continuation_decision,
    )
    return _outcome(
        status=status,
        ledger_id=ledger_id,
            flow_id=flow_id,
        route_class=route_class,
        binding=binding,
        response=None,
        error_type=error_type,
        context=context,
        egress_decision_id=recorded.decision_id,
        packet_digest=None,
        ticket_id=None,
        reservation_id=None,
        reason_code=recorded.reason_code,
        trigger_ids=(),
        blocked=True,
        context_sources_count=stop.source_count,
        decision_reason_override=f"egress:{detail_reason}",
        blocked_reason_override=detail_reason,
    )


def _terminal_job(
    *,
    flow_id: str,
    task_kind: str,
    requested_route_class: str | None,
    route_class: str,
    binding: ProviderBinding,
    prompt_digest: str,
    context: _ContextView,
    route_metadata: dict[str, object],
    status: str,
    error_type: str,
    started_at: float,
    decision_reason: str,
    blocked_reason: str | None,
    fallback_index: int,
    requested_output_ceiling: int | None,
    effective_output_ceiling: int | None,
    registry: ProviderRegistry,
    continuation_decision: ContinuationDecision | None = None,
) -> str:
    queued = create_queued_ai_job(
        task_kind=task_kind,
        requested_route_class=requested_route_class,
        selected_route_class=route_class,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        decision_reason=decision_reason,
        blocked_reason=blocked_reason,
        prompt_digest=prompt_digest,
        context_digest=context.digest,
        context_sources=(
            context_sources_manifest(list(context.blocks)) if context.blocks else None
        ),
        route_metadata=route_metadata,
    )
    finalize_external_attempt(
        flow_id=flow_id,
        ai_job_id=queued.ai_job_id,
        binding=binding,
        fallback_index=fallback_index,
        status=status,
        response=None,
        latency_ms=_elapsed_ms(started_at),
        error_type=error_type,
        adapter_invoked=False,
        dispatch_state=AIExternalDispatchState.not_started,
        requested_output_ceiling=requested_output_ceiling,
        effective_output_ceiling=effective_output_ceiling,
        outcome_reason=blocked_reason or error_type,
        registry=registry,
        continuation_decision=continuation_decision,
    )
    return queued.ai_job_id



def _outcome(
    *,
    status: str,
    ledger_id: str,
    flow_id: str,
    route_class: str,
    binding: ProviderBinding,
    response: AIResponse | None,
    error_type: str | None,
    context: _ContextView,
    egress_decision_id: str | None,
    packet_digest: str | None,
    ticket_id: str | None,
    reservation_id: str | None,
    reason_code: str | None,
    trigger_ids: tuple[str, ...],
    blocked: bool,
    retryable_error_code: str | None = None,
    context_sources_count: int | None = None,
    decision_reason_override: str | None = None,
    blocked_reason_override: str | None = None,
) -> ExternalTaskOutcome:
    decision = RoutingDecision(
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        blocked=blocked,
        blocked_reason=(
            blocked_reason_override
            if blocked_reason_override is not None
            else (reason_code if blocked else None)
        ),
        decision_reason=(
            decision_reason_override
            if decision_reason_override is not None
            else (f"egress:{reason_code}" if blocked else f"bound:{route_class}")
        ),
    )
    return ExternalTaskOutcome(
        status=status,
        ledger_id=ledger_id,
        selected_route_class=route_class,
        decision=decision,
        response=response,
        error_type=error_type,
        context_digest=context.digest,
        context_sources_count=(
            context_sources_count
            if context_sources_count is not None
            else len(context.blocks)
        ),
        retryable_error_code=retryable_error_code,
        egress_decision_id=egress_decision_id,
        egress_packet_digest=packet_digest,
        egress_ticket_id=ticket_id,
        egress_reservation_id=reservation_id,
        egress_reason_code=reason_code,
        egress_trigger_ids=trigger_ids,
        flow_id=flow_id,
    )


def _release_before_network_strict(*, reservation_id: str, ai_job_id: str) -> None:
    try:
        reconcile_reserved_attempt(
            reservation_id,
            ai_job_id=ai_job_id,
            network_attempt=False,
        )
        return
    except Exception as exc:
        with open_sqlite_connection() as connection:
            row = connection.execute(
                "SELECT state FROM egress_budget_reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
        if row is not None and row["state"] in _TERMINAL_RESERVATION_STATES:
            return
        raise EgressSpineStateError(
            "failed-before-network reservation was not released"
        ) from exc


def _load_packet(packet_json: str) -> dict[str, object]:
    try:
        packet = json.loads(packet_json)
    except json.JSONDecodeError as exc:
        raise EgressContractError("persisted egress packet is malformed") from exc
    if not isinstance(packet, dict) or set(packet) != {"prompt", "context_blocks"}:
        raise EgressContractError("persisted egress packet has an unexpected shape")
    prompt = packet["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise EgressContractError("persisted egress prompt is invalid")
    if not isinstance(packet["context_blocks"], list):
        raise EgressContractError("persisted egress context is invalid")
    return {
        "prompt": prompt,
        "context_blocks": canonicalize_blocks(packet["context_blocks"]),
    }


def _binding_chain(
    *,
    route_class: str,
    primary: ProviderBinding,
    bindings: dict[str, ProviderBinding] | None,
    registry: ProviderRegistry,
) -> list[ProviderBinding]:
    if bindings is not None:
        return [primary]
    configured = registry.fallback_chains.get(route_class)
    if not configured:
        return [primary]
    result: list[ProviderBinding] = []
    for item in configured:
        provider = registry.providers[item.provider_id]
        model = registry.models[(item.provider_id, item.model_id)]
        result.append(
            ProviderBinding(
                route_class=route_class,
                provider_id=item.provider_id,
                model_id=item.model_id,
                requires_network=provider.requires_network,
                max_output_tokens=model.max_output_tokens,
                execution_class=provider.execution_class,
                context_window_tokens=model.context_window_tokens,
            )
        )
    return result


def _route_metadata(
    *,
    route_class: str,
    fallback_index: int,
    binding: ProviderBinding,
    prior_retryable_error_code: str | None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "fallback_attempt_index": fallback_index,
        "fallback_chain_route": route_class,
        "fallback_model_id": binding.model_id,
        "fallback_provider_id": binding.provider_id,
    }
    if prior_retryable_error_code is not None:
        result["prior_retryable_error_code"] = prior_retryable_error_code
    return result


def _response_status(response: AIResponse) -> tuple[str, str | None]:
    if response.error is None and response.text is not None:
        return "success", None
    return (
        "provider_error",
        response.error.code.value if response.error is not None else "empty_response",
    )


def _stop(
    *,
    result: str,
    reason_code: str,
    prompt_level: str,
    context_level: str = "unknown",
    context_digest: str | None = None,
    source_count: int = 0,
    included_count: int = 0,
    withheld_count: int = 0,
    detail_reason: str | None = None,
    ai_error_type: str | None = None,
) -> _PrepacketStop:
    return _PrepacketStop(
        result=result,
        reason_code=reason_code,
        prompt_level=prompt_level,
        context_level=context_level,
        context_digest=context_digest,
        source_count=source_count,
        included_count=included_count,
        withheld_count=withheld_count,
        detail_reason=detail_reason,
        ai_error_type=ai_error_type,
    )


def _prompt_floor_or_unknown(prompt: str) -> str:
    return sensitivity.deterministic_floor(prompt) or "unknown"


def _prepacket_final_level(prompt_level: str, context_level: str) -> str:
    if "S4" in {prompt_level, context_level}:
        return "S4"
    if "unknown" in {prompt_level, context_level}:
        return "unknown"
    return max((prompt_level, context_level), key=_LEVEL_RANK.__getitem__)


def _default_task_prompt(task_kind: str, blocks: list[dict], prompt: str) -> str:
    del task_kind
    return assemble_prompt(blocks, prompt)


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)
