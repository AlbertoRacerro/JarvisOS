from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.contracts import AITaskType
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_continuation import evaluate_direct_continuation
from app.modules.ai.token_flow_local_continuation import plan_local_continuation
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import TokenFlowConflictError, create_flow

BODY = "Partial answer that ends at a model output boundary."
NOW = datetime.now(UTC)


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _eligible_decision():
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    flow = create_flow(
        task_kind="synthesis",
        requested_route_class="local:test",
    )
    flow_id = str(flow["id"])
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
                capability_version, accounting_version
            ) VALUES (
                'parent', ?, 'success', 'synthesis', 'local:test',
                'local:test', 'local-test', 'model-test', 0, ?, ?, 5, 8,
                ?, 0, 'local_compute', 1, 'not_applicable', 'length',
                'estimated', 'local_compute_unpriced', '0', 'success',
                'provider-registry-v1', 'token-flow-v0'
            )
            """,
            (
                utc_now(),
                '{"decision_reason":"bound:local:test","fallback_attempt_index":0}',
                canonical_digest({"text": BODY}),
                flow_id,
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
    return evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    )


def _local_binding(
    *,
    context_window_tokens: int = 4096,
    max_output_tokens: int = 512,
) -> ProviderBinding:
    return ProviderBinding(
        route_class="local:test",
        provider_id="local-test",
        model_id="model-test",
        requires_network=False,
        max_output_tokens=max_output_tokens,
        execution_class="local_compute",
        context_window_tokens=context_window_tokens,
    )


def test_builds_fresh_local_request_without_invoking_adapter(
    initialized_database,
) -> None:
    decision = _eligible_decision()
    plan = plan_local_continuation(
        decision=decision,
        route_class="local:test",
        task_type=AITaskType.synthesis,
        original_prompt="Produce a complete bounded engineering explanation.",
        workspace_id=None,
        expected_sensitivity_level="S1",
        requested_output_tokens=300,
        bindings={"local:test": _local_binding()},
    )

    assert plan.ready is True
    assert plan.reason == "ready"
    assert plan.request is not None
    assert plan.request.model_preference == "model-test"
    assert plan.request.max_output_tokens == 300
    assert plan.request.correlation_id == decision.flow_id
    assert plan.request.metadata["continuation_parent_attempt_id"] == "parent"
    assert plan.request.metadata["continuation_index"] == 1
    assert BODY in plan.request.prompt
    assert "Do not restart" in plan.request.prompt


def test_fresh_binding_caps_output_ceiling(
    initialized_database,
) -> None:
    decision = _eligible_decision()
    plan = plan_local_continuation(
        decision=decision,
        route_class="local:test",
        task_type=AITaskType.synthesis,
        original_prompt="Continue the answer.",
        workspace_id=None,
        expected_sensitivity_level="S1",
        requested_output_tokens=900,
        bindings={"local:test": _local_binding(max_output_tokens=128)},
    )

    assert plan.ready is True
    assert plan.effective_output_tokens == 128
    assert plan.request is not None
    assert plan.request.max_output_tokens == 128


def test_external_binding_is_never_planned_as_local(
    initialized_database,
) -> None:
    decision = _eligible_decision()
    external = ProviderBinding(
        route_class="local:test",
        provider_id="external-test",
        model_id="external-model",
        requires_network=True,
        max_output_tokens=256,
        execution_class="external_provider",
        context_window_tokens=4096,
    )
    plan = plan_local_continuation(
        decision=decision,
        route_class="local:test",
        task_type=AITaskType.synthesis,
        original_prompt="Continue the answer.",
        workspace_id=None,
        expected_sensitivity_level="S1",
        requested_output_tokens=128,
        bindings={"local:test": external},
    )

    assert plan.ready is False
    assert plan.reason == "external_route_requires_059b"
    assert plan.request is None


def test_missing_binding_metadata_fails_closed(
    initialized_database,
) -> None:
    decision = _eligible_decision()
    incomplete = ProviderBinding(
        route_class="local:test",
        provider_id="unregistered-local",
        model_id="unregistered-model",
        requires_network=False,
        max_output_tokens=256,
        execution_class=None,
        context_window_tokens=None,
    )
    plan = plan_local_continuation(
        decision=decision,
        route_class="local:test",
        task_type=AITaskType.synthesis,
        original_prompt="Continue the answer.",
        workspace_id=None,
        expected_sensitivity_level="S1",
        requested_output_tokens=128,
        bindings={"local:test": incomplete},
    )

    assert plan.ready is False
    assert plan.reason == "binding_metadata_incomplete"


def test_context_capacity_is_recomputed_and_can_block(
    initialized_database,
) -> None:
    decision = _eligible_decision()
    plan = plan_local_continuation(
        decision=decision,
        route_class="local:test",
        task_type=AITaskType.synthesis,
        original_prompt="x" * 400,
        workspace_id=None,
        expected_sensitivity_level="S1",
        requested_output_tokens=80,
        bindings={
            "local:test": _local_binding(
                context_window_tokens=100,
                max_output_tokens=80,
            )
        },
    )

    assert plan.ready is False
    assert plan.reason == "context_capacity_exceeded"
    assert plan.estimated_input_tokens is not None
    assert plan.estimated_input_tokens + 80 > 100
    assert plan.request is None


def test_route_unavailable_and_noneligible_decisions_do_not_plan(
    initialized_database,
) -> None:
    decision = _eligible_decision()
    unavailable = plan_local_continuation(
        decision=decision,
        route_class="local:missing",
        task_type=AITaskType.synthesis,
        original_prompt="Continue.",
        workspace_id=None,
        expected_sensitivity_level="S1",
        requested_output_tokens=64,
        bindings={"local:test": _local_binding()},
    )
    assert unavailable.ready is False
    assert unavailable.reason == "route_unavailable"

    with pytest.raises(TokenFlowConflictError, match="eligible decision"):
        plan_local_continuation(
            decision=decision.__class__(
                eligible=False,
                reason="guard_exhausted",
                flow_id=decision.flow_id,
                parent_attempt_id=decision.parent_attempt_id,
                parent_flow_attempt_index=decision.parent_flow_attempt_index,
                next_continuation_index=decision.next_continuation_index,
                protected_segment_index=decision.protected_segment_index,
            ),
            route_class="local:test",
            task_type=AITaskType.synthesis,
            original_prompt="Continue.",
            workspace_id=None,
            expected_sensitivity_level="S1",
            requested_output_tokens=64,
            bindings={"local:test": _local_binding()},
        )
