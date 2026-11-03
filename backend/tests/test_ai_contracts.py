import json
from typing import Any

import pytest

from app.modules.ai.contracts import (
    AIMessage,
    AIModelCapability,
    AIPolicyMode,
    AIPrivacyClass,
    AIProviderAdapter,
    AIProviderHealth,
    AIProviderId,
    AIProviderKind,
    AIProviderStatus,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
    ModelRegistry,
    ModelRegistryEntry,
    ProviderRegistry,
    ProviderRegistryEntry,
)


class StubProviderAdapter(AIProviderAdapter):
    provider_id: AIProviderId = "stub"

    def health(self) -> AIProviderHealth:
        return AIProviderHealth.healthy

    def list_models(self) -> list[ModelRegistryEntry]:
        return [
            ModelRegistryEntry(
                model_id="stub-fast",
                provider_id=self.provider_id,
                provider_model_name="stub-fast",
                display_name="Stub Fast",
                enabled=True,
                capabilities={AIModelCapability.chat_text, AIModelCapability.structured_json},
                default_task_types={AITaskType.smoke_console_test},
            )
        ]

    def complete(self, request: AIRequest) -> AIResponse:
        return AIResponse(
            provider_id=self.provider_id,
            model_id=request.model_preference or "stub-fast",
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text="stub response",
            usage=AIUsage(
                provider_id=self.provider_id,
                model_id=request.model_preference or "stub-fast",
                input_tokens=2,
                output_tokens=3,
                usage_source=AIUsageSource.estimated,
            ),
            finish_reason="stop",
            safety_status="allowed",
            raw_provider_metadata={"mock": True},
        )

    def stream(self, request: AIRequest) -> Any:
        raise NotImplementedError


def test_provider_registry_register_list_and_get_provider_adapter() -> None:
    registry = ProviderRegistry()
    adapter = StubProviderAdapter()
    entry = ProviderRegistryEntry(
        provider_id="stub",
        kind=AIProviderKind.fake,
        display_name="Stub Provider",
        status=AIProviderStatus.enabled,
        health=AIProviderHealth.healthy,
        enabled=True,
    )

    registry.register_provider(adapter, entry)

    assert registry.get_provider("stub") is adapter
    assert registry.get_entry("stub") == entry
    assert registry.list_providers() == [entry]
    assert registry.list_providers(enabled_only=True) == [entry]
    assert registry.get_provider("missing") is None


def test_provider_registry_rejects_adapter_entry_id_mismatch() -> None:
    registry = ProviderRegistry()

    with pytest.raises(ValueError, match="Provider adapter id"):
        registry.register_provider(
            StubProviderAdapter(),
            ProviderRegistryEntry(
                provider_id="other",
                kind=AIProviderKind.fake,
                display_name="Other",
            ),
        )


def test_model_registry_register_list_and_filter_models() -> None:
    registry = ModelRegistry()
    fast = ModelRegistryEntry(
        model_id="stub-fast",
        provider_id="stub",
        provider_model_name="stub-fast",
        display_name="Stub Fast",
        enabled=True,
        capabilities={AIModelCapability.chat_text, AIModelCapability.low_latency},
        default_task_types={AITaskType.smoke_console_test, AITaskType.model_spec_draft},
        allowed_privacy_classes={AIPrivacyClass.public, AIPrivacyClass.internal},
    )
    critic = ModelRegistryEntry(
        model_id="stub-critic",
        provider_id="stub",
        provider_model_name="stub-critic",
        display_name="Stub Critic",
        enabled=False,
        capabilities={AIModelCapability.high_reasoning},
        default_task_types={AITaskType.critic_review},
        allowed_privacy_classes={AIPrivacyClass.public},
    )

    registry.register_model(fast)
    registry.register_model(critic)

    assert registry.get_model("stub-fast") == fast
    assert registry.list_enabled_models() == [fast]
    assert registry.find_models(capability=AIModelCapability.low_latency) == [fast]
    assert registry.find_models(task_type=AITaskType.model_spec_draft) == [fast]
    assert registry.find_models(privacy_class=AIPrivacyClass.internal) == [fast]
    assert registry.find_models(provider_id="stub") == [fast]
    assert registry.find_models(capability=AIModelCapability.high_reasoning) == []
    assert registry.find_models(capability=AIModelCapability.high_reasoning, enabled_only=False) == [critic]


def test_ai_usage_computes_and_validates_total_tokens() -> None:
    usage = AIUsage(provider_id="stub", model_id="stub-fast", input_tokens=7, output_tokens=5)

    assert usage.total_tokens == 12

    with pytest.raises(ValueError, match="total_tokens"):
        AIUsage(provider_id="stub", model_id="stub-fast", input_tokens=7, output_tokens=5, total_tokens=99)


def test_ai_request_and_response_round_trip_through_pydantic_serialization() -> None:
    request = AIRequest(
        task_type=AITaskType.model_spec_draft,
        privacy_class=AIPrivacyClass.internal,
        prompt="Draft a harmless model spec.",
        messages=[AIMessage(role="user", content="Draft a harmless model spec.")],
        workspace_id="main",
        model_preference="stub-fast",
        max_output_tokens=80,
        structured_output_schema={"type": "object"},
        metadata={"source": "test"},
        correlation_id="corr-1",
    )
    request_payload = request.model_dump(mode="json")
    restored_request = AIRequest.model_validate(request_payload)

    assert restored_request == request

    response = StubProviderAdapter().complete(restored_request)
    response_payload = response.model_dump(mode="json")
    restored_response = AIResponse.model_validate(response_payload)

    assert restored_response == response
    assert "stub response" in json.dumps(response_payload)


def test_privacy_class_values_match_existing_privacy_policy_classes() -> None:
    from app.modules.ai.privacy import PrivacyPolicyEngine

    policy = PrivacyPolicyEngine()
    samples = {
        "public": "Public research request: summarize published literature.",
        "internal": "Internal note for rough sizing.",
        "confidential": "Zephyr concept sketch.",
        "sensitive_ip": "Quick Coupler proprietary geometry details.",
        "secret": "Example .env content: API key and password.",
        "unknown": "Unlabeled fragment.",
    }

    for expected, text in samples.items():
        assert policy.classify(text) == expected
        assert AIPrivacyClass(expected).value == expected


def test_task_type_enum_contains_required_values() -> None:
    required = {
        "smoke_console_test",
        "smoke_test",
        "assumption_review",
        "equation_review",
        "literature_query_planning",
        "source_extraction",
        "model_spec_draft",
        "simulation_result_interpretation",
        "code_review",
        "runner_error_explanation",
        "artifact_summary",
        "decision_support",
        "critic_review",
        "synthesis",
    }

    assert required.issubset({task.value for task in AITaskType})


def test_policy_mode_enum_contains_fast_dev_strict_ip_and_disabled() -> None:
    assert {mode.value for mode in AIPolicyMode} == {"FAST_DEV", "STRICT_IP", "DISABLED"}


def test_adapter_stub_returns_provider_neutral_response_without_network() -> None:
    adapter = StubProviderAdapter()
    request = AIRequest(
        task_type=AITaskType.smoke_console_test,
        privacy_class=AIPrivacyClass.public,
        prompt="ciao",
        model_preference="stub-fast",
    )

    response = adapter.complete(request)

    assert response.provider_id == "stub"
    assert response.model_id == "stub-fast"
    assert response.text == "stub response"
    assert response.usage.total_tokens == 5
    assert response.safety_status == "allowed"
