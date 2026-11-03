from __future__ import annotations

import pytest

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderError,
    AIProviderErrorCode,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
)
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_external import (
    external_not_started_evidence,
    external_reconciled_evidence,
)


def _binding() -> ProviderBinding:
    return ProviderBinding(
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        requires_network=True,
        max_output_tokens=64,
        execution_class="external_provider",
        context_window_tokens=8192,
    )


def _pretransport_response() -> AIResponse:
    request = AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    return AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id=request.request_id,
        usage=AIUsage(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            input_tokens=0,
            output_tokens=0,
        ),
        external_dispatch_state=AIExternalDispatchState.not_started,
        error=AIProviderError(
            code=AIProviderErrorCode.provider_auth_missing,
            message="safe",
            retryable=False,
        ),
    )


def test_adapter_invoked_pretransport_stop_remains_not_started() -> None:
    response = _pretransport_response()
    evidence = external_reconciled_evidence(
        binding=_binding(),
        pricing_version="pricing-v1",
        dispatch_state=AIExternalDispatchState.not_started,
        reconciliation_status="not_sent",
        reconciled_cost_usd="0",
        response=response,
        outcome_reason="provider auth missing",
        requested_output_ceiling=64,
        effective_output_ceiling=64,
        fallback_index=0,
    )

    assert evidence.adapter_invoked is True
    assert evidence.external_dispatch_state == "not_started"
    assert evidence.normalized_usage_source == "none"
    assert evidence.accounting_basis == "external_not_sent"
    assert evidence.normalized_finish_reason == "error"


def test_noninvoked_stop_rejects_response_payload() -> None:
    with pytest.raises(ValueError, match="non-invoked"):
        external_not_started_evidence(
            binding=_binding(),
            pricing_version="pricing-v1",
            outcome_reason="blocked",
            requested_output_ceiling=64,
            effective_output_ceiling=64,
            fallback_index=0,
            adapter_invoked=False,
            response=_pretransport_response(),
        )
