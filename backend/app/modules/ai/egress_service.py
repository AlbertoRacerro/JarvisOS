from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.modules.ai.egress_policy import (
    EXTERNAL_PROVIDER_OPERATION,
    EgressPolicyConfig,
    load_default_egress_policy,
)
from app.modules.ai.provider_registry import (
    ModelPricing,
    ProviderRegistry,
    load_default_provider_registry,
    resolve_model_pricing,
)

_SAFE_EXTERNAL_LEVELS = frozenset({"S0", "S1"})
_LEVEL_RANK = {"S0": 0, "S1": 1}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_SAFE_METADATA_KEYS = frozenset(
    {
        "authorization",
        "authorization_header",
        "body",
        "content",
        "credential",
        "credentials",
        "derivative_content",
        "packet_json",
        "prompt",
        "raw_payload",
        "secret",
        "text",
    }
)


class EgressContractError(ValueError):
    """Fail-closed validation error for deterministic 059b packet construction."""


@dataclass(frozen=True)
class EgressPacketMaterial:
    operation: str
    task_kind: str
    route_class: str
    provider_id: str
    model_id: str
    fallback_index: int
    prompt: str
    context_blocks: tuple[dict[str, Any], ...]
    prompt_level: str
    context_level: str
    final_level: str
    max_output_tokens: int
    workspace_id: str | None = None
    prompt_derivative_id: str | None = None
    included_manifest: tuple[dict[str, Any], ...] = ()
    withheld_manifest: tuple[dict[str, Any], ...] = ()
    sanitizer_failed_manifest: tuple[dict[str, Any], ...] = ()
    policy_capped_manifest: tuple[dict[str, Any], ...] = ()
    budget_dropped_manifest: tuple[dict[str, Any], ...] = ()
    source_digests: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EgressPacketProjection:
    packet_json: str
    packet_digest: str
    prompt_digest: str
    safe_input_digest: str
    included_manifest_json: str
    withheld_manifest_json: str
    sanitizer_failed_manifest_json: str
    policy_capped_manifest_json: str
    budget_dropped_manifest_json: str
    source_digests_json: str
    projected_input_tokens: int
    projected_output_tokens: int
    projected_cost_upper_usd: float
    pricing_version: str
    pricing_effective_at: str
    policy_version: str
    trigger_version: str
    config_digest: str
    source_count: int
    included_count: int
    withheld_count: int


def build_packet_projection(
    material: EgressPacketMaterial,
    *,
    policy: EgressPolicyConfig | None = None,
    registry: ProviderRegistry | None = None,
) -> EgressPacketProjection:
    """Validate and canonically bind one concrete external-provider attempt.

    This function is pure: it performs no database write, secret lookup, reservation,
    ticket transition, or provider call. Persistence and execution may consume only the
    returned canonical strings and digests, never mutable caller fields.
    """

    policy = policy or load_default_egress_policy()
    registry = registry or load_default_provider_registry()
    _validate_material(material, policy=policy)
    pricing = _validate_concrete_binding(material, registry=registry)

    context_blocks = list(material.context_blocks)
    packet_payload = {
        "context_blocks": context_blocks,
        "prompt": material.prompt,
    }
    packet_json = canonical_json(packet_payload)
    prompt_digest = sha256_text(material.prompt)
    context_block_digests = [sha256_text(canonical_json(block)) for block in context_blocks]

    included_manifest_json = _canonical_safe_manifest(material.included_manifest, "included_manifest")
    withheld_manifest_json = _canonical_safe_manifest(material.withheld_manifest, "withheld_manifest")
    sanitizer_failed_manifest_json = _canonical_safe_manifest(
        material.sanitizer_failed_manifest, "sanitizer_failed_manifest"
    )
    policy_capped_manifest_json = _canonical_safe_manifest(
        material.policy_capped_manifest, "policy_capped_manifest"
    )
    budget_dropped_manifest_json = _canonical_safe_manifest(
        material.budget_dropped_manifest, "budget_dropped_manifest"
    )
    source_digests = _canonical_source_digests(material.source_digests)
    source_digests_json = canonical_json(source_digests)

    safe_input = {
        "context_block_digests": context_block_digests,
        "context_level": material.context_level,
        "fallback_index": material.fallback_index,
        "final_level": material.final_level,
        "max_output_tokens": material.max_output_tokens,
        "model_id": material.model_id,
        "operation": material.operation,
        "prompt_derivative_id": material.prompt_derivative_id,
        "prompt_digest": prompt_digest,
        "prompt_level": material.prompt_level,
        "provider_id": material.provider_id,
        "route_class": material.route_class,
        "source_digests": source_digests,
        "task_kind": material.task_kind,
        "workspace_id": material.workspace_id,
    }
    safe_input_digest = sha256_text(canonical_json(safe_input))

    packet_envelope = {
        **safe_input,
        "budget_dropped_manifest": json.loads(budget_dropped_manifest_json),
        "config_digest": policy.config_digest,
        "included_manifest": json.loads(included_manifest_json),
        "packet": packet_payload,
        "policy_capped_manifest": json.loads(policy_capped_manifest_json),
        "policy_version": policy.policy_version,
        "sanitizer_failed_manifest": json.loads(sanitizer_failed_manifest_json),
        "trigger_version": policy.trigger_version,
        "withheld_manifest": json.loads(withheld_manifest_json),
    }
    packet_digest = sha256_text(canonical_json(packet_envelope))

    projected_input_tokens = max(1, len(packet_json.encode("utf-8")))
    projected_output_tokens = material.max_output_tokens
    projected_cost = _projected_cost_upper_usd(
        input_tokens=projected_input_tokens,
        output_tokens=projected_output_tokens,
        pricing=pricing,
    )

    return EgressPacketProjection(
        packet_json=packet_json,
        packet_digest=packet_digest,
        prompt_digest=prompt_digest,
        safe_input_digest=safe_input_digest,
        included_manifest_json=included_manifest_json,
        withheld_manifest_json=withheld_manifest_json,
        sanitizer_failed_manifest_json=sanitizer_failed_manifest_json,
        policy_capped_manifest_json=policy_capped_manifest_json,
        budget_dropped_manifest_json=budget_dropped_manifest_json,
        source_digests_json=source_digests_json,
        projected_input_tokens=projected_input_tokens,
        projected_output_tokens=projected_output_tokens,
        projected_cost_upper_usd=projected_cost,
        pricing_version=pricing.pricing_version,
        pricing_effective_at=pricing.pricing_effective_at,
        policy_version=policy.policy_version,
        trigger_version=policy.trigger_version,
        config_digest=policy.config_digest,
        source_count=len(source_digests),
        included_count=len(material.included_manifest),
        withheld_count=len(material.withheld_manifest),
    )


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EgressContractError("egress value is not canonical-JSON serializable") from exc


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise EgressContractError("digest input must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sanitizer_sample_value(
    *,
    derivative_kind: str,
    derivative_id: str,
    derivative_digest: str,
    iso_week: str,
    policy_version: str,
) -> int:
    if derivative_kind not in {"canonical", "prompt"}:
        raise EgressContractError("unsupported sanitizer derivative kind")
    for field_name, value in (
        ("derivative_id", derivative_id),
        ("iso_week", iso_week),
        ("policy_version", policy_version),
    ):
        _required_text(value, field_name)
    digest_pattern = (
        _CANONICAL_SHA256_RE if derivative_kind == "canonical" else _SHA256_RE
    )
    if not digest_pattern.fullmatch(derivative_digest):
        raise EgressContractError(
            f"{derivative_kind} derivative_digest must use its canonical lowercase SHA-256 format"
        )
    payload = canonical_json(
        {
            "derivative_digest": derivative_digest,
            "derivative_id": derivative_id,
            "derivative_kind": derivative_kind,
            "iso_week": iso_week,
            "policy_version": policy_version,
        }
    )
    return int(sha256_text(payload)[:16], 16) % 10_000


def sanitizer_should_sample(
    *,
    derivative_kind: str,
    derivative_id: str,
    derivative_digest: str,
    iso_week: str,
    policy_version: str,
    sample_rate_bps: int,
) -> bool:
    if isinstance(sample_rate_bps, bool) or not isinstance(sample_rate_bps, int):
        raise EgressContractError("sample_rate_bps must be an integer")
    if sample_rate_bps < 500 or sample_rate_bps > 10_000:
        raise EgressContractError("sample_rate_bps must be between 500 and 10000")
    return (
        sanitizer_sample_value(
            derivative_kind=derivative_kind,
            derivative_id=derivative_id,
            derivative_digest=derivative_digest,
            iso_week=iso_week,
            policy_version=policy_version,
        )
        < sample_rate_bps
    )


def _validate_material(material: EgressPacketMaterial, *, policy: EgressPolicyConfig) -> None:
    if material.operation != EXTERNAL_PROVIDER_OPERATION:
        raise EgressContractError("unsupported egress operation")
    if material.operation not in policy.supported_operations:
        raise EgressContractError("egress operation is not enabled by policy")
    for field_name, value in (
        ("task_kind", material.task_kind),
        ("route_class", material.route_class),
        ("provider_id", material.provider_id),
        ("model_id", material.model_id),
        ("prompt", material.prompt),
    ):
        _required_text(value, field_name)
    if isinstance(material.fallback_index, bool) or not isinstance(material.fallback_index, int):
        raise EgressContractError("fallback_index must be an integer")
    if material.fallback_index < 0:
        raise EgressContractError("fallback_index must be non-negative")
    if isinstance(material.max_output_tokens, bool) or not isinstance(material.max_output_tokens, int):
        raise EgressContractError("max_output_tokens must be an integer")
    if material.max_output_tokens <= 0:
        raise EgressContractError("max_output_tokens must be positive")
    if len(material.prompt) > policy.max_prompt_chars:
        raise EgressContractError("prompt exceeds egress policy character cap")
    if len(material.context_blocks) > policy.max_context_blocks:
        raise EgressContractError("context block count exceeds egress policy cap")
    context_json = canonical_json(list(material.context_blocks))
    if len(context_json) > policy.max_context_chars:
        raise EgressContractError("context exceeds egress policy character cap")
    for index, block in enumerate(material.context_blocks):
        if not isinstance(block, dict):
            raise EgressContractError(f"context block {index} must be an object")
    for level_name, level in (
        ("prompt_level", material.prompt_level),
        ("context_level", material.context_level),
        ("final_level", material.final_level),
    ):
        if level not in _SAFE_EXTERNAL_LEVELS:
            raise EgressContractError(f"{level_name} must be effective S0 or S1")
    expected_final = max(
        (material.prompt_level, material.context_level),
        key=_LEVEL_RANK.__getitem__,
    )
    if material.final_level != expected_final:
        raise EgressContractError("final_level must equal the maximum effective packet level")
    if material.prompt_derivative_id is not None:
        _required_text(material.prompt_derivative_id, "prompt_derivative_id")
    if material.workspace_id is not None:
        _required_text(material.workspace_id, "workspace_id")


def _validate_concrete_binding(
    material: EgressPacketMaterial, *, registry: ProviderRegistry
) -> ModelPricing:
    provider = registry.providers.get(material.provider_id)
    if provider is None or not provider.enabled or not provider.requires_network:
        raise EgressContractError("packet provider is not an enabled network provider")
    model = registry.models.get((material.provider_id, material.model_id))
    if model is None:
        raise EgressContractError("packet model is not configured for the provider")
    if material.max_output_tokens > model.max_output_tokens:
        raise EgressContractError("max_output_tokens exceeds configured model capability")

    chain = registry.fallback_chains.get(material.route_class)
    if chain is None:
        binding = registry.bindings.get(material.route_class)
        valid = (
            material.fallback_index == 0
            and binding is not None
            and binding.requires_network
            and (binding.provider_id, binding.model_id)
            == (material.provider_id, material.model_id)
        )
    else:
        valid = material.fallback_index < len(chain) and (
            chain[material.fallback_index].provider_id,
            chain[material.fallback_index].model_id,
        ) == (material.provider_id, material.model_id)
    if not valid:
        raise EgressContractError("provider/model is not the configured binding at fallback_index")
    try:
        return resolve_model_pricing(registry, material.provider_id, material.model_id)
    except ValueError as exc:
        raise EgressContractError(str(exc)) from exc


def _canonical_safe_manifest(items: tuple[dict[str, Any], ...], field_name: str) -> str:
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise EgressContractError(f"{field_name}[{index}] must be an object")
        _assert_safe_metadata(item, path=f"{field_name}[{index}]")
    return canonical_json(list(items))


def _assert_safe_metadata(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise EgressContractError(f"{path} contains a non-text key")
            if key.casefold() in _FORBIDDEN_SAFE_METADATA_KEYS:
                raise EgressContractError(f"{path} contains forbidden body field {key}")
            _assert_safe_metadata(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_metadata(child, path=f"{path}[{index}]")
    elif value is not None and not isinstance(value, str | int | float | bool):
        raise EgressContractError(f"{path} contains unsupported metadata type")


def _canonical_source_digests(items: tuple[tuple[str, str], ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for source_ref, digest in items:
        _required_text(source_ref, "source_ref")
        if source_ref in result:
            raise EgressContractError("source_digests contains duplicate source_ref")
        if _CANONICAL_SHA256_RE.fullmatch(digest):
            canonical_digest = digest
        elif _SHA256_RE.fullmatch(digest):
            canonical_digest = f"sha256:{digest}"
        else:
            raise EgressContractError("source digest must be a lowercase SHA-256 digest")
        result[source_ref] = canonical_digest
    return result


def _projected_cost_upper_usd(
    *, input_tokens: int, output_tokens: int, pricing: ModelPricing
) -> float:
    return (
        input_tokens * pricing.input_usd_per_1m_tokens
        + output_tokens * pricing.output_usd_per_1m_tokens
    ) / 1_000_000


def _required_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EgressContractError(f"{field_name} must be non-empty text")
