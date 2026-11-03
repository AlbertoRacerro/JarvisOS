from app.modules.ai.egress_authority import _sanitizer_config_digest
from app.modules.ai.egress_policy import load_default_egress_policy
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import (
    FallbackEntry,
    ModelConfig,
    ProviderConfig,
    ProviderRegistry,
)


def _registry(*, model_id: str, max_output_tokens: int) -> ProviderRegistry:
    provider = ProviderConfig(
        provider_id="fake",
        kind="fake",
        enabled=True,
        requires_network=False,
        base_url=None,
        api_key_ref=None,
        timeout_seconds=1.0,
        monthly_token_cap=0,
        monthly_cost_cap_usd=0.0,
    )
    model = ModelConfig(
        provider_id="fake",
        model_id=model_id,
        provider_model_name=model_id,
        route_classes=("local:fake",),
        max_output_tokens=max_output_tokens,
        pricing=None,
    )
    binding = ProviderBinding(
        route_class="local:fake",
        provider_id="fake",
        model_id=model_id,
        requires_network=False,
        max_output_tokens=max_output_tokens,
    )
    return ProviderRegistry(
        providers={"fake": provider},
        models={("fake", model_id): model},
        bindings={"local:fake": binding},
        fallback_chains={
            "local:fake": (FallbackEntry("fake", model_id),)
        },
    )


def _digest(registry: ProviderRegistry) -> str:
    return _sanitizer_config_digest(
        policy=load_default_egress_policy(),
        route_class="local:fake",
        template="test sanitizer template",
        version="test-sanitizer-v1",
        registry=registry,
    )


def test_sanitizer_config_digest_binds_concrete_model_and_token_ceiling():
    baseline = _digest(
        _registry(model_id="fake-deterministic-v1", max_output_tokens=512)
    )
    changed_model = _digest(
        _registry(model_id="fake-deterministic-v2", max_output_tokens=512)
    )
    changed_ceiling = _digest(
        _registry(model_id="fake-deterministic-v1", max_output_tokens=256)
    )

    assert baseline != changed_model
    assert baseline != changed_ceiling
