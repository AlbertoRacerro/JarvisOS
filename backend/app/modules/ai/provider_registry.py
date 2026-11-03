from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from app.modules.ai.execution_types import ExecutionClass, ProviderBinding

PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ROUTE_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")
INLINE_SECRET_HINT_RE = re.compile(r"(?i)(sk-|bearer\s+|api[_-]?key|token)")
_EXECUTION_CLASSES = frozenset({"synthetic", "local_compute", "external_provider"})
_REQUIRED_PRICING_KEYS = frozenset(
    {
        "currency",
        "input_usd_per_1m_tokens",
        "output_usd_per_1m_tokens",
        "pricing_version",
        "pricing_effective_at",
    }
)
_OPTIONAL_PRICING_KEYS = frozenset({"cache_read_input_usd_per_million"})
_ALLOWED_PRICING_KEYS = _REQUIRED_PRICING_KEYS | _OPTIONAL_PRICING_KEYS
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    kind: str
    enabled: bool
    requires_network: bool
    base_url: str | None
    api_key_ref: str | None
    timeout_seconds: float
    monthly_token_cap: int
    monthly_cost_cap_usd: float
    execution_class: ExecutionClass | None = None


@dataclass(frozen=True)
class ModelPricing:
    currency: str
    input_usd_per_1m_tokens: float
    output_usd_per_1m_tokens: float
    pricing_version: str
    pricing_effective_at: str
    cache_read_input_usd_per_million: float | None = None


@dataclass(frozen=True)
class ModelConfig:
    provider_id: str
    model_id: str
    provider_model_name: str
    route_classes: tuple[str, ...]
    max_output_tokens: int
    pricing: ModelPricing | None
    context_window_tokens: int | None = None


@dataclass(frozen=True)
class FallbackEntry:
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class ProviderRegistry:
    providers: dict[str, ProviderConfig]
    models: dict[tuple[str, str], ModelConfig]
    bindings: dict[str, ProviderBinding]
    fallback_chains: dict[str, tuple[FallbackEntry, ...]]


def default_registry_path() -> Path:
    return Path(__file__).resolve().parents[4] / "configs" / "ai_providers.yaml"


def load_provider_registry(path: str | Path | None = None) -> ProviderRegistry:
    config_path = Path(path) if path is not None else default_registry_path()
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return parse_provider_registry(raw)


@lru_cache(maxsize=1)
def load_default_provider_registry() -> ProviderRegistry:
    return load_provider_registry(default_registry_path())


def parse_provider_registry(raw: dict[str, Any]) -> ProviderRegistry:
    if raw.get("version") != 1:
        raise ValueError("provider registry version must be 1")
    raw_providers = raw.get("providers")
    if not isinstance(raw_providers, dict) or not raw_providers:
        raise ValueError("provider registry requires providers")

    providers: dict[str, ProviderConfig] = {}
    models: dict[tuple[str, str], ModelConfig] = {}
    bindings: dict[str, ProviderBinding] = {}

    for provider_id, provider_raw in raw_providers.items():
        _validate_provider_id(provider_id)
        if not isinstance(provider_raw, dict):
            raise ValueError(f"provider {provider_id} must be a mapping")
        api_key_ref = provider_raw.get("api_key_ref")
        if api_key_ref is not None:
            _validate_api_key_ref(str(api_key_ref))
        provider = ProviderConfig(
            provider_id=provider_id,
            kind=_required_str(provider_raw, "kind", f"provider {provider_id}"),
            enabled=bool(provider_raw.get("enabled", False)),
            requires_network=bool(provider_raw.get("requires_network", False)),
            base_url=_optional_url(provider_raw.get("base_url"), provider_id),
            api_key_ref=str(api_key_ref) if api_key_ref is not None else None,
            timeout_seconds=_positive_float(
                provider_raw.get("timeout_seconds", 20),
                f"provider {provider_id} timeout_seconds",
                allow_zero=True,
            ),
            monthly_token_cap=_nonnegative_int(
                provider_raw.get("monthly_token_cap", 0),
                f"provider {provider_id} monthly_token_cap",
            ),
            monthly_cost_cap_usd=_nonnegative_float(
                provider_raw.get("monthly_cost_cap_usd", 0),
                f"provider {provider_id} monthly_cost_cap_usd",
            ),
            execution_class=_parse_execution_class(provider_raw, provider_id),
        )
        _validate_provider_execution_contract(provider)
        providers[provider_id] = provider

        raw_models = provider_raw.get("models")
        if not isinstance(raw_models, dict) or not raw_models:
            raise ValueError(f"provider {provider_id} requires models")
        for model_id, model_raw in raw_models.items():
            if not isinstance(model_raw, dict):
                raise ValueError(f"model {provider_id}/{model_id} must be a mapping")
            owner = f"model {provider_id}/{model_id}"
            routes = model_raw.get("route_classes")
            if not isinstance(routes, list) or not routes:
                raise ValueError(f"{owner} requires route_classes")
            route_tuple = tuple(str(route) for route in routes)
            if len(set(route_tuple)) != len(route_tuple):
                raise ValueError(f"{owner} has duplicate route_classes")
            for route in route_tuple:
                _validate_route_class(route)
                if route in bindings:
                    raise ValueError(f"duplicate route binding {route}")

            context_window_tokens = _positive_int(
                model_raw.get("context_window_tokens"),
                f"{owner} context_window_tokens",
            )
            max_tokens = _positive_int(
                model_raw.get("max_output_tokens"),
                f"{owner} max_output_tokens",
            )
            if max_tokens > context_window_tokens:
                raise ValueError(
                    f"{owner} max_output_tokens cannot exceed context_window_tokens"
                )
            pricing = _parse_model_pricing(
                model_raw.get("pricing"), provider=provider, model_id=str(model_id)
            )
            if provider.enabled and provider.execution_class == "external_provider" and pricing is None:
                raise ValueError(
                    f"enabled external model {provider_id}/{model_id} requires concrete pricing"
                )
            model = ModelConfig(
                provider_id=provider_id,
                model_id=str(model_id),
                provider_model_name=_required_str(
                    model_raw, "provider_model_name", owner
                ),
                route_classes=route_tuple,
                context_window_tokens=context_window_tokens,
                max_output_tokens=max_tokens,
                pricing=pricing,
            )
            models[(provider_id, str(model_id))] = model
            if provider.enabled:
                for route in route_tuple:
                    bindings[route] = ProviderBinding(
                        route_class=route,
                        provider_id=provider_id,
                        model_id=str(model_id),
                        requires_network=provider.requires_network,
                        max_output_tokens=max_tokens,
                        execution_class=provider.execution_class,
                        context_window_tokens=context_window_tokens,
                    )

    fallback_chains = _parse_fallback_chains(raw.get("fallback_chains", {}), bindings)
    return ProviderRegistry(
        providers=providers,
        models=models,
        bindings=bindings,
        fallback_chains=fallback_chains,
    )


def registry_bindings() -> dict[str, ProviderBinding]:
    return _bindings_with_env_overrides(load_default_provider_registry())


def resolve_model_pricing(
    registry: ProviderRegistry, provider_id: str, model_id: str
) -> ModelPricing:
    model = registry.models.get((provider_id, model_id))
    if model is None:
        raise ValueError(f"unknown concrete model {provider_id}/{model_id}")
    provider = registry.providers.get(provider_id)
    if provider is None or provider.execution_class != "external_provider":
        raise ValueError(f"model {provider_id}/{model_id} is not an external-provider binding")
    if model.pricing is None:
        raise ValueError(f"missing concrete pricing for {provider_id}/{model_id}")
    return model.pricing


def _bindings_with_env_overrides(
    registry: ProviderRegistry,
) -> dict[str, ProviderBinding]:
    result = dict(registry.bindings)
    llama_binding = result.get("local:llamacpp")
    if llama_binding is not None:
        from app.modules.local_ai.runtime.llama_cpp import llama_cpp_runtime_config

        configured_model_id = llama_cpp_runtime_config().model_id.strip()
        if configured_model_id:
            result["local:llamacpp"] = ProviderBinding(
                route_class=llama_binding.route_class,
                provider_id=llama_binding.provider_id,
                model_id=configured_model_id,
                requires_network=llama_binding.requires_network,
                max_output_tokens=llama_binding.max_output_tokens,
                execution_class=llama_binding.execution_class,
                context_window_tokens=llama_binding.context_window_tokens,
            )
    overrides = {
        "local:fake": ("AI_ROUTE_FAKE_MODEL",),
        "local:fast": ("AI_ROUTE_LOCAL_FAST_MODEL",),
        "local:general": ("AI_ROUTE_LOCAL_GENERAL_MODEL", "AI_ROUTE_LOCAL_MODEL"),
        "local:gemma": ("AI_ROUTE_LOCAL_GENERAL_MODEL", "AI_ROUTE_LOCAL_MODEL"),
        "local:coder": ("AI_ROUTE_LOCAL_CODER_MODEL",),
        "local:coder_heavy": ("AI_ROUTE_LOCAL_CODER_HEAVY_MODEL",),
        "external:cheap": ("AI_ROUTE_CHEAP_MODEL",),
        "external:reasoning": ("AI_ROUTE_REASONING_MODEL",),
    }
    for route, names in overrides.items():
        binding = result.get(route)
        if binding is None:
            continue
        for name in names:
            value = os.getenv(name)
            if not value:
                continue
            model = _resolve_override_model(
                registry=registry,
                binding=binding,
                route=route,
                configured_value=value,
            )
            result[route] = ProviderBinding(
                route_class=route,
                provider_id=binding.provider_id,
                model_id=model.model_id,
                requires_network=binding.requires_network,
                max_output_tokens=model.max_output_tokens,
                execution_class=binding.execution_class,
                context_window_tokens=model.context_window_tokens,
            )
            break
    return result


def _resolve_override_model(
    *,
    registry: ProviderRegistry,
    binding: ProviderBinding,
    route: str,
    configured_value: str,
) -> ModelConfig:
    matches = [
        model
        for model in registry.models.values()
        if model.provider_id == binding.provider_id
        and configured_value in {model.model_id, model.provider_model_name}
        and route in model.route_classes
    ]
    if len(matches) != 1:
        raise ValueError(
            f"model override for {route} must resolve uniquely to a registered model "
            f"configured for provider {binding.provider_id}"
        )
    model = matches[0]
    provider = registry.providers[binding.provider_id]
    if provider.execution_class != binding.execution_class:
        raise ValueError(f"model override for {route} changed execution class")
    if provider.execution_class == "external_provider" and model.pricing is None:
        raise ValueError(f"external model override for {route} requires concrete pricing")
    return model


def _parse_model_pricing(
    raw: Any, *, provider: ProviderConfig, model_id: str
) -> ModelPricing | None:
    owner = f"model {provider.provider_id}/{model_id}"
    if raw is None:
        return None
    if provider.execution_class != "external_provider":
        raise ValueError(f"{owner} cannot define external-provider pricing")
    if not isinstance(raw, dict):
        raise ValueError(f"{owner} pricing must be a mapping")
    keys = frozenset(raw)
    missing = _REQUIRED_PRICING_KEYS - keys
    extra = keys - _ALLOWED_PRICING_KEYS
    if missing:
        raise ValueError(f"{owner} pricing missing keys: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{owner} pricing has unsupported keys: {', '.join(sorted(extra))}")
    currency = _required_str(raw, "currency", f"{owner} pricing")
    if currency != "USD":
        raise ValueError(f"{owner} pricing currency must be USD")
    effective_at = _required_str(raw, "pricing_effective_at", f"{owner} pricing")
    _validate_aware_timestamp(effective_at, owner)
    ordinary_price = _positive_float if provider.enabled else _nonnegative_float
    input_price = ordinary_price(
        raw.get("input_usd_per_1m_tokens"),
        f"{owner} input_usd_per_1m_tokens",
    )
    cache_price_raw = raw.get("cache_read_input_usd_per_million")
    cache_price = (
        None
        if cache_price_raw is None
        else _nonnegative_float(
            cache_price_raw,
            f"{owner} cache_read_input_usd_per_million",
        )
    )
    if cache_price is not None and cache_price > input_price:
        raise ValueError(f"{owner} cache-read price cannot exceed ordinary input price")
    return ModelPricing(
        currency=currency,
        input_usd_per_1m_tokens=input_price,
        output_usd_per_1m_tokens=ordinary_price(
            raw.get("output_usd_per_1m_tokens"),
            f"{owner} output_usd_per_1m_tokens",
        ),
        pricing_version=_required_str(raw, "pricing_version", f"{owner} pricing"),
        pricing_effective_at=effective_at,
        cache_read_input_usd_per_million=cache_price,
    )


def _parse_fallback_chains(
    raw: Any, bindings: dict[str, ProviderBinding]
) -> dict[str, tuple[FallbackEntry, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("fallback_chains must be a mapping")
    chains: dict[str, tuple[FallbackEntry, ...]] = {}
    enabled_models = {
        (binding.provider_id, binding.model_id) for binding in bindings.values()
    }
    for route, chain_raw in raw.items():
        route = str(route)
        _validate_route_class(route)
        primary = bindings.get(route)
        if primary is None:
            raise ValueError(f"fallback chain route {route} is not bound")
        if not isinstance(chain_raw, list) or not chain_raw:
            raise ValueError(f"fallback chain {route} must be a non-empty list")
        chain = tuple(_parse_fallback_entry(str(item), route) for item in chain_raw)
        first = chain[0]
        if (first.provider_id, first.model_id) != (
            primary.provider_id,
            primary.model_id,
        ):
            raise ValueError(f"fallback chain {route} first entry must match primary binding")
        for item in chain:
            if (item.provider_id, item.model_id) not in enabled_models:
                raise ValueError(
                    f"fallback chain {route} references unknown or disabled model "
                    f"{item.provider_id}/{item.model_id}"
                )
        chains[route] = chain
    return chains


def _parse_fallback_entry(value: str, route: str) -> FallbackEntry:
    parts = value.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"fallback chain {route} entry must be provider_id/model_id"
        )
    provider_id, model_id = parts
    _validate_provider_id(provider_id)
    return FallbackEntry(provider_id=provider_id, model_id=model_id)


def _parse_execution_class(raw: dict[str, Any], provider_id: str) -> ExecutionClass:
    value = raw.get("execution_class")
    if value not in _EXECUTION_CLASSES:
        raise ValueError(
            f"provider {provider_id} execution_class must be one of "
            f"{', '.join(sorted(_EXECUTION_CLASSES))}"
        )
    return value


def _validate_provider_execution_contract(provider: ProviderConfig) -> None:
    owner = f"provider {provider.provider_id}"
    if provider.execution_class == "synthetic":
        if provider.kind != "fake" or provider.requires_network:
            raise ValueError(f"{owner} synthetic classification contradicts kind/egress metadata")
        if provider.base_url is not None or provider.api_key_ref is not None:
            raise ValueError(f"{owner} synthetic provider cannot define endpoint or credentials")
        return
    if provider.execution_class == "local_compute":
        if provider.kind != "local" or provider.requires_network:
            raise ValueError(f"{owner} local_compute classification contradicts kind/egress metadata")
        if provider.api_key_ref is not None:
            raise ValueError(f"{owner} local_compute provider cannot require external credentials")
        if provider.base_url is not None and not _is_loopback_url(provider.base_url):
            raise ValueError(f"{owner} local_compute endpoint must be loopback")
        return
    if provider.kind == "scaleway" and provider.provider_id != "scaleway":
        raise ValueError(f"{owner} dedicated Scaleway kind requires provider id scaleway")
    if provider.kind not in {"openai_compatible", "scaleway"} or not provider.requires_network:
        raise ValueError(f"{owner} external_provider classification contradicts kind/egress metadata")
    if provider.base_url is None or provider.api_key_ref is None:
        raise ValueError(f"{owner} external_provider requires endpoint and credential reference")


def _is_loopback_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and parsed.hostname in _LOOPBACK_HOSTS


def _required_str(raw: dict[str, Any], key: str, owner: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner} requires {key}")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _positive_float(value: Any, field: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field} must be {qualifier}")
    return parsed


def _nonnegative_float(value: Any, field: str) -> float:
    return _positive_float(value, field, allow_zero=True)


def _validate_aware_timestamp(value: str, owner: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{owner} pricing_effective_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{owner} pricing_effective_at must include a timezone")


def _optional_url(value: Any, provider_id: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip().rstrip("/")
    if INLINE_SECRET_HINT_RE.search(text):
        raise ValueError(f"provider {provider_id} base_url looks like a secret")
    if not text.startswith(("http://", "https://")):
        raise ValueError(f"provider {provider_id} base_url must be http(s)")
    return text


def _validate_provider_id(provider_id: str) -> None:
    if not PROVIDER_ID_RE.match(str(provider_id)):
        raise ValueError(f"malformed provider id {provider_id}")


def _validate_route_class(route_class: str) -> None:
    if not ROUTE_CLASS_RE.match(route_class):
        raise ValueError(f"malformed route class {route_class}")


def _validate_api_key_ref(api_key_ref: str) -> None:
    if not api_key_ref.startswith("env:"):
        raise ValueError("api_key_ref must be an env: reference")
    env_name = api_key_ref.removeprefix("env:")
    if not re.match(r"^[A-Z][A-Z0-9_]*$", env_name):
        raise ValueError("api_key_ref has malformed env var name")
    if INLINE_SECRET_HINT_RE.search(env_name.removesuffix("_API_KEY")):
        raise ValueError("api_key_ref looks like an inline secret")
