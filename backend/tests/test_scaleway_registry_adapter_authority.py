import pytest

from app.modules.ai.contracts import AIRequest, AITaskType
from app.modules.ai.execution import _default_adapters
from app.modules.ai.provider_registry import (
    load_default_provider_registry,
    registry_bindings,
)
from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter
from app.modules.ai.providers.scaleway import ScalewayChatResult, ScalewayProvider
from app.modules.ai.providers.scaleway_adapter import ScalewayProviderAdapter

MODEL_ID = "deepseek-v4-flash-0731"
LEGACY_SMOKE_MODEL = "llama-3.1-8b-instruct"


def test_scaleway_registry_kind_preserves_provider_specific_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://scaleway.example.test/v1")
    monkeypatch.setenv("SCALEWAY_MODEL", "legacy-smoke-only")

    registry = load_default_provider_registry()
    adapters = _default_adapters()

    assert registry.providers["scaleway"].kind == "scaleway"
    assert isinstance(adapters["scaleway"], ScalewayProviderAdapter)
    assert not isinstance(adapters["scaleway"], OpenAICompatAdapter)
    assert (
        adapters["scaleway"].provider.base_url()
        == "https://scaleway.example.test/v1"
    )
    assert adapters["scaleway"].provider.model() == "legacy-smoke-only"
    assert isinstance(adapters["deepseek"], OpenAICompatAdapter)


def test_route_has_no_fallback_and_legacy_smoke_model_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALEWAY_MODEL", "unregistered-legacy-smoke-model")

    registry = load_default_provider_registry()
    binding = registry_bindings()["external:scaleway"]

    assert binding.provider_id == "scaleway"
    assert binding.model_id == MODEL_ID
    assert binding.execution_class == "external_provider"
    assert binding.requires_network is True
    assert "external:scaleway" not in registry.fallback_chains


def test_routed_work_passes_exact_binding_model_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCALEWAY_MODEL", raising=False)
    binding = registry_bindings()["external:scaleway"]
    captured: dict[str, object] = {}

    class CapturingProvider(ScalewayProvider):
        def create_work_completion(
            self,
            *,
            prompt: str,
            estimated_output_tokens: int,
            model: str,
        ) -> ScalewayChatResult:
            captured.update(
                prompt=prompt,
                estimated_output_tokens=estimated_output_tokens,
                model=model,
            )
            return ScalewayChatResult(
                provider_name="scaleway",
                model=model,
                mode="work",
                external_call_attempted=True,
                external_call_succeeded=True,
                response_text="OK",
                reported_input_tokens=2,
                reported_output_tokens=1,
                reported_total_tokens=3,
                sanitized_metadata={"usage_returned": True},
            )

    provider = CapturingProvider()
    response = ScalewayProviderAdapter(provider).complete(
        AIRequest(
            task_type=AITaskType.decision_support,
            prompt="Reply OK.",
            model_preference=binding.model_id,
            max_output_tokens=8,
        )
    )

    assert provider.model() == LEGACY_SMOKE_MODEL
    assert captured["model"] == MODEL_ID
    assert response.model_id == MODEL_ID
    assert response.text == "OK"


def test_routed_work_rejects_missing_model_without_provider_call() -> None:
    class NoCallProvider(ScalewayProvider):
        def create_work_completion(
            self,
            *,
            prompt: str,
            estimated_output_tokens: int,
            model: str,
        ) -> ScalewayChatResult:
            raise AssertionError("provider must not be called")

    response = ScalewayProviderAdapter(NoCallProvider()).complete(
        AIRequest(
            task_type=AITaskType.decision_support,
            prompt="Reply OK.",
            max_output_tokens=8,
        )
    )

    assert response.error is not None
    assert response.blocked_reason == "scaleway_routed_model_missing"
    assert response.raw_provider_metadata["external_call_attempted"] is False


def test_empty_routed_completion_is_not_success() -> None:
    class EmptyProvider(ScalewayProvider):
        def create_work_completion(
            self,
            *,
            prompt: str,
            estimated_output_tokens: int,
            model: str,
        ) -> ScalewayChatResult:
            return ScalewayChatResult(
                provider_name="scaleway",
                model=model,
                mode="work",
                external_call_attempted=True,
                external_call_succeeded=True,
                response_text="   ",
                reported_input_tokens=1,
                reported_output_tokens=0,
                reported_total_tokens=1,
                sanitized_metadata={},
            )

    response = ScalewayProviderAdapter(EmptyProvider()).complete(
        AIRequest(
            task_type=AITaskType.synthesis,
            prompt="Reply OK.",
            model_preference=MODEL_ID,
            max_output_tokens=8,
        )
    )

    assert response.error is not None
    assert response.text is None
    assert response.blocked_reason == "scaleway_empty_completion"
    assert response.raw_provider_metadata["external_call_attempted"] is True
