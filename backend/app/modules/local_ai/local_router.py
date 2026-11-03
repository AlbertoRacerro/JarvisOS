"""Local-only selection, atomic admission, governed execution and lease evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import perf_counter
from uuid import uuid4

from app.modules.ai.contracts import AIProviderAdapter
from app.modules.ai.execution import AiTaskOutcome, run_ai_task
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import ProviderRegistry, load_default_provider_registry
from app.modules.local_ai.decision_contracts import (
    DecisionOutputSpec,
    DecisionRequest,
    DecisionResult,
    validate_decision_result,
)
from app.modules.local_ai.decision_service import DecisionModel, LocalCandidate, RuleDecisionModel
from app.modules.local_ai.resource_arbiter import InProcessResourceArbiter, ResourceCapacityError, get_resource_arbiter
from app.modules.local_ai.resource_contracts import (
    ResourceAmounts,
    ResourceLeaseError,
    ResourceReservationRequest,
    RuntimeResourceSnapshot,
)
from app.modules.local_ai.runtime.status import get_local_ai_runtime_status


@dataclass(frozen=True)
class LocalRouteAttempt:
    decision: DecisionResult
    lease_id: str | None
    ledger_id: str | None
    status: str


@dataclass(frozen=True)
class LocalRouteOutcome:
    status: str
    outcome: AiTaskOutcome | None
    attempts: tuple[LocalRouteAttempt, ...]


_LATENCY_LOCK = Lock()
_OBSERVED_LATENCY_MS: dict[str, int] = {}


def local_candidates(registry: ProviderRegistry | None = None) -> tuple[LocalCandidate, ...]:
    registry = registry or load_default_provider_registry()
    candidates: list[LocalCandidate] = []
    for route, binding in registry.bindings.items():
        if binding.execution_class != "local_compute" or binding.requires_network or not route.startswith("local:"):
            continue
        model = registry.models.get((binding.provider_id, binding.model_id))
        if model is None or binding.context_window_tokens is None:
            continue
        capability = route.removeprefix("local:")
        # Keep route identity: two route classes may intentionally bind the same
        # model, and execution/evidence must retain the requested route.
        candidates.append(
            LocalCandidate(
                route,
                model.provider_model_name,
                binding.context_window_tokens,
                frozenset({capability}),
                latency_ms=_OBSERVED_LATENCY_MS.get(model.provider_model_name),
            )
        )
    return tuple(candidates)


def with_cold_load_footprint(
    candidate: LocalCandidate, installed_sizes: Mapping[str, int], snapshot: RuntimeResourceSnapshot
) -> LocalCandidate:
    """Claim the installed model size for a model that is not loaded yet.

    Without a numeric claim the arbiter must refuse a cold model, so the size
    on disk (a lower bound of what loading needs) becomes the reservation: VRAM
    on the first observed GPU, otherwise RAM. Loaded models and explicit claims
    are left unchanged.
    """
    if candidate.ram_bytes or candidate.vram_bytes:
        return candidate
    if any(model.name == candidate.model_name for model in snapshot.loaded_models):
        return candidate
    size = installed_sizes.get(candidate.model_name)
    if not size:
        return candidate
    if snapshot.gpus:
        return replace(candidate, vram_bytes=size, gpu_index=snapshot.gpus[0].index)
    return replace(candidate, ram_bytes=size)


def _safe_binding(candidate: LocalCandidate, registry: ProviderRegistry) -> ProviderBinding:
    binding = registry.bindings.get(candidate.candidate_id)
    model = registry.models.get((binding.provider_id, binding.model_id)) if binding is not None else None
    provider = registry.providers.get(binding.provider_id) if binding is not None else None
    if (
        binding is None
        or model is None
        or provider is None
        or binding.execution_class != "local_compute"
        or provider.execution_class != "local_compute"
        or binding.requires_network
        or provider.requires_network
        or provider.base_url is not None
        or provider.api_key_ref is not None
        or provider.kind != "local"
        or not binding.route_class.startswith("local:")
        or candidate.model_name != model.provider_model_name
        or binding.context_window_tokens != candidate.context_window_tokens
    ):
        raise ValueError("candidate has no verified local-only registry binding")
    return binding


def run_local_selected_task(
    *,
    user_prompt: str,
    task_kind: str,
    owner_id: str,
    arbiter: InProcessResourceArbiter | None = None,
    candidates: tuple[LocalCandidate, ...] | None = None,
    registry: ProviderRegistry | None = None,
    decision_model: DecisionModel | None = None,
    context_tokens: int = 0,
    capability: str = "",
    adapters: dict[str, AIProviderAdapter] | None = None,
    installed_sizes: Mapping[str, int] | None = None,
) -> LocalRouteOutcome:
    """Retry a different candidate only after an execution/load failure.

    An invalid model result is refused, never silently replaced. Every retry
    observes and ranks afresh, then reserves against that exact generation.
    """
    registry = registry or load_default_provider_registry()
    arbiter = arbiter or get_resource_arbiter()
    if installed_sizes is None:
        sizes = get_local_ai_runtime_status().get("installed_model_sizes")
        installed_sizes = sizes if isinstance(sizes, dict) else {}
    if context_tokens < 0:
        raise ValueError("context_tokens must be nonnegative")
    remaining = []
    for candidate in candidates if candidates is not None else local_candidates(registry):
        binding = _safe_binding(candidate, registry)
        if binding.context_window_tokens is None:
            continue
        if context_tokens + binding.max_output_tokens > binding.context_window_tokens:
            continue
        model_config = registry.models[(binding.provider_id, binding.model_id)]
        if capability and (
            binding.route_class != f"local:{capability}"
            or f"local:{capability}" not in model_config.route_classes
        ):
            continue
        remaining.append(candidate)
    model = decision_model or RuleDecisionModel()
    attempts: list[LocalRouteAttempt] = []
    last_outcome: AiTaskOutcome | None = None
    if not remaining:
        now = datetime.now(UTC)
        request = DecisionRequest(
            decision_id=str(uuid4()),
            decision_type="local_model.select",
            output_specs=(DecisionOutputSpec(name="fit", kind="score"),),
            resource_snapshot=arbiter.snapshot(),
            requested_at=now,
            deadline_at=now + timedelta(seconds=30),
        )
        decision = DecisionResult(
            decision_id=request.decision_id,
            outcome="abstained",
            model_ref="jarvis.deterministic-admission.v1",
            reason_code="no_candidate_fits",
            decided_at=now,
        )
        validate_decision_result(request, decision)
        return LocalRouteOutcome("abstained", None, (LocalRouteAttempt(decision, None, None, "abstained"),))
    while remaining:
        snapshot = arbiter.snapshot()
        remaining = [with_cold_load_footprint(candidate, installed_sizes, snapshot) for candidate in remaining]
        now = datetime.now(UTC)
        decision_id = str(uuid4())
        request = DecisionRequest(
            decision_id=decision_id,
            decision_type="local_model.select",
            candidate_set=tuple(candidate.candidate_id for candidate in remaining),
            output_specs=(DecisionOutputSpec(name="fit", kind="score"),),
            constraints={"context_tokens": context_tokens, "capability": capability, "task_text": user_prompt[:256]},
            resource_snapshot=snapshot,
            requested_at=now,
            deadline_at=now + timedelta(seconds=30),
        )
        decision = model.decide(request, tuple(remaining))
        validate_decision_result(request, decision)
        if decision.outcome == "abstained":
            attempts.append(LocalRouteAttempt(decision, None, None, "abstained"))
            return LocalRouteOutcome("abstained", last_outcome, tuple(attempts))
        candidate = next(candidate for candidate in remaining if candidate.candidate_id == decision.selected_candidate)
        binding = _safe_binding(candidate, registry)
        reservation = ResourceReservationRequest(
            request_id=str(uuid4()),
            owner_kind="inference_envelope",
            owner_id=owner_id,
            correlation_id=decision_id,
            resources=ResourceAmounts(
                model_name=candidate.model_name,
                ram_bytes=candidate.ram_bytes,
                vram_bytes=candidate.vram_bytes,
                gpu_index=candidate.gpu_index,
            ),
            snapshot_generation=snapshot.generation,
            requested_at=now,
            deadline_at=now + timedelta(seconds=30),
            max_hold_seconds=300,
        )
        try:
            lease = arbiter.reserve(reservation)
        except (ResourceLeaseError, ResourceCapacityError):
            attempts.append(LocalRouteAttempt(decision, None, None, "reservation_refused"))
            return LocalRouteOutcome("reservation_refused", last_outcome, tuple(attempts))
        outcome: AiTaskOutcome | None = None
        started = perf_counter()
        try:
            outcome = run_ai_task(
                user_prompt=user_prompt,
                task_kind=task_kind,
                route_class=binding.route_class,
                bindings={binding.route_class: binding},
                adapters=adapters,
            )
            last_outcome = outcome
        finally:
            elapsed_ms = max(1, round((perf_counter() - started) * 1000))
            with _LATENCY_LOCK:
                _OBSERVED_LATENCY_MS[candidate.model_name] = elapsed_ms
            arbiter.end(
                lease.lease_id,
                expected_version=lease.version,
                reason="completed" if outcome and outcome.status == "success" else "failed",
            )
        attempts.append(LocalRouteAttempt(decision, lease.lease_id, outcome.ledger_id, outcome.status))
        if outcome.status != "provider_error":
            return LocalRouteOutcome(outcome.status, outcome, tuple(attempts))
        remaining = [item for item in remaining if item.model_name != candidate.model_name]
    return LocalRouteOutcome(last_outcome.status if last_outcome else "abstained", last_outcome, tuple(attempts))
