from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.provider_registry import (
    ProviderRegistry,
    load_default_provider_registry,
)
from app.modules.ai.token_flow_continuation import evaluate_direct_continuation
from app.modules.ai.token_flow_external_continuation import (
    build_external_continuation_packet,
)
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import TokenFlowConflictError, create_flow

BODY = "Externally eligible partial output stopped at the exact length boundary."
NOW = datetime.now(UTC)
_PACKET_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _eligible_external():
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    registry = load_default_provider_registry()
    binding = registry.bindings["external:cheap"]
    flow = create_flow(
        task_kind="synthesis",
        requested_route_class="external:cheap",
    )
    flow_id = str(flow["id"])
    pricing = registry.models[(binding.provider_id, binding.model_id)].pricing
    assert pricing is not None
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, fallback_index,
                route_reason_json, output_digest, input_tokens, output_tokens,
                flow_id, flow_attempt_index, execution_class, adapter_invoked,
                external_dispatch_state, normalized_finish_reason,
                normalized_usage_source, accounting_basis,
                accounted_provider_spend_usd_decimal, outcome_reason,
                capability_version, pricing_version, accounting_version
            ) VALUES (
                'parent', ?, 'success', 'synthesis', 'external:cheap',
                ?, ?, ?, 0, ?, ?, 8, 11, ?, 0, 'external_provider', 1,
                'started', 'length', 'actual', 'provider_exact', '0.001',
                'success', 'provider-registry-v1', ?, 'token-flow-v0'
            )
            """,
            (
                utc_now(),
                binding.route_class,
                binding.provider_id,
                binding.model_id,
                '{"decision_reason":"silent_allow","fallback_attempt_index":0}',
                canonical_digest({"text": BODY}),
                flow_id,
                pricing.pricing_version,
            ),
        )
        connection.commit()
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="parent",
        body_text=BODY,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW,
    )
    decision = evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    )
    return decision, registry, binding


def test_builds_new_canonical_packet_projection_with_segment_digest(
    initialized_database,
) -> None:
    decision, registry, binding = _eligible_external()
    packet = build_external_continuation_packet(
        decision=decision,
        route_class="external:cheap",
        task_kind="synthesis",
        original_prompt="Complete the bounded engineering response.",
        workspace_id=None,
        prompt_level="S1",
        expected_sensitivity_level="S1",
        requested_output_tokens=300,
        bindings={"external:cheap": binding},
        registry=registry,
    )

    assert packet.binding == binding
    assert packet.material.provider_id == binding.provider_id
    assert packet.material.model_id == binding.model_id
    assert packet.material.max_output_tokens == min(300, binding.max_output_tokens)
    assert BODY in packet.material.prompt
    assert _PACKET_DIGEST_RE.fullmatch(packet.projection.packet_digest)
    assert packet.projection.projected_output_tokens == packet.material.max_output_tokens
    source_digests = json.loads(packet.projection.source_digests_json)
    assert source_digests == {"segment:0": canonical_digest({"text": BODY})}


def test_current_registry_pricing_is_used_for_each_projection(
    initialized_database,
) -> None:
    decision, registry, binding = _eligible_external()
    model_key = (binding.provider_id, binding.model_id)
    original_model = registry.models[model_key]
    assert original_model.pricing is not None
    updated_pricing = replace(
        original_model.pricing,
        pricing_version="continuation-price-test-v2",
        input_usd_per_1m_tokens=original_model.pricing.input_usd_per_1m_tokens + 0.1,
    )
    updated_models = dict(registry.models)
    updated_models[model_key] = replace(original_model, pricing=updated_pricing)
    updated_registry = ProviderRegistry(
        providers=registry.providers,
        models=updated_models,
        bindings=registry.bindings,
        fallback_chains=registry.fallback_chains,
    )

    packet = build_external_continuation_packet(
        decision=decision,
        route_class="external:cheap",
        task_kind="synthesis",
        original_prompt="Complete the response.",
        workspace_id=None,
        prompt_level="S1",
        expected_sensitivity_level="S1",
        requested_output_tokens=64,
        bindings={"external:cheap": binding},
        registry=updated_registry,
    )
    assert packet.projection.pricing_version == "continuation-price-test-v2"


def test_output_ceiling_is_recomputed_from_fresh_binding(
    initialized_database,
) -> None:
    decision, registry, binding = _eligible_external()
    smaller = replace(binding, max_output_tokens=32)
    packet = build_external_continuation_packet(
        decision=decision,
        route_class="external:cheap",
        task_kind="synthesis",
        original_prompt="Complete the response.",
        workspace_id=None,
        prompt_level="S1",
        expected_sensitivity_level="S1",
        requested_output_tokens=200,
        bindings={"external:cheap": smaller},
        registry=registry,
    )
    assert packet.material.max_output_tokens == 32


def test_local_or_incomplete_binding_cannot_build_external_packet(
    initialized_database,
) -> None:
    decision, registry, binding = _eligible_external()
    local = replace(
        binding,
        requires_network=False,
        execution_class="local_compute",
    )
    with pytest.raises(TokenFlowConflictError, match="external-provider metadata"):
        build_external_continuation_packet(
            decision=decision,
            route_class="external:cheap",
            task_kind="synthesis",
            original_prompt="Complete the response.",
            workspace_id=None,
            prompt_level="S1",
            expected_sensitivity_level="S1",
            requested_output_tokens=64,
            bindings={"external:cheap": local},
            registry=registry,
        )


def test_sensitive_or_stale_segment_state_fails_before_packet_projection(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    decision, registry, binding = _eligible_external()
    with pytest.raises(
        TokenFlowConflictError,
        match="segments must already be S0 or S1",
    ):
        build_external_continuation_packet(
            decision=decision,
            route_class="external:cheap",
            task_kind="synthesis",
            original_prompt="Complete the response.",
            workspace_id=None,
            prompt_level="S1",
            expected_sensitivity_level="S2",
            requested_output_tokens=64,
            bindings={"external:cheap": binding},
            registry=registry,
        )

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flow_segments SET body_text = 'tampered' WHERE flow_id = ?",
            (decision.flow_id,),
        )
        connection.commit()
    with pytest.raises(TokenFlowConflictError, match="digest evidence"):
        build_external_continuation_packet(
            decision=decision,
            route_class="external:cheap",
            task_kind="synthesis",
            original_prompt="Complete the response.",
            workspace_id=None,
            prompt_level="S1",
            expected_sensitivity_level="S1",
            requested_output_tokens=64,
            bindings={"external:cheap": binding},
            registry=registry,
        )
