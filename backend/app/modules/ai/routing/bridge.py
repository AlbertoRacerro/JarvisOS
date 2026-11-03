from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.packs import default_workspace_slug
from app.modules.ai.context_builder import (
    DEFAULT_CONTEXT_BUDGET_CHARS,
    canonical_digest,
    context_sources_manifest,
)
from app.modules.ai.costs import build_escalation_proposal
from app.modules.ai.execution import AiTaskOutcome
from app.modules.ai.models import AITaskRunRequest, AITaskRunResponse
from app.modules.ai.routing.capability_route_matrix import (
    CAPABILITY_CODING,
    CAPABILITY_DEEP_REASONING,
    CAPABILITY_GENERAL_REASONING,
    CAPABILITY_HEAVY_CODING,
    CAPABILITY_SIMPLE,
    CONTEXT_LEVEL_DEEP,
    CONTEXT_LEVEL_LIGHT,
    CONTEXT_LEVEL_NONE,
    CONTEXT_LEVEL_STANDARD,
    context_budget_chars_for_route_level,
    local_route_for_capability,
    route_supported_context_level,
)
from app.modules.ai.routing.decision import decide_router_policy
from app.modules.events.service import utc_now
from app.modules.local_ai.classification.adapter import ClassificationAdapterConfig, LocalGemmaClassificationAdapter
from app.modules.local_ai.classification.contracts import (
    LOW_CONFIDENCE_THRESHOLD,
    AllowedNextStep,
    ClassificationFailureCode,
    ClassificationInput,
    ClassificationResultSource,
    ClassificationServiceResult,
    ClassificationSource,
    ComplexityHint,
    ProjectArea,
    SensitivityHint,
    TaskType,
    make_advisory_hints,
    make_output,
)
from app.modules.local_ai.classification.service import classify_text

AUTO_ROUTE_CLASS = "auto"
AUTO_CLASSIFICATION_MODEL = "qwen3:8b"

CONTROL_NEEDS_CLARIFICATION = "needs_clarification"
CONTROL_BLOCKED = "blocked"
CONTROL_PROPOSED_EXTERNAL = "proposed_external"

SOURCE_SELECTION_BUDGET_ONLY = "budget_only"
SOURCE_SELECTION_NOT_REQUESTED = "not_requested"

RunAiTaskFunc = Callable[..., AiTaskOutcome]
ClassifyFunc = Callable[..., ClassificationServiceResult]


@dataclass(frozen=True)
class AutoDecisionBundle:
    router_input: dict
    decision: dict
    classification_result: ClassificationServiceResult
    capability: str
    local_route_class: str
    context_decision: dict[str, object]
    control_status: str | None = None
    confirmation_payload: dict[str, object] | None = None


def build_auto_router_input(
    request: AITaskRunRequest,
    classification_result: ClassificationServiceResult | None = None,
) -> dict:
    classification = classification_result.classification if classification_result is not None else None
    sensitivity_hint = _router_sensitivity_hint(request, classification)
    sensitivity = _router_sensitivity(sensitivity_hint)
    return {
        "message_text": request.prompt,
        "phase_a_signals": {
            "contains_secret_or_credential": sensitivity_hint == SensitivityHint.secret,
            "contains_raw_private_or_ip_sensitive_context": sensitivity == "sensitive",
            "mentions_external_provider_or_upload_intent": False,
            "external_provider_allowed": False,
            "clarification_required": False,
            "hard_reason_codes": ["low_risk"],
            "sensitivity_bucket_proposal": sensitivity,
            "requires_manual_review": False,
            "source_policy_for_future_retrieval": "not_applicable",
            "allowed_future_retrieval_behavior": "none",
        },
        "phase_b_soft_proposal": {
            "project_bucket": _project_bucket(classification.project_area if classification is not None else ProjectArea.unknown),
            "primary_domain": "general",
            "domain_tags": ["answer"],
            "soft_reason_code": "contextual_summary",
            "suggested_followup_question": "",
        },
        "router_hint": {
            # RouterPolicy is the local execution permission gate here. Semantic
            # task/capability is carried separately in Auto metadata and matrix.
            "task_type": "answer",
            "complexity": "low",
            "domain": "general",
            "confidence": "high",
            "estimated_tokens": 200,
            "needs_reasoning": False,
            "needs_current_info": False,
            "needs_file_context": False,
            "needs_code_execution": False,
            "needs_scientific_depth": False,
        },
        "action_hint": {
            "requested_action_type": "answer",
            "modifies_state": False,
            "side_effect_level": "none",
            "reversibility": "reversible",
            "environment_type": "chat",
            "state_scope": "none",
            "needs_terminal": False,
            "needs_file_write": False,
            "needs_memory_write": False,
            "needs_provider_call": False,
            "confidence": "high",
        },
        "user_policy": {
            "external_routing_enabled": False,
            "external_requires_confirmation": True,
            "allow_persistent_auto_allow": False,
        },
        "provider_policy": {
            "allowed_provider_tiers": ["LOCAL_ONLY", "LOCAL_FAST"],
            "blocked_provider_tiers": ["CHEAP_EXTERNAL", "SCIENTIFIC_MEDIUM", "FRONTIER"],
        },
        "budget_policy": {
            "max_tier": "LOCAL_FAST",
            "max_tokens": request.max_tokens or 2048,
            "require_confirmation_above_tier": "CHEAP_EXTERNAL",
        },
        "context_metadata": {
            "attached_files_present": bool(request.context_blocks),
            "conversation_context_available": True,
        },
    }


def build_auto_decision(
    request: AITaskRunRequest,
    *,
    now: str | None = None,
    classifier_func: ClassifyFunc | None = None,
) -> tuple[dict, dict]:
    bundle = build_auto_decision_bundle(request, now=now, classifier_func=classifier_func)
    return bundle.router_input, bundle.decision


def build_auto_decision_bundle(
    request: AITaskRunRequest,
    *,
    now: str | None = None,
    classifier_func: ClassifyFunc | None = None,
) -> AutoDecisionBundle:
    classification_result = classify_auto_request(request, classifier_func=classifier_func)
    classification = classification_result.classification
    capability = capability_from_classification(classification)
    local_route_class = local_route_for_capability(capability)
    control_status = control_status_from_classification(classification)
    confirmation_payload = _confirmation_payload(classification_result, capability) if control_status == CONTROL_PROPOSED_EXTERNAL else None
    router_input = build_auto_router_input(request, classification_result)
    if control_status is not None:
        decision = _control_decision(control_status, classification_result, capability)
    else:
        decision = decide_router_policy(router_input, now=now)
    context_decision = context_decision_from_classification(
        request,
        classification_result,
        local_route_class=local_route_class,
        capability=capability,
    )
    return AutoDecisionBundle(
        router_input=router_input,
        decision=decision,
        classification_result=classification_result,
        capability=capability,
        local_route_class=local_route_class,
        context_decision=context_decision,
        control_status=control_status,
        confirmation_payload=confirmation_payload,
    )


def classify_auto_request(
    request: AITaskRunRequest,
    *,
    classifier_func: ClassifyFunc | None = None,
) -> ClassificationServiceResult:
    classifier = classifier_func or classify_text
    try:
        classification_input = ClassificationInput(
            text=request.prompt,
            source=ClassificationSource.user_prompt,
            metadata={"route_class": AUTO_ROUTE_CLASS, "task_kind": request.task_kind},
        )
        adapter = LocalGemmaClassificationAdapter(ClassificationAdapterConfig(model_name=AUTO_CLASSIFICATION_MODEL))
        return classifier(classification_input, adapter=adapter)
    except Exception:
        return _fallback_classification()


def capability_from_classification(classification) -> str:
    if classification.task_type in {TaskType.code_change, TaskType.bug_report}:
        return CAPABILITY_HEAVY_CODING if classification.complexity_hint == ComplexityHint.high else CAPABILITY_CODING
    if classification.task_type in {TaskType.engineering_question, TaskType.project_planning}:
        return CAPABILITY_GENERAL_REASONING
    if classification.task_type in {TaskType.documentation, TaskType.local_note, TaskType.personal_question, TaskType.unknown}:
        return CAPABILITY_SIMPLE
    return CAPABILITY_SIMPLE


def control_status_from_classification(classification) -> str | None:
    if classification.sensitivity_hint == SensitivityHint.secret:
        return CONTROL_BLOCKED
    if classification.task_type == TaskType.external_api_request:
        return CONTROL_PROPOSED_EXTERNAL
    if classification.task_type == TaskType.ambiguous:
        return CONTROL_NEEDS_CLARIFICATION
    if classification.task_type in {TaskType.unsafe_tool_request, TaskType.overbroad_orchestration_request}:
        return CONTROL_BLOCKED
    if classification.allowed_next_step == AllowedNextStep.ask_clarification:
        return CONTROL_NEEDS_CLARIFICATION
    if classification.allowed_next_step == AllowedNextStep.human_review and classification.task_type in {
        TaskType.unsafe_tool_request,
        TaskType.overbroad_orchestration_request,
    }:
        return CONTROL_BLOCKED
    return None


def context_decision_from_classification(
    request: AITaskRunRequest,
    classification_result: ClassificationServiceResult,
    *,
    local_route_class: str | None = None,
    capability: str | None = None,
) -> dict[str, object]:
    classification = classification_result.classification
    route_class = local_route_class or local_route_for_capability(capability or CAPABILITY_SIMPLE)
    fallback_or_low_confidence = (
        classification_result.source == ClassificationResultSource.fallback
        or classification.confidence < LOW_CONFIDENCE_THRESHOLD
    )
    manual_context_chars = _serialized_context_chars(request.context_blocks or [])
    requested_level = _classifier_requested_context_level(classification, fallback_or_low_confidence)
    effective_requested_level = CONTEXT_LEVEL_NONE
    if not request.include_project_context:
        reason = "user_context_permission_off"
    elif not classification.needs_context:
        reason = "classifier_did_not_request_project_context"
    elif fallback_or_low_confidence:
        effective_requested_level = CONTEXT_LEVEL_LIGHT
        reason = "classification_fallback_uses_conservative_context"
    elif classification.project_area == ProjectArea.project:
        effective_requested_level = requested_level
        reason = f"classifier_requested_workspace_{requested_level}_context"
    else:
        reason = "classifier_context_not_workspace_relevant"

    context_level, budget_reason = route_supported_context_level(route_class, effective_requested_level)
    context_budget_chars = context_budget_chars_for_route_level(
        route_class,
        context_level,
        max_budget_chars=DEFAULT_CONTEXT_BUDGET_CHARS,
    )
    workspace_budget_chars = max(0, context_budget_chars - manual_context_chars)
    final_include_project_context = context_level != CONTEXT_LEVEL_NONE and workspace_budget_chars > 0
    if context_level != CONTEXT_LEVEL_NONE and workspace_budget_chars == 0:
        reason = "manual_context_exhausted_workspace_budget"
        budget_reason = "workspace_context_budget_exhausted_by_manual_context"
    source_selection_status = (
        SOURCE_SELECTION_BUDGET_ONLY if final_include_project_context else SOURCE_SELECTION_NOT_REQUESTED
    )
    return {
        "context_permission": request.include_project_context,
        "context_level": context_level,
        "requested_context_level": requested_level,
        "effective_requested_context_level": effective_requested_level,
        "context_budget_chars": context_budget_chars,
        "workspace_context_budget_chars": workspace_budget_chars if final_include_project_context else 0,
        "manual_context_chars": manual_context_chars,
        "context_budget_reason": budget_reason,
        "context_used": False,
        "source_selection_status": source_selection_status,
        "classifier_needs_context": classification.needs_context,
        "project_area": classification.project_area.value,
        "user_context_permission": request.include_project_context,
        "final_include_project_context": final_include_project_context,
        "context_decision_reason": reason,
        "classification_source": classification_result.source.value,
        "classification_confidence": classification.confidence,
        "selected_local_route_class": route_class,
        "capability": capability or CAPABILITY_SIMPLE,
        "manual_context_blocks_count": len(request.context_blocks or []),
        "workspace_context_blocks_count": 0,
        "source_selection_note": "budget_only_no_retrieval_intelligence",
    }


def resolve_bridge_outcome_from_decision(
    *,
    request: AITaskRunRequest,
    decision: dict,
    run_ai_task_func: RunAiTaskFunc | None = None,
    context_blocks: list[dict] | None = None,
    context_build_error: str | None = None,
    requested_workspace_id: str | None = None,
    selected_auto_route_class: str | None = None,
    auto_metadata: dict[str, object] | None = None,
    control_status: str | None = None,
    confirmation_payload: dict[str, object] | None = None,
) -> AITaskRunResponse:
    from app.modules.ai import execution

    runner = run_ai_task_func or execution.run_ai_task
    if _is_executable_auto_local(decision):
        outcome = runner(
            user_prompt=request.prompt,
            task_kind=request.task_kind,
            route_class=selected_auto_route_class or local_route_for_capability(CAPABILITY_SIMPLE),
            context_blocks=context_blocks,
            max_output_tokens=request.max_tokens,
            context_build_error=context_build_error,
        )
        return _response_from_outcome(
            request=request,
            outcome=outcome,
            requested_workspace_id=requested_workspace_id,
            auto_metadata=auto_metadata,
        )

    started = time.perf_counter()
    status = control_status or _control_status(decision)
    ledger_id = _write_auto_control_job(
        status=status,
        task_kind=request.task_kind,
        prompt=request.prompt,
        decision=decision,
        context_blocks=context_blocks,
        latency_ms=int((time.perf_counter() - started) * 1000),
        auto_metadata=auto_metadata,
    )
    escalation_proposal = None
    if status == CONTROL_PROPOSED_EXTERNAL and auto_metadata and auto_metadata.get("capability_exceeds_local") is True:
        sensitivity_hint = str(auto_metadata.get("classification", {}).get("sensitivity_hint", "unknown"))
        escalation_proposal = build_escalation_proposal(
            prompt=request.prompt,
            proposal_ledger_id=ledger_id,
            max_output_tokens=request.max_tokens,
            sensitivity_hint=sensitivity_hint,
        )
        _attach_escalation_proposal_to_job(ledger_id, escalation_proposal)
    return AITaskRunResponse(
        status=status,
        ledger_id=ledger_id,
        selected_route_class=AUTO_ROUTE_CLASS,
        decision_reason=_decision_reason(decision),
        blocked_reason=_blocked_reason(decision, status),
        response_text=None,
        provider_id=None,
        model_id=None,
        usage=None,
        error_type=status,
        include_project_context=bool(auto_metadata.get("context_decision", {}).get("final_include_project_context")) if auto_metadata else request.include_project_context,
        workspace_id=requested_workspace_id,
        context_digest=canonical_digest(context_blocks) if context_blocks else None,
        context_sources_count=len(context_sources_manifest(context_blocks)) if context_blocks else 0,
        auto_metadata=auto_metadata,
        confirmation_payload=confirmation_payload,
        escalation_proposal=escalation_proposal,
    )


def run_auto_task(
    request: AITaskRunRequest,
    *,
    run_ai_task_func: RunAiTaskFunc | None = None,
    classifier_func: ClassifyFunc | None = None,
) -> AITaskRunResponse:
    bundle = build_auto_decision_bundle(request, classifier_func=classifier_func)
    manual_blocks = list(request.context_blocks or [])
    context_blocks = manual_blocks
    context_build_error = None
    executable_local = bundle.control_status is None and _is_executable_auto_local(bundle.decision)
    requested_workspace_id = (
        request.workspace_id or default_workspace_slug()
        if executable_local and bundle.context_decision["final_include_project_context"]
        else None
    )
    if not executable_local and bundle.context_decision["final_include_project_context"]:
        bundle.context_decision["final_include_project_context"] = False
        bundle.context_decision["workspace_context_budget_chars"] = 0
        bundle.context_decision["source_selection_status"] = SOURCE_SELECTION_NOT_REQUESTED
        bundle.context_decision["workspace_context_skipped_reason"] = "auto_execution_not_local_safe"
    if executable_local and bundle.context_decision["final_include_project_context"]:
        context_blocks, context_build_error = _build_auto_context(
            manual_blocks,
            requested_workspace_id,
            budget_chars=int(bundle.context_decision["workspace_context_budget_chars"]),
        )
    workspace_context_blocks_count = max(0, len(context_blocks) - len(manual_blocks))
    bundle.context_decision["manual_context_blocks_count"] = len(manual_blocks)
    bundle.context_decision["workspace_context_blocks_count"] = workspace_context_blocks_count
    bundle.context_decision["context_used"] = workspace_context_blocks_count > 0
    if context_build_error is not None:
        bundle.context_decision["context_build_error"] = context_build_error
    control_status = bundle.control_status
    decision = bundle.decision
    if control_status is None and bundle.capability == CAPABILITY_DEEP_REASONING:
        control_status = CONTROL_PROPOSED_EXTERNAL
        decision = _control_decision(control_status, bundle.classification_result, bundle.capability)
    auto_metadata = auto_metadata_from_bundle(bundle)
    if control_status == CONTROL_PROPOSED_EXTERNAL:
        auto_metadata["control_status"] = CONTROL_PROPOSED_EXTERNAL
    return resolve_bridge_outcome_from_decision(
        request=request,
        decision=decision,
        run_ai_task_func=run_ai_task_func,
        context_blocks=context_blocks,
        context_build_error=context_build_error,
        requested_workspace_id=requested_workspace_id,
        selected_auto_route_class=bundle.local_route_class,
        auto_metadata=auto_metadata,
        control_status=control_status,
        confirmation_payload=bundle.confirmation_payload,
    )


def auto_metadata_from_bundle(bundle: AutoDecisionBundle) -> dict[str, object]:
    classification = bundle.classification_result.classification
    diagnostics = bundle.classification_result.diagnostics
    capability_exceeds_local = bundle.capability == CAPABILITY_DEEP_REASONING
    return {
        "capability_exceeds_local": capability_exceeds_local,
        "classification": {
            "task_type": classification.task_type.value,
            "project_area": classification.project_area.value,
            "complexity_hint": classification.complexity_hint.value,
            "needs_context": classification.needs_context,
            "sensitivity_hint": classification.sensitivity_hint.value,
            "allowed_next_step": classification.allowed_next_step.value,
            "confidence": classification.confidence,
            "source": bundle.classification_result.source.value,
            "model_name": diagnostics.model_name if diagnostics is not None else AUTO_CLASSIFICATION_MODEL,
            "fallback_reasons": [reason.value for reason in bundle.classification_result.fallback_reasons],
            "deterministic_reasons": bundle.classification_result.deterministic_reasons,
        },
        "capability": {
            "row": bundle.capability,
            "local_route_class": bundle.local_route_class,
            "capability_exceeds_local": capability_exceeds_local,
            "note": "would_benefit_from_external" if bundle.capability == CAPABILITY_DEEP_REASONING else None,
        },
        "context_decision": bundle.context_decision,
        "control_status": bundle.control_status,
    }


def _build_auto_context(
    manual_blocks: list[dict],
    requested_workspace_id: str | None,
    *,
    budget_chars: int,
) -> tuple[list[dict], str | None]:
    from app.modules.ai.context_builder import build_workspace_context_bundle

    if budget_chars <= 0:
        return manual_blocks, "workspace_context_budget_exhausted"
    try:
        bundle = build_workspace_context_bundle(requested_workspace_id or default_workspace_slug(), budget_chars=budget_chars)
    except Exception as exc:
        return manual_blocks, f"workspace_context_build_failed: {type(exc).__name__}"
    return manual_blocks + bundle.blocks, None


def _requested_context_level(classification) -> str:
    if _classification_requests_deep_context(classification):
        return CONTEXT_LEVEL_DEEP
    if classification.task_type == TaskType.project_planning:
        return CONTEXT_LEVEL_STANDARD
    if classification.task_type == TaskType.engineering_question:
        if classification.complexity_hint in {ComplexityHint.medium, ComplexityHint.high}:
            return CONTEXT_LEVEL_STANDARD
        return CONTEXT_LEVEL_LIGHT
    if classification.task_type in {TaskType.code_change, TaskType.bug_report}:
        return CONTEXT_LEVEL_STANDARD
    if classification.complexity_hint in {ComplexityHint.medium, ComplexityHint.high}:
        return CONTEXT_LEVEL_STANDARD
    return CONTEXT_LEVEL_LIGHT


def _classifier_requested_context_level(
    classification,
    fallback_or_low_confidence: bool,
) -> str:
    if not classification.needs_context:
        return CONTEXT_LEVEL_NONE
    if fallback_or_low_confidence:
        return CONTEXT_LEVEL_LIGHT
    if classification.project_area != ProjectArea.project:
        return CONTEXT_LEVEL_NONE
    return _requested_context_level(classification)


def _classification_requests_deep_context(classification) -> bool:
    if classification.confidence < LOW_CONFIDENCE_THRESHOLD:
        return False
    if not classification.needs_context or classification.project_area != ProjectArea.project:
        return False
    return (
        classification.task_type == TaskType.project_planning
        and classification.complexity_hint == ComplexityHint.high
        and classification.allowed_next_step == AllowedNextStep.request_bounded_context
    )


def _serialized_context_chars(context_blocks: list[dict]) -> int:
    if not context_blocks:
        return 0
    return len(json.dumps(context_blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _is_executable_auto_local(decision: dict) -> bool:
    return _is_auto_local_safe(decision)


def _is_auto_local_safe(decision: dict) -> bool:
    """Return true for Auto decisions that are safe to answer with a local model.

    Auto is a local-only slice. A propose_only decision with no external target
    and no side effects is treated as a safe local answer, not as an external
    proposal to execute.
    """

    return all(
        (
            decision.get("route_action") in {"answer_local", "route_local"},
            decision.get("route_tier") in {"LOCAL_FAST", "LOCAL_ONLY"},
            decision.get("proposed_external_target") is None,
            decision.get("response_allowed_now") is True,
            decision.get("external_allowed") is False,
            decision.get("provider_call_allowed_now") is False,
            decision.get("external_network_allowed_now") is False,
            decision.get("tool_execution_allowed_now") is False,
            decision.get("state_change_allowed_now") is False,
            decision.get("modifies_state") is False,
            decision.get("side_effect_level") == "none",
            decision.get("environment_type") == "chat",
            decision.get("allowed_execution_mode") in {"answer_only", "propose_only"},
        )
    )


def _control_status(decision: dict) -> str:
    if decision.get("proposed_external_target") is not None:
        return CONTROL_PROPOSED_EXTERNAL
    route_action = decision.get("route_action")
    if route_action == "ask_user_confirm":
        return "needs_confirmation"
    if route_action == "ask_clarification":
        return CONTROL_NEEDS_CLARIFICATION
    if route_action == "blocked":
        return CONTROL_BLOCKED
    return "propose_only"


def _decision_reason(decision: dict) -> str:
    reason_codes = decision.get("reason_codes")
    if isinstance(reason_codes, list) and reason_codes:
        return ",".join(str(reason) for reason in reason_codes)
    route_action = decision.get("route_action")
    route_tier = decision.get("route_tier")
    return f"auto:{route_action}:{route_tier}"


def _blocked_reason(decision: dict, status: str) -> str:
    if status == CONTROL_PROPOSED_EXTERNAL:
        return "auto_external_proposal_refused"
    route_action = decision.get("route_action")
    if isinstance(route_action, str) and route_action:
        return route_action
    return status


def _response_from_outcome(
    *,
    request: AITaskRunRequest,
    outcome: AiTaskOutcome,
    requested_workspace_id: str | None,
    auto_metadata: dict[str, object] | None,
) -> AITaskRunResponse:
    response = outcome.response
    return AITaskRunResponse(
        status=outcome.status,
        ledger_id=outcome.ledger_id,
        selected_route_class=outcome.selected_route_class,
        decision_reason=outcome.decision.decision_reason,
        blocked_reason=outcome.decision.blocked_reason,
        response_text=response.text if response is not None else None,
        provider_id=response.provider_id if response is not None else outcome.decision.provider_id,
        model_id=response.model_id if response is not None else outcome.decision.model_id,
        usage=response.usage if response is not None else None,
        error_type=outcome.error_type,
        include_project_context=bool(auto_metadata.get("context_decision", {}).get("final_include_project_context")) if auto_metadata else request.include_project_context,
        workspace_id=requested_workspace_id,
        context_digest=outcome.context_digest,
        context_sources_count=outcome.context_sources_count,
        auto_metadata=auto_metadata,
    )


def _write_auto_control_job(
    *,
    status: str,
    task_kind: str,
    prompt: str,
    decision: dict,
    context_blocks: list[dict] | None,
    latency_ms: int,
    auto_metadata: dict[str, object] | None,
) -> str:
    job_id = str(uuid4())
    context_digest = canonical_digest(context_blocks) if context_blocks else None
    context_sources = context_sources_manifest(context_blocks) if context_blocks else None
    route_reason_json = json.dumps(
        {
            "decision_reason": _decision_reason(decision),
            "route_action": decision.get("route_action"),
            "route_tier": decision.get("route_tier"),
            "permissions": {
                "response_allowed_now": decision.get("response_allowed_now"),
                "external_allowed": decision.get("external_allowed"),
                "provider_call_allowed_now": decision.get("provider_call_allowed_now"),
                "external_network_allowed_now": decision.get("external_network_allowed_now"),
                "tool_execution_allowed_now": decision.get("tool_execution_allowed_now"),
                "state_change_allowed_now": decision.get("state_change_allowed_now"),
                "allowed_execution_mode": decision.get("allowed_execution_mode"),
            },
            "reason_codes": decision.get("reason_codes"),
            "blocked_reason": _blocked_reason(decision, status),
            "auto_metadata": auto_metadata,
        },
        sort_keys=True,
    )
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class, selected_route_class,
                provider_id, model_id, route_reason_json, prompt_digest, context_digest,
                context_sources_json, output_digest, input_tokens, output_tokens, cost_estimate,
                latency_ms, error_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                utc_now(),
                status,
                task_kind,
                AUTO_ROUTE_CLASS,
                AUTO_ROUTE_CLASS,
                None,
                None,
                route_reason_json,
                canonical_digest({"prompt": prompt}),
                context_digest,
                json.dumps(context_sources) if context_sources else None,
                None,
                None,
                None,
                None,
                latency_ms,
                status,
            ),
        )
        connection.commit()
    return job_id


def _fallback_classification() -> ClassificationServiceResult:
    output = make_output(
        task_type=TaskType.unknown,
        project_area=ProjectArea.unknown,
        complexity_hint=ComplexityHint.unknown,
        needs_context=False,
        sensitivity_hint=SensitivityHint.unknown,
        allowed_next_step=AllowedNextStep.answer_locally,
        confidence=0,
        refusal_or_uncertainty_reason="Classification failed; using conservative local fallback.",
    )
    return ClassificationServiceResult(
        classification=output,
        advisory_hints=make_advisory_hints(output),
        source=ClassificationResultSource.fallback,
        model_output_accepted=False,
        fallback_reasons=[ClassificationFailureCode.unknown],
    )


def _control_decision(
    status: str,
    classification_result: ClassificationServiceResult,
    capability: str,
) -> dict:
    classification = classification_result.classification
    route_action = {
        CONTROL_NEEDS_CLARIFICATION: "ask_clarification",
        CONTROL_BLOCKED: "blocked",
        CONTROL_PROPOSED_EXTERNAL: "ask_user_confirm",
    }[status]
    return {
        "route_action": route_action,
        "route_tier": "USER_CONFIRM" if status != CONTROL_BLOCKED else "BLOCKED",
        "provider_candidate": "none",
        "proposed_external_target": "external:scientific_medium" if status == CONTROL_PROPOSED_EXTERNAL else None,
        "response_allowed_now": status != CONTROL_BLOCKED,
        "external_allowed": False,
        "provider_call_allowed_now": False,
        "external_network_allowed_now": False,
        "tool_execution_allowed_now": False,
        "state_change_allowed_now": False,
        "allowed_execution_mode": "blocked" if status == CONTROL_BLOCKED else "propose_only",
        "modifies_state": False,
        "side_effect_level": "none",
        "environment_type": "chat",
        "reason_codes": [
            f"auto_{status}",
            f"classification_task:{classification.task_type.value}",
            f"capability:{capability}",
        ],
    }


def _confirmation_payload(
    classification_result: ClassificationServiceResult,
    capability: str,
) -> dict[str, object]:
    classification = classification_result.classification
    return {
        "scope": "external_provider_request_detected",
        "target": "external:scientific_medium",
        "classification_task_type": classification.task_type.value,
        "capability": capability,
        "message": "Auto detected an external provider/API request. Auto is local-only in this slice.",
    }


def _project_bucket(project_area: ProjectArea) -> str:
    return project_area.value


def _router_sensitivity(sensitivity: SensitivityHint) -> str:
    if sensitivity == SensitivityHint.public:
        return "public"
    if sensitivity == SensitivityHint.internal:
        return "internal"
    if sensitivity in {SensitivityHint.confidential, SensitivityHint.sensitive_ip, SensitivityHint.secret}:
        return "sensitive"
    return "unknown"


def _router_sensitivity_hint(request: AITaskRunRequest, classification) -> SensitivityHint:
    if classification is not None and classification.sensitivity_hint in {
        SensitivityHint.confidential,
        SensitivityHint.sensitive_ip,
        SensitivityHint.secret,
    }:
        return classification.sensitivity_hint
    if request.include_project_context:
        return SensitivityHint.internal
    return SensitivityHint.unknown


def _attach_escalation_proposal_to_job(job_id: str, escalation_proposal: dict[str, object]) -> None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT route_reason_json FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return
        route_reason = json.loads(row["route_reason_json"])
        # The spine contract is "the ledger stores only digests + metadata, never
        # prompt/output content" (see execution.py). Escalation is exactly the
        # sensitive path, so persist a redacted proposal: replace the raw prompt with
        # a digest. The full outbound_text stays only in the response payload for the
        # UI card. Provider/model/cost columns are also left unset so a non-executing
        # proposal row is never misread as a real external call or summed as spend.
        ledger_proposal = dict(escalation_proposal)
        raw_outbound = ledger_proposal.pop("outbound_text", None)
        if raw_outbound is not None:
            ledger_proposal["outbound_text_digest"] = canonical_digest({"prompt": raw_outbound})
        route_reason["escalation_proposal"] = ledger_proposal
        connection.execute(
            "UPDATE ai_jobs SET route_reason_json = ? WHERE id = ?",
            (json.dumps(route_reason, sort_keys=True), job_id),
        )
        connection.commit()
