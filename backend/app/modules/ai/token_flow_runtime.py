from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.ai.contracts import AIResponse, AIUsageSource
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_guard import estimate_tokens

CAPABILITY_VERSION = "provider-registry-v1"
ACCOUNTING_VERSION = "token-flow-v0"
_FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_call", "error", "unknown"})
_REASON_PART = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class AttemptUsageOverride:
    input_tokens: int | None = None
    output_tokens: int | None = None


def no_execution_evidence(
    *,
    selected_route_class: str | None,
    binding: ProviderBinding | None,
    outcome_reason: str,
    requested_output_ceiling: int | None,
    effective_output_ceiling: int | None,
    fallback_index: int | None = None,
) -> AttemptEvidence:
    return AttemptEvidence(
        execution_class="none",
        adapter_invoked=False,
        external_dispatch_state="not_applicable",
        normalized_usage_source="none",
        accounting_basis="no_execution",
        accounted_provider_spend_usd_decimal="0",
        outcome_reason=normalize_outcome_reason(outcome_reason),
        accounting_version=ACCOUNTING_VERSION,
        provider_id=binding.provider_id if binding is not None else None,
        model_id=binding.model_id if binding is not None else None,
        selected_route_class=selected_route_class,
        fallback_index=fallback_index,
        requested_output_ceiling=requested_output_ceiling,
        effective_output_ceiling=effective_output_ceiling,
    )


def local_response_evidence(
    *,
    binding: ProviderBinding,
    response: AIResponse,
    selected_route_class: str,
    outcome_reason: str,
    requested_output_ceiling: int | None,
    effective_output_ceiling: int | None,
    fallback_index: int,
) -> AttemptEvidence:
    execution_class = _local_execution_class(binding)
    usage_source = response.usage.usage_source.value
    if execution_class == "synthetic" and usage_source != AIUsageSource.estimated.value:
        raise ValueError("synthetic response usage must be estimated")
    if execution_class == "local_compute" and usage_source not in {
        AIUsageSource.actual.value,
        AIUsageSource.estimated.value,
    }:
        raise ValueError("local compute usage must be actual or estimated")
    return AttemptEvidence(
        execution_class=execution_class,
        adapter_invoked=True,
        external_dispatch_state="not_applicable",
        normalized_usage_source=usage_source,
        accounting_basis=(
            "synthetic_not_economic"
            if execution_class == "synthetic"
            else "local_compute_unpriced"
        ),
        accounted_provider_spend_usd_decimal="0",
        outcome_reason=normalize_outcome_reason(outcome_reason),
        accounting_version=ACCOUNTING_VERSION,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        selected_route_class=selected_route_class,
        fallback_index=fallback_index,
        requested_output_ceiling=requested_output_ceiling,
        effective_output_ceiling=effective_output_ceiling,
        normalized_finish_reason=normalize_finish_reason(
            response.finish_reason, failed=response.error is not None
        ),
        capability_version=CAPABILITY_VERSION,
    )


def local_exception_evidence(
    *,
    binding: ProviderBinding,
    prompt: str,
    selected_route_class: str,
    requested_output_ceiling: int | None,
    effective_output_ceiling: int | None,
    fallback_index: int,
) -> tuple[AttemptEvidence, AttemptUsageOverride]:
    execution_class = _local_execution_class(binding)
    evidence = AttemptEvidence(
        execution_class=execution_class,
        adapter_invoked=True,
        external_dispatch_state="not_applicable",
        normalized_usage_source="estimated",
        accounting_basis=(
            "synthetic_not_economic"
            if execution_class == "synthetic"
            else "local_compute_unpriced"
        ),
        accounted_provider_spend_usd_decimal="0",
        outcome_reason="adapter_exception",
        accounting_version=ACCOUNTING_VERSION,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        selected_route_class=selected_route_class,
        fallback_index=fallback_index,
        requested_output_ceiling=requested_output_ceiling,
        effective_output_ceiling=effective_output_ceiling,
        normalized_finish_reason="error",
        capability_version=CAPABILITY_VERSION,
    )
    return evidence, AttemptUsageOverride(input_tokens=estimate_tokens(prompt), output_tokens=0)


def normalize_finish_reason(value: str | None, *, failed: bool) -> str:
    if failed:
        return "error"
    if value is None:
        return "unknown"
    normalized = value.strip().lower().replace("-", "_")
    return normalized if normalized in _FINISH_REASONS else "unknown"


def normalize_outcome_reason(value: str) -> str:
    normalized = _REASON_PART.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        return "unknown_outcome"
    return normalized[:128]


def _local_execution_class(binding: ProviderBinding) -> str:
    if binding.requires_network:
        raise ValueError("network binding cannot use local evidence mapping")
    if binding.execution_class not in {"synthetic", "local_compute"}:
        raise ValueError("local binding requires explicit synthetic or local_compute class")
    return binding.execution_class
