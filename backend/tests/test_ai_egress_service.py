from dataclasses import replace

import pytest

from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION, load_default_egress_policy
from app.modules.ai.egress_service import (
    EgressContractError,
    EgressPacketMaterial,
    build_packet_projection,
    canonical_json,
    sanitizer_sample_value,
    sanitizer_should_sample,
    sha256_text,
)
from app.modules.ai.provider_registry import load_default_provider_registry, resolve_model_pricing


def _material(**overrides) -> EgressPacketMaterial:
    values = {
        "operation": EXTERNAL_PROVIDER_OPERATION,
        "task_kind": "general",
        "route_class": "external:cheap",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "fallback_index": 0,
        "prompt": "Summarize the approved public-domain pump note.",
        "context_blocks": (
            {
                "source": "derivative:derivative-1",
                "content": "Generic pump sizing note.",
            },
        ),
        "prompt_level": "S1",
        "context_level": "S1",
        "final_level": "S1",
        "max_output_tokens": 128,
        "workspace_id": "main",
        "included_manifest": (
            {
                "derivative_id": "derivative-1",
                "effective_level": "S1",
                "source_ref": "decision:decision-1",
            },
        ),
        "source_digests": (
            ("decision:decision-1", sha256_text("source-body")),
        ),
    }
    values.update(overrides)
    return EgressPacketMaterial(**values)


def test_projection_is_deterministic_and_binds_exact_attempt_metadata():
    policy = load_default_egress_policy()
    registry = load_default_provider_registry()
    material = _material()

    first = build_packet_projection(material, policy=policy, registry=registry)
    second = build_packet_projection(material, policy=policy, registry=registry)

    assert first == second
    assert first.packet_digest == second.packet_digest
    assert first.prompt_digest == sha256_text(material.prompt)
    assert first.projected_input_tokens == len(first.packet_json.encode("utf-8"))
    assert first.projected_output_tokens == 128
    price = resolve_model_pricing(registry, material.provider_id, material.model_id)
    assert first.projected_cost_upper_usd == pytest.approx(
        (first.projected_input_tokens * price.input_usd_per_1m_tokens
         + 128 * price.output_usd_per_1m_tokens) / 1_000_000
    )
    assert first.pricing_version == price.pricing_version
    assert first.policy_version == policy.policy_version
    assert first.trigger_version == policy.trigger_version
    assert first.config_digest == policy.config_digest
    assert first.source_count == 1
    assert first.included_count == 1
    assert first.withheld_count == 0
    assert material.prompt not in first.safe_input_digest


def test_source_digest_representations_collapse_to_one_canonical_packet_binding():
    digest = sha256_text("source-body")
    bare = build_packet_projection(
        _material(source_digests=(("decision:decision-1", digest),))
    )
    prefixed = build_packet_projection(
        _material(source_digests=(("decision:decision-1", f"sha256:{digest}"),))
    )

    assert bare == prefixed
    assert bare.source_digests_json == canonical_json(
        {"decision:decision-1": f"sha256:{digest}"}
    )


def test_projection_digest_changes_with_binding_tokens_content_or_policy():
    base = _material()
    policy = load_default_egress_policy()
    registry = load_default_provider_registry()
    original = build_packet_projection(base, policy=policy, registry=registry)

    changed_prompt = build_packet_projection(
        replace(base, prompt=base.prompt + " Changed."),
        policy=policy,
        registry=registry,
    )
    changed_tokens = build_packet_projection(
        replace(base, max_output_tokens=127),
        policy=policy,
        registry=registry,
    )
    changed_policy = build_packet_projection(
        base,
        policy=replace(policy, policy_version="egress-policy-v2"),
        registry=registry,
    )

    assert changed_prompt.packet_digest != original.packet_digest
    assert changed_tokens.packet_digest != original.packet_digest
    assert changed_policy.packet_digest != original.packet_digest


def test_configured_fallback_index_is_part_of_binding_authority():
    registry = load_default_provider_registry()
    fallback = _material(
        provider_id="glm",
        model_id="glm-5.2",
        fallback_index=1,
        max_output_tokens=256,
    )

    projection = build_packet_projection(fallback, registry=registry)

    assert projection.projected_output_tokens == 256

    with pytest.raises(EgressContractError, match="configured binding"):
        build_packet_projection(replace(fallback, fallback_index=0), registry=registry)


def test_projection_rejects_noneligible_or_incoherent_levels():
    with pytest.raises(EgressContractError, match="effective S0 or S1"):
        build_packet_projection(_material(prompt_level="S2", final_level="S2"))

    with pytest.raises(EgressContractError, match="maximum effective packet level"):
        build_packet_projection(
            _material(prompt_level="S1", context_level="S0", final_level="S0")
        )


def test_projection_rejects_unknown_binding_and_capability_overshoot():
    with pytest.raises(EgressContractError, match="not configured"):
        build_packet_projection(_material(model_id="unknown-model"))

    with pytest.raises(EgressContractError, match="exceeds configured model capability"):
        build_packet_projection(_material(max_output_tokens=513))


def test_projection_rejects_body_fields_inside_safe_manifests():
    with pytest.raises(EgressContractError, match="forbidden body field content"):
        build_packet_projection(
            _material(
                included_manifest=(
                    {
                        "source_ref": "decision:decision-1",
                        "content": "body must not be copied into safe metadata",
                    },
                )
            )
        )


def test_projection_enforces_policy_prompt_and_context_caps():
    policy = load_default_egress_policy()
    with pytest.raises(EgressContractError, match="prompt exceeds"):
        build_packet_projection(
            _material(prompt="x" * (policy.max_prompt_chars + 1)),
            policy=policy,
        )

    with pytest.raises(EgressContractError, match="context block count"):
        build_packet_projection(
            _material(context_blocks=tuple({"content": "x"} for _ in range(25))),
            policy=policy,
        )


def test_sampling_is_deterministic_version_bound_and_thresholded():
    digest = sha256_text("approved derivative")
    kwargs = {
        "derivative_kind": "prompt",
        "derivative_id": "derivative-1",
        "derivative_digest": digest,
        "iso_week": "2026-W29",
        "policy_version": "egress-policy-v1",
    }

    first = sanitizer_sample_value(**kwargs)
    second = sanitizer_sample_value(**kwargs)
    changed = sanitizer_sample_value(**{**kwargs, "policy_version": "egress-policy-v2"})

    assert first == second
    assert 0 <= first <= 9999
    assert changed != first
    assert sanitizer_should_sample(**kwargs, sample_rate_bps=10_000) is True
    assert sanitizer_should_sample(**kwargs, sample_rate_bps=500) is (first < 500)


def test_sampling_accepts_only_the_digest_format_owned_by_each_derivative_kind():
    digest = sha256_text("approved derivative")
    prompt_kwargs = {
        "derivative_kind": "prompt",
        "derivative_id": "prompt-derivative-1",
        "derivative_digest": digest,
        "iso_week": "2026-W29",
        "policy_version": "egress-policy-v1",
    }
    canonical_kwargs = {
        "derivative_kind": "canonical",
        "derivative_id": "canonical-derivative-1",
        "derivative_digest": f"sha256:{digest}",
        "iso_week": "2026-W29",
        "policy_version": "egress-policy-v1",
    }

    assert sanitizer_sample_value(**prompt_kwargs) == sanitizer_sample_value(
        **prompt_kwargs
    )
    assert sanitizer_sample_value(**canonical_kwargs) == sanitizer_sample_value(
        **canonical_kwargs
    )
    assert sanitizer_should_sample(**canonical_kwargs, sample_rate_bps=10_000) is True

    with pytest.raises(EgressContractError, match="canonical lowercase SHA-256"):
        sanitizer_sample_value(
            **{**canonical_kwargs, "derivative_digest": digest}
        )
    with pytest.raises(EgressContractError, match="canonical lowercase SHA-256"):
        sanitizer_sample_value(
            **{**prompt_kwargs, "derivative_digest": f"sha256:{digest}"}
        )


def test_sampling_and_canonical_json_fail_closed_on_malformed_values():
    with pytest.raises(EgressContractError, match="lowercase SHA-256"):
        sanitizer_sample_value(
            derivative_kind="prompt",
            derivative_id="derivative-1",
            derivative_digest="not-a-digest",
            iso_week="2026-W29",
            policy_version="egress-policy-v1",
        )

    with pytest.raises(EgressContractError, match="between 500 and 10000"):
        sanitizer_should_sample(
            derivative_kind="prompt",
            derivative_id="derivative-1",
            derivative_digest=sha256_text("approved derivative"),
            iso_week="2026-W29",
            policy_version="egress-policy-v1",
            sample_rate_bps=499,
        )

    with pytest.raises(EgressContractError, match="canonical-JSON"):
        canonical_json({"invalid": float("nan")})
