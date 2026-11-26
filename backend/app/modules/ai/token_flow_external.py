from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.modules.ai.contracts import AIExternalDispatchState, AIResponse
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_flow_runtime import (
    ACCOUNTING_VERSION,
    CAPABILITY_VERSION,
    normalize_finish_reason,
    normalize_outcome_reason,
)

_STANDARD_INPUT_STATUSES = frozenset(
    {
        "conservative_cost_binding_mismatch",
        "conservative_pricing_drift",
    }
)
_ESTIMATED_USAGE_STATUSES = frozenset(
    {
        "conservative_missing_usage",
        "conservative_unverified_usage",
        "conservative_usage_binding_mismatch",
    }
)
_RECONCILIATION_STATUSES = frozenset(
    {
        "actual",
        "not_sent",
        *_STANDARD_INPUT_STATUSES,
        *_ESTIMATED_USAGE_STATUSES,
    }
)


def external_not_started_evidence(
    *,
    binding: ProviderBinding,
    pricing_version: str,
    outcome_reason: str,
    requested_output_ceiling: int | None,
    effective_output_ceiling: int | None,
    fallback_index: int,
    adapter_invoked: bool = False,
    response: AIResponse | None = None,
) -> AttemptEvidence:
    _require_external_binding(binding)
    if not isinstance(adapter_invoked, bool):
        raise ValueError("adapter_invoked must be boolean")
    if not adapter_invoked and response is not None:
        raise ValueError("non-invoked external stop cannot carry a response")
    finish_reason = (
        normalize_finish_reason(
            response.finish_reason if response is not None else None,
            failed=response is None or response.error is not None,
        )
        if adapter_invoked
        else None
    )
    return AttemptEvidence(
        execution_class="external_provider",
        adapter_invoked=adapter_invoked,
        external_dispatch_state="not_started",
        normalized_usage_source="none",
        accounting_basis="external_not_sent",
        accounted_provider_spend_usd_decimal="0",
        outcome_reason=normalize_outcome_reason(outcome_reason),
        accounting_version=ACCOUNTING_VERSION,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        selected_route_class=binding.route_class,
        fallback_index=fallback_index,
        requested_output_ceiling=requested_output_ceiling,
        effective_output_ceiling=effective_output_ceiling,
        normalized_finish_reason=finish_reason,
        capability_version=CAPABILITY_VERSION,
        pricing_version=_required_version(pricing_version),
    )


def external_reconciled_evidence(
    *,
    binding: ProviderBinding,
    pricing_version: str,
    dispatch_state: AIExternalDispatchState,
    reconciliation_status: str,
    reconciled_cost_usd: float | str | Decimal,
    response: AIResponse | None,
    outcome_reason: str,
    requested_output_ceiling: int | None,
    effective_output_ceiling: int | None,
    fallback_index: int,
) -> AttemptEvidence:
    _require_external_binding(binding)
    if dispatch_state is AIExternalDispatchState.not_started:
        if reconciliation_status != "not_sent":
            raise ValueError("not_started dispatch requires not_sent reconciliation")
        return external_not_started_evidence(
            binding=binding,
            pricing_version=pricing_version,
            outcome_reason=outcome_reason,
            requested_output_ceiling=requested_output_ceiling,
            effective_output_ceiling=effective_output_ceiling,
            fallback_index=fallback_index,
            adapter_invoked=True,
            response=response,
        )
    if reconciliation_status not in _RECONCILIATION_STATUSES - {"not_sent"}:
        raise ValueError("unsupported external reconciliation status")

    spend = _canonical_spend(reconciled_cost_usd)
    if dispatch_state is AIExternalDispatchState.unknown:
        usage_source = "estimated"
        accounting_basis = "conservative_estimated_usage"
    elif dispatch_state is AIExternalDispatchState.started:
        if reconciliation_status == "actual":
            usage_source = "actual"
            accounting_basis = "provider_exact"
        elif reconciliation_status in _STANDARD_INPUT_STATUSES:
            usage_source = "actual"
            accounting_basis = "conservative_standard_input"
        else:
            usage_source = "estimated"
            accounting_basis = "conservative_estimated_usage"
    else:  # pragma: no cover - enum exhaustiveness
        raise ValueError("unsupported external dispatch state")

    if accounting_basis != "provider_exact" and Decimal(spend) <= 0:
        raise ValueError("conservative external evidence requires positive spend")
    finish_reason = normalize_finish_reason(
        response.finish_reason if response is not None else None,
        failed=response is None or response.error is not None,
    )
    return AttemptEvidence(
        execution_class="external_provider",
        adapter_invoked=True,
        external_dispatch_state=dispatch_state.value,
        normalized_usage_source=usage_source,
        accounting_basis=accounting_basis,
        accounted_provider_spend_usd_decimal=spend,
        outcome_reason=normalize_outcome_reason(outcome_reason),
        accounting_version=ACCOUNTING_VERSION,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        selected_route_class=binding.route_class,
        fallback_index=fallback_index,
        requested_output_ceiling=requested_output_ceiling,
        effective_output_ceiling=effective_output_ceiling,
        normalized_finish_reason=finish_reason,
        capability_version=CAPABILITY_VERSION,
        pricing_version=_required_version(pricing_version),
    )


def _require_external_binding(binding: ProviderBinding) -> None:
    if not binding.requires_network or binding.execution_class != "external_provider":
        raise ValueError("external evidence requires an explicit external-provider binding")


def _required_version(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("pricing_version must be non-empty text")
    return value.strip()


def _canonical_spend(value: float | str | Decimal) -> str:
    if isinstance(value, bool):
        raise ValueError("reconciled spend must be a finite non-negative decimal")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("reconciled spend must be a finite non-negative decimal") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("reconciled spend must be a finite non-negative decimal")
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
