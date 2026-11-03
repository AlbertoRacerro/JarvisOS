from __future__ import annotations

import pytest

from app.modules.ai.contracts import (
    AIProviderError,
    AIProviderErrorCode,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_runtime import (
    local_exception_evidence,
    local_response_evidence,
    no_execution_evidence,
    normalize_finish_reason,
    normalize_outcome_reason,
)


def _binding(execution_class: str = "synthetic") -> ProviderBinding:
    return ProviderBinding(
        route_class="local:fake",
        provider_id="fake",
        model_id="fake-modeling-draft-v1",
        requires_network=False,
        max_output_tokens=128,
        execution_class=execution_class,
        context_window_tokens=4096,
    )


def _response(*, source: AIUsageSource = AIUsageSource.estimated, error=False) -> AIResponse:
    request = AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    return AIResponse(
        provider_id="fake",
        model_id="fake-modeling-draft-v1",
        request_id=request.request_id,
        text=None if error else "done",
        finish_reason=None if error else "stop",
        usage=AIUsage(
            provider_id="fake",
            model_id="fake-modeling-draft-v1",
            input_tokens=3,
            output_tokens=2,
            usage_source=source,
        ),
        error=(
            AIProviderError(
                code=AIProviderErrorCode.provider_unknown_error,
                message="safe",
            )
            if error
            else None
        ),
    )


def test_no_execution_preserves_requested_route_and_optional_binding() -> None:
    without_binding = no_execution_evidence(
        selected_route_class="local:missing",
        binding=None,
        outcome_reason="Route unavailable",
        requested_output_ceiling=None,
        effective_output_ceiling=None,
    )
    with_binding = no_execution_evidence(
        selected_route_class="local:fake",
        binding=_binding(),
        outcome_reason="adapter unavailable",
        requested_output_ceiling=100,
        effective_output_ceiling=100,
        fallback_index=0,
    )

    assert without_binding.execution_class == "none"
    assert without_binding.selected_route_class == "local:missing"
    assert without_binding.provider_id is None
    assert without_binding.outcome_reason == "route_unavailable"
    assert with_binding.provider_id == "fake"
    assert with_binding.fallback_index == 0


def test_synthetic_and_local_compute_use_distinct_accounting() -> None:
    synthetic = local_response_evidence(
        binding=_binding("synthetic"),
        response=_response(),
        selected_route_class="local:fake",
        outcome_reason="success",
        requested_output_ceiling=128,
        effective_output_ceiling=128,
        fallback_index=0,
    )
    local = local_response_evidence(
        binding=_binding("local_compute"),
        response=_response(),
        selected_route_class="local:fake",
        outcome_reason="success",
        requested_output_ceiling=128,
        effective_output_ceiling=128,
        fallback_index=0,
    )

    assert synthetic.accounting_basis == "synthetic_not_economic"
    assert local.accounting_basis == "local_compute_unpriced"
    assert synthetic.accounted_provider_spend_usd_decimal == "0"
    assert local.accounted_provider_spend_usd_decimal == "0"


def test_local_exception_uses_estimated_input_and_zero_output() -> None:
    evidence, usage = local_exception_evidence(
        binding=_binding("local_compute"),
        prompt="one two three four",
        selected_route_class="local:general",
        requested_output_ceiling=64,
        effective_output_ceiling=32,
        fallback_index=1,
    )

    assert evidence.adapter_invoked is True
    assert evidence.normalized_finish_reason == "error"
    assert evidence.accounting_basis == "local_compute_unpriced"
    assert usage.input_tokens is not None and usage.input_tokens > 0
    assert usage.output_tokens == 0


def test_mapper_rejects_missing_or_network_execution_class() -> None:
    missing = _binding()
    object.__setattr__(missing, "execution_class", None)
    network = ProviderBinding(
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        requires_network=True,
        max_output_tokens=64,
        execution_class="external_provider",
        context_window_tokens=4096,
    )

    with pytest.raises(ValueError, match="explicit"):
        local_response_evidence(
            binding=missing,
            response=_response(),
            selected_route_class="local:fake",
            outcome_reason="success",
            requested_output_ceiling=64,
            effective_output_ceiling=64,
            fallback_index=0,
        )
    with pytest.raises(ValueError, match="network"):
        local_exception_evidence(
            binding=network,
            prompt="hello",
            selected_route_class="external:cheap",
            requested_output_ceiling=64,
            effective_output_ceiling=64,
            fallback_index=0,
        )


def test_usage_and_reason_normalization_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="synthetic"):
        local_response_evidence(
            binding=_binding("synthetic"),
            response=_response(source=AIUsageSource.actual),
            selected_route_class="local:fake",
            outcome_reason="success",
            requested_output_ceiling=64,
            effective_output_ceiling=64,
            fallback_index=0,
        )

    failed = local_response_evidence(
        binding=_binding("synthetic"),
        response=_response(error=True),
        selected_route_class="local:fake",
        outcome_reason="provider error",
        requested_output_ceiling=64,
        effective_output_ceiling=64,
        fallback_index=0,
    )
    assert failed.normalized_finish_reason == "error"
    assert normalize_finish_reason("MAX-TOKENS", failed=False) == "unknown"
    assert normalize_outcome_reason("RuntimeError: bad") == "runtimeerror_bad"
