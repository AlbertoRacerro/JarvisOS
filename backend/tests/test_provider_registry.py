import copy

import pytest

from app.modules.ai.provider_registry import (
    ModelConfig,
    load_provider_registry,
    parse_provider_registry,
    registry_bindings,
    resolve_model_pricing,
)


def test_model_config_direct_constructor_preserves_legacy_signature():
    model = ModelConfig(
        "fake",
        "legacy-model",
        "legacy-model",
        ("local:fake",),
        128,
        None,
    )

    assert model.max_output_tokens == 128
    assert model.pricing is None
    assert model.context_window_tokens is None


def test_default_provider_registry_loads_with_complete_execution_metadata(monkeypatch):
    monkeypatch.delenv("AI_ROUTE_FAKE_MODEL", raising=False)
    monkeypatch.delenv("AI_ROUTE_CHEAP_MODEL", raising=False)
    monkeypatch.delenv("AI_ROUTE_REASONING_MODEL", raising=False)
    monkeypatch.delenv("SCALEWAY_MODEL", raising=False)
    registry = load_provider_registry()

    fake = registry.bindings["local:fake"]
    assert fake.provider_id == "fake"
    assert fake.model_id == "fake-deterministic-v1"
    assert fake.execution_class == "synthetic"
    assert fake.context_window_tokens == 4096
    assert fake.max_output_tokens == 256

    local = registry.bindings["local:fast"]
    assert local.execution_class == "local_compute"
    assert local.requires_network is False
    assert local.context_window_tokens == 8192

    scaleway = registry.bindings["external:scaleway"]
    assert registry.providers["scaleway"].kind == "scaleway"
    assert registry.providers["scaleway"].api_key_ref == "env:SCALEWAY_API_KEY"
    assert scaleway.provider_id == "scaleway"
    assert scaleway.model_id == "deepseek-v4-flash-0731"
    assert scaleway.execution_class == "external_provider"
    assert scaleway.requires_network is True
    assert scaleway.context_window_tokens == 8192
    assert scaleway.max_output_tokens == 512
    assert "external:scaleway" not in registry.fallback_chains

    cheap = registry.bindings["external:cheap"]
    assert cheap.provider_id == "deepseek"
    assert cheap.model_id == "deepseek-v4-pro"
    assert cheap.execution_class == "external_provider"
    assert cheap.context_window_tokens == 8192
    assert cheap.max_output_tokens == 512

    reasoning = registry.bindings["external:reasoning"]
    assert reasoning.provider_id == "glm"
    assert reasoning.model_id == "glm-5.2"
    assert reasoning.execution_class == "external_provider"
    assert reasoning.max_output_tokens == 1024
    assert registry.providers["deepseek"].api_key_ref == "env:DEEPSEEK_API_KEY"
    assert registry.providers["glm"].api_key_ref == "env:GLM_API_KEY"
    assert "external:kimi" not in registry.bindings

    deepseek_price = resolve_model_pricing(registry, "deepseek", "deepseek-v4-pro")
    assert deepseek_price.currency == "USD"
    assert deepseek_price.input_usd_per_1m_tokens == 1.32
    assert deepseek_price.output_usd_per_1m_tokens == 3.96
    assert deepseek_price.cache_read_input_usd_per_million is None
    assert deepseek_price.pricing_version == "deepseek-peak-2026-09-28"


def test_exact_external_model_override_uses_canonical_registry_metadata(monkeypatch):
    monkeypatch.setenv("AI_ROUTE_CHEAP_MODEL", "deepseek-v4-pro")
    monkeypatch.delenv("AI_ROUTE_REASONING_MODEL", raising=False)

    binding = registry_bindings()["external:cheap"]

    assert binding.provider_id == "deepseek"
    assert binding.model_id == "deepseek-v4-pro"
    assert binding.execution_class == "external_provider"
    assert binding.context_window_tokens == 8192
    assert binding.max_output_tokens == 512
    assert binding.requires_network is True


def test_external_model_override_rejects_provider_model_mismatch(monkeypatch):
    monkeypatch.setenv("AI_ROUTE_CHEAP_MODEL", "glm-5.2")
    monkeypatch.delenv("AI_ROUTE_REASONING_MODEL", raising=False)

    with pytest.raises(ValueError, match="must resolve uniquely to a registered model"):
        registry_bindings()


def test_external_model_override_rejects_unknown_model(monkeypatch):
    monkeypatch.setenv("AI_ROUTE_REASONING_MODEL", "unregistered-model")
    monkeypatch.delenv("AI_ROUTE_CHEAP_MODEL", raising=False)

    with pytest.raises(ValueError, match="must resolve uniquely to a registered model"):
        registry_bindings()


def test_local_model_override_must_resolve_to_registered_model(monkeypatch):
    monkeypatch.setenv("AI_ROUTE_FAKE_MODEL", "fake-deterministic-v1")
    monkeypatch.delenv("AI_ROUTE_CHEAP_MODEL", raising=False)
    monkeypatch.delenv("AI_ROUTE_REASONING_MODEL", raising=False)

    binding = registry_bindings()["local:fake"]

    assert binding.provider_id == "fake"
    assert binding.model_id == "fake-deterministic-v1"
    assert binding.execution_class == "synthetic"
    assert binding.context_window_tokens == 4096


def test_local_model_override_rejects_unregistered_model(monkeypatch):
    monkeypatch.setenv("AI_ROUTE_FAKE_MODEL", "local-test-override")
    monkeypatch.delenv("AI_ROUTE_CHEAP_MODEL", raising=False)
    monkeypatch.delenv("AI_ROUTE_REASONING_MODEL", raising=False)

    with pytest.raises(ValueError, match="must resolve uniquely to a registered model"):
        registry_bindings()


def test_registry_rejects_missing_required_fields():
    with pytest.raises(ValueError, match="requires providers"):
        parse_provider_registry({"version": 1})


def test_registry_rejects_missing_or_unknown_execution_class():
    raw = _minimal_registry()
    del raw["providers"]["fake"]["execution_class"]
    with pytest.raises(ValueError, match="execution_class must be one of"):
        parse_provider_registry(raw)

    raw = _minimal_registry()
    raw["providers"]["fake"]["execution_class"] = "free"
    with pytest.raises(ValueError, match="execution_class must be one of"):
        parse_provider_registry(raw)


def test_registry_rejects_contradictory_execution_metadata():
    raw = _minimal_registry()
    raw["providers"]["fake"]["requires_network"] = True
    with pytest.raises(ValueError, match="contradicts kind/egress metadata"):
        parse_provider_registry(raw)

    raw = _minimal_registry()
    raw["providers"]["fake"]["execution_class"] = "local_compute"
    with pytest.raises(ValueError, match="contradicts kind/egress metadata"):
        parse_provider_registry(raw)

    raw = _minimal_network_registry()
    raw["providers"]["network"]["execution_class"] = "local_compute"
    with pytest.raises(ValueError, match="contradicts kind/egress metadata"):
        parse_provider_registry(raw)


def test_registry_rejects_non_loopback_local_endpoint():
    raw = _minimal_registry()
    provider = raw["providers"]["fake"]
    provider["kind"] = "local"
    provider["execution_class"] = "local_compute"
    provider["base_url"] = "https://example.test"
    with pytest.raises(ValueError, match="endpoint must be loopback"):
        parse_provider_registry(raw)


def test_registry_rejects_external_provider_without_endpoint_or_credentials():
    raw = _minimal_network_registry()
    raw["providers"]["network"]["api_key_ref"] = None
    with pytest.raises(ValueError, match="requires endpoint and credential reference"):
        parse_provider_registry(raw)


def test_registry_rejects_missing_or_invalid_context_capability():
    raw = _minimal_registry()
    del raw["providers"]["fake"]["models"]["m"]["context_window_tokens"]
    with pytest.raises(ValueError, match="context_window_tokens must be a positive integer"):
        parse_provider_registry(raw)

    raw = _minimal_registry()
    raw["providers"]["fake"]["models"]["m"]["context_window_tokens"] = True
    with pytest.raises(ValueError, match="context_window_tokens must be a positive integer"):
        parse_provider_registry(raw)

    raw = _minimal_registry()
    raw["providers"]["fake"]["models"]["m"]["max_output_tokens"] = 11
    with pytest.raises(ValueError, match="cannot exceed context_window_tokens"):
        parse_provider_registry(raw)


def test_registry_rejects_malformed_route_class():
    raw = _minimal_registry()
    raw["providers"]["fake"]["models"]["m"]["route_classes"] = ["bad route"]
    with pytest.raises(ValueError, match="malformed route class"):
        parse_provider_registry(raw)


def test_registry_rejects_unknown_fallback_target():
    raw = _minimal_registry()
    raw["fallback_chains"] = {"local:fake": ["fake/m", "fake/missing"]}
    with pytest.raises(ValueError, match="unknown or disabled model"):
        parse_provider_registry(raw)


def test_registry_rejects_inline_looking_secret_values():
    raw = _minimal_registry()
    raw["providers"]["fake"]["api_key_ref"] = "sk-inline-secret"
    with pytest.raises(ValueError, match="api_key_ref must be an env"):
        parse_provider_registry(raw)


def test_registry_rejects_duplicate_routes_within_one_model():
    raw = _minimal_registry()
    raw["providers"]["fake"]["models"]["m"]["route_classes"] = [
        "local:fake",
        "local:fake",
    ]
    with pytest.raises(ValueError, match="duplicate route_classes"):
        parse_provider_registry(raw)


def test_registry_rejects_fallback_first_entry_mismatch():
    raw = _minimal_registry()
    raw["providers"]["other"] = {
        "kind": "fake",
        "execution_class": "synthetic",
        "enabled": True,
        "requires_network": False,
        "models": {
            "other-model": {
                "provider_model_name": "other-model",
                "route_classes": ["local:other"],
                "context_window_tokens": 10,
                "max_output_tokens": 1,
            }
        },
    }
    raw["fallback_chains"] = {"local:fake": ["other/other-model", "fake/m"]}
    with pytest.raises(ValueError, match="first entry must match primary binding"):
        parse_provider_registry(raw)


def test_disabled_provider_yields_no_bindings_but_loads_without_pricing():
    raw = _minimal_registry()
    raw["providers"]["disabled"] = {
        "kind": "openai_compatible",
        "execution_class": "external_provider",
        "enabled": False,
        "requires_network": True,
        "base_url": "https://example.test",
        "api_key_ref": "env:KIMI_API_KEY",
        "models": {
            "disabled-model": {
                "provider_model_name": "disabled-model",
                "route_classes": ["external:disabled"],
                "context_window_tokens": 20,
                "max_output_tokens": 10,
            }
        },
    }
    registry = parse_provider_registry(raw)
    assert "disabled" in registry.providers
    assert "external:disabled" not in registry.bindings
    with pytest.raises(ValueError, match="missing concrete pricing"):
        resolve_model_pricing(registry, "disabled", "disabled-model")


def test_enabled_external_model_requires_concrete_pricing():
    raw = _minimal_network_registry()
    del raw["providers"]["network"]["models"]["model"]["pricing"]
    with pytest.raises(ValueError, match="requires concrete pricing"):
        parse_provider_registry(raw)


def test_external_pricing_is_strict_and_timezone_aware():
    raw = _minimal_network_registry()
    pricing = raw["providers"]["network"]["models"]["model"]["pricing"]
    pricing["currency"] = "EUR"
    with pytest.raises(ValueError, match="currency must be USD"):
        parse_provider_registry(raw)

    raw = _minimal_network_registry()
    pricing = raw["providers"]["network"]["models"]["model"]["pricing"]
    pricing["pricing_effective_at"] = "2026-07-13T00:00:00"
    with pytest.raises(ValueError, match="must include a timezone"):
        parse_provider_registry(raw)

    raw = _minimal_network_registry()
    pricing = raw["providers"]["network"]["models"]["model"]["pricing"]
    pricing["unexpected"] = 1
    with pytest.raises(ValueError, match="unsupported keys"):
        parse_provider_registry(raw)


@pytest.mark.parametrize(
    "field",
    ["input_usd_per_1m_tokens", "output_usd_per_1m_tokens"],
)
def test_enabled_external_ordinary_prices_must_be_positive(field: str):
    raw = _minimal_network_registry()
    raw["providers"]["network"]["models"]["model"]["pricing"][field] = 0

    with pytest.raises(ValueError, match=rf"{field} must be positive"):
        parse_provider_registry(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in ("input_usd_per_1m_tokens", "output_usd_per_1m_tokens")
        for value in (float("nan"), float("inf"), float("-inf"))
    ],
)
def test_enabled_external_ordinary_prices_must_be_finite(
    field: str,
    value: float,
):
    raw = _minimal_network_registry()
    raw["providers"]["network"]["models"]["model"]["pricing"][field] = value

    with pytest.raises(ValueError, match=rf"{field} must be finite"):
        parse_provider_registry(raw)


def test_cache_read_price_is_optional_bounded_external_metadata():
    raw = _minimal_network_registry()
    pricing = raw["providers"]["network"]["models"]["model"]["pricing"]
    pricing["cache_read_input_usd_per_million"] = 0
    registry = parse_provider_registry(raw)
    resolved = resolve_model_pricing(registry, "network", "model")
    assert resolved.cache_read_input_usd_per_million == 0

    raw = _minimal_network_registry()
    pricing = raw["providers"]["network"]["models"]["model"]["pricing"]
    pricing["cache_read_input_usd_per_million"] = 0.25
    registry = parse_provider_registry(raw)
    resolved = resolve_model_pricing(registry, "network", "model")
    assert resolved.cache_read_input_usd_per_million == 0.25

    raw = _minimal_network_registry()
    pricing = raw["providers"]["network"]["models"]["model"]["pricing"]
    pricing["cache_read_input_usd_per_million"] = 1.5
    with pytest.raises(ValueError, match="cannot exceed ordinary input price"):
        parse_provider_registry(raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_cache_read_price_must_be_finite(value: float):
    raw = _minimal_network_registry()
    pricing = raw["providers"]["network"]["models"]["model"]["pricing"]
    pricing["cache_read_input_usd_per_million"] = value

    with pytest.raises(ValueError, match="cache_read_input_usd_per_million must be finite"):
        parse_provider_registry(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", float("nan")),
        ("monthly_cost_cap_usd", float("inf")),
        ("monthly_cost_cap_usd", float("-inf")),
    ],
)
def test_generic_provider_numeric_fields_must_be_finite(field: str, value: float):
    raw = _minimal_network_registry()
    raw["providers"]["network"][field] = value

    with pytest.raises(ValueError, match=rf"{field} must be finite"):
        parse_provider_registry(raw)


def test_generic_provider_numeric_zero_semantics_are_preserved():
    raw = _minimal_network_registry()
    provider = raw["providers"]["network"]
    provider["timeout_seconds"] = 0
    provider["monthly_cost_cap_usd"] = 0

    registry = parse_provider_registry(raw)

    assert registry.providers["network"].timeout_seconds == 0
    assert registry.providers["network"].monthly_cost_cap_usd == 0


def test_disabled_external_models_preserve_nonnegative_price_metadata():
    raw = _minimal_network_registry()
    provider = raw["providers"]["network"]
    provider["enabled"] = False
    pricing = provider["models"]["model"]["pricing"]
    pricing["input_usd_per_1m_tokens"] = 0
    pricing["output_usd_per_1m_tokens"] = 0
    raw["fallback_chains"] = {}

    registry = parse_provider_registry(raw)

    assert registry.bindings == {}
    resolved = resolve_model_pricing(registry, "network", "model")
    assert resolved.input_usd_per_1m_tokens == 0
    assert resolved.output_usd_per_1m_tokens == 0


def test_non_external_models_still_cannot_define_price_metadata():
    raw = _minimal_registry()
    raw["providers"]["fake"]["models"]["m"]["pricing"] = copy.deepcopy(
        _minimal_network_registry()["providers"]["network"]["models"]["model"][
            "pricing"
        ]
    )

    with pytest.raises(ValueError, match="cannot define external-provider pricing"):
        parse_provider_registry(raw)


def test_resolve_model_pricing_fails_for_unknown_or_non_external_models():
    registry = parse_provider_registry(_minimal_registry())
    with pytest.raises(ValueError, match="unknown concrete model"):
        resolve_model_pricing(registry, "fake", "missing")
    with pytest.raises(ValueError, match="not an external-provider binding"):
        resolve_model_pricing(registry, "fake", "m")


def _minimal_registry():
    return copy.deepcopy(
        {
            "version": 1,
            "providers": {
                "fake": {
                    "kind": "fake",
                    "execution_class": "synthetic",
                    "enabled": True,
                    "requires_network": False,
                    "models": {
                        "m": {
                            "provider_model_name": "m",
                            "route_classes": ["local:fake"],
                            "context_window_tokens": 10,
                            "max_output_tokens": 1,
                        }
                    },
                }
            },
            "fallback_chains": {"local:fake": ["fake/m"]},
        }
    )


def _minimal_network_registry():
    return {
        "version": 1,
        "providers": {
            "network": {
                "kind": "openai_compatible",
                "execution_class": "external_provider",
                "enabled": True,
                "requires_network": True,
                "base_url": "https://example.test",
                "api_key_ref": "env:NETWORK_API_KEY",
                "models": {
                    "model": {
                        "provider_model_name": "model",
                        "route_classes": ["external:test"],
                        "context_window_tokens": 20,
                        "max_output_tokens": 10,
                        "pricing": {
                            "currency": "USD",
                            "input_usd_per_1m_tokens": 1.0,
                            "output_usd_per_1m_tokens": 2.0,
                            "pricing_version": "test-v1",
                            "pricing_effective_at": "2026-07-13T00:00:00Z",
                        },
                    }
                },
            }
        },
        "fallback_chains": {"external:test": ["network/model"]},
    }
