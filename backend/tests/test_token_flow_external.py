from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderError,
    AIProviderErrorCode,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_external import (
    external_not_started_evidence,
    external_reconciled_evidence,
)


def _binding(*, requires_network: bool = True, execution_class: str = "external_provider") -> ProviderBinding:
    return ProviderBinding(
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        requires_network=requires_network,
        max_output_tokens=128,
        execution_class=execution_class,
        context_window_tokens=8192,
    )


def _response(*, failed: bool = False) -> AIResponse:
    request = AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    return AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id=request.request_id,
        text=None if failed else "done",
        finish_reason=None if failed else "stop",
        usage=AIUsage(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            input_tokens=3,
            output_tokens=2,
            usage_source=AIUsageSource.actual,
            provider_cost_estimate=0.01,
            currency="USD",
        ),
        external_dispatch_state=(
            AIExternalDispatchState.unknown if failed else AIExternalDispatchState.started
        ),
        error=(
            AIProviderError(
                code=AIProviderErrorCode.provider_timeout,
                message="safe",
                retryable=True,
            )
            if failed
            else None
        ),
    )


def test_not_started_external_evidence_is_non_economic_and_uninvoked() -> None:
    evidence = external_not_started_evidence(
        binding=_binding(),
        pricing_version="pricing-v1",
        outcome_reason="provider gate blocked",
        requested_output_ceiling=128,
        effective_output_ceiling=64,
        fallback_index=1,
    )

    assert evidence.execution_class == "external_provider"
    assert evidence.adapter_invoked is False
    assert evidence.external_dispatch_state == "not_started"
    assert evidence.normalized_usage_source == "none"
    assert evidence.accounting_basis == "external_not_sent"
    assert evidence.accounted_provider_spend_usd_decimal == "0"
    assert evidence.pricing_version == "pricing-v1"
    assert evidence.normalized_finish_reason is None


@pytest.mark.parametrize(
    ("status", "expected_usage", "expected_basis"),
    [
        ("actual", "actual", "provider_exact"),
        ("conservative_pricing_drift", "actual", "conservative_standard_input"),
        ("conservative_cost_binding_mismatch", "actual", "conservative_standard_input"),
        ("conservative_missing_usage", "estimated", "conservative_estimated_usage"),
        ("conservative_unverified_usage", "estimated", "conservative_estimated_usage"),
        ("conservative_usage_binding_mismatch", "estimated", "conservative_estimated_usage"),
    ],
)
def test_started_reconciliation_maps_to_canonical_accounting(
    status: str,
    expected_usage: str,
    expected_basis: str,
) -> None:
    evidence = external_reconciled_evidence(
        binding=_binding(),
        pricing_version="pricing-v1",
        dispatch_state=AIExternalDispatchState.started,
        reconciliation_status=status,
        reconciled_cost_usd="0.0100",
        response=_response(),
        outcome_reason="success",
        requested_output_ceiling=128,
        effective_output_ceiling=128,
        fallback_index=0,
    )

    assert evidence.normalized_usage_source == expected_usage
    assert evidence.accounting_basis == expected_basis
    assert evidence.accounted_provider_spend_usd_decimal == "0.01"
    assert evidence.normalized_finish_reason == "stop"


def test_unknown_dispatch_forces_estimated_conservative_evidence() -> None:
    evidence = external_reconciled_evidence(
        binding=_binding(),
        pricing_version="pricing-v1",
        dispatch_state=AIExternalDispatchState.unknown,
        reconciliation_status="actual",
        reconciled_cost_usd=Decimal("0.0200"),
        response=_response(failed=True),
        outcome_reason="adapter exception",
        requested_output_ceiling=128,
        effective_output_ceiling=128,
        fallback_index=0,
    )

    assert evidence.adapter_invoked is True
    assert evidence.normalized_usage_source == "estimated"
    assert evidence.accounting_basis == "conservative_estimated_usage"
    assert evidence.accounted_provider_spend_usd_decimal == "0.02"
    assert evidence.normalized_finish_reason == "error"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", "0"), ("10", "10"), ("10.000", "10"), ("0.0000010", "0.000001")],
)
def test_provider_exact_spend_is_canonical_fixed_point(value: str, expected: str) -> None:
    evidence = external_reconciled_evidence(
        binding=_binding(),
        pricing_version="pricing-v1",
        dispatch_state=AIExternalDispatchState.started,
        reconciliation_status="actual",
        reconciled_cost_usd=value,
        response=_response(),
        outcome_reason="success",
        requested_output_ceiling=128,
        effective_output_ceiling=128,
        fallback_index=0,
    )
    assert evidence.accounted_provider_spend_usd_decimal == expected


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", True])
def test_conservative_external_spend_fails_closed(value: object) -> None:
    with pytest.raises(ValueError):
        external_reconciled_evidence(
            binding=_binding(),
            pricing_version="pricing-v1",
            dispatch_state=AIExternalDispatchState.unknown,
            reconciliation_status="conservative_missing_usage",
            reconciled_cost_usd=value,
            response=None,
            outcome_reason="provider exception",
            requested_output_ceiling=128,
            effective_output_ceiling=128,
            fallback_index=0,
        )


def test_mapper_rejects_invalid_binding_status_and_pricing() -> None:
    with pytest.raises(ValueError, match="external-provider"):
        external_not_started_evidence(
            binding=_binding(requires_network=False, execution_class="synthetic"),
            pricing_version="pricing-v1",
            outcome_reason="blocked",
            requested_output_ceiling=128,
            effective_output_ceiling=128,
            fallback_index=0,
        )
    with pytest.raises(ValueError, match="not_sent"):
        external_reconciled_evidence(
            binding=_binding(),
            pricing_version="pricing-v1",
            dispatch_state=AIExternalDispatchState.not_started,
            reconciliation_status="actual",
            reconciled_cost_usd="0",
            response=None,
            outcome_reason="blocked",
            requested_output_ceiling=128,
            effective_output_ceiling=128,
            fallback_index=0,
        )
    with pytest.raises(ValueError, match="pricing_version"):
        external_not_started_evidence(
            binding=_binding(),
            pricing_version=" ",
            outcome_reason="blocked",
            requested_output_ceiling=128,
            effective_output_ceiling=128,
            fallback_index=0,
        )
