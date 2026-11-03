from __future__ import annotations

import test_token_flow_external_runtime_integration as integration

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.egress_confirmation import run_confirmation_ticket
from app.modules.ai.token_flow_service import get_flow

initialized_database = integration.initialized_database


class _ConfirmedLengthSequenceAdapter:
    provider_id = integration.BINDING.provider_id

    def __init__(self, *, final_finish_reason: str = "stop") -> None:
        self.final_finish_reason = final_finish_reason
        self.requests: list[integration.AIRequest] = []

    def complete(self, request: integration.AIRequest) -> integration.AIResponse:
        self.requests.append(request)
        index = len(self.requests)
        if index == 1:
            with integration.open_sqlite_connection() as connection:
                now = integration.utc_now()
                connection.execute(
                    """
                    INSERT INTO workspace_egress_policy (
                        workspace_id, ask_me, created_at, updated_at, updated_by
                    ) VALUES (?, 0, ?, ?, 'confirmed-length-test')
                    ON CONFLICT(workspace_id) DO UPDATE SET
                        ask_me = 0, updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    (integration.WORKSPACE_ID, now, now),
                )
                connection.commit()
            text = "confirmed beta "
            finish_reason = "length"
        else:
            text = "final gamma"
            finish_reason = self.final_finish_reason
        return integration.AIResponse(
            provider_id=integration.BINDING.provider_id,
            model_id=integration.BINDING.model_id,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=text,
            content=text,
            usage=integration.AIUsage(
                provider_id=integration.BINDING.provider_id,
                model_id=integration.BINDING.model_id,
                input_tokens=40 + index,
                output_tokens=10 + index,
                usage_source=integration.AIUsageSource.actual,
                provider_cost_estimate=integration.actual_registry_cost_usd(
                    provider_id=integration.BINDING.provider_id,
                    model_id=integration.BINDING.model_id,
                    input_tokens=40 + index, output_tokens=10 + index,
                ),
                currency="USD",
            ),
            finish_reason=finish_reason,
            safety_status="allowed",
            external_dispatch_state=(
                integration.AIExternalDispatchState.started
            ),
        )

    def health(self):  # pragma: no cover
        raise NotImplementedError

    def list_models(self):  # pragma: no cover
        raise NotImplementedError

    def stream(self, request: integration.AIRequest):  # pragma: no cover
        raise NotImplementedError


def test_confirmed_length_reenters_governed_loop_and_completes(
    initialized_database,
) -> None:
    paused = integration._pause_external_continuation()
    flow_id = str(paused.flow_id)
    adapter = _ConfirmedLengthSequenceAdapter()

    outcome = run_confirmation_ticket(
        str(paused.egress_ticket_id),
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "external alpha confirmed beta final gamma"
    assert len(adapter.requests) == 2
    second_prompt = adapter.requests[1].prompt or ""
    assert second_prompt.count("ORIGINAL_REQUEST:") == 1
    assert "Return a bounded external answer." in second_prompt
    assert "external alpha confirmed beta " in second_prompt

    flow = get_flow(flow_id)
    assert flow["state"] == "complete"
    assert flow["attempt_count"] == 3
    assert flow["continuation_count"] == 2
    assert flow["final_output_digest"] == canonical_digest(
        {"text": "external alpha confirmed beta final gamma"}
    )

    with integration.open_sqlite_connection() as connection:
        attempts = connection.execute(
            """
            SELECT id, flow_attempt_index, parent_attempt_id, continuation_index
            FROM ai_jobs WHERE flow_id = ? ORDER BY flow_attempt_index
            """,
            (flow_id,),
        ).fetchall()
        segments = connection.execute(
            """
            SELECT segment_index, originating_attempt_id, body_text
            FROM ai_flow_segments WHERE flow_id = ? ORDER BY segment_index
            """,
            (flow_id,),
        ).fetchall()

    assert [row["flow_attempt_index"] for row in attempts] == [0, 1, 2]
    assert [row["continuation_index"] for row in attempts] == [None, 1, 2]
    assert attempts[1]["parent_attempt_id"] == attempts[0]["id"]
    assert attempts[2]["parent_attempt_id"] == attempts[1]["id"]
    assert [row["segment_index"] for row in segments] == [0, 1, 2]
    assert [row["originating_attempt_id"] for row in segments] == [
        row["id"] for row in attempts
    ]
    assert [row["body_text"] for row in segments] == [
        "external alpha ",
        "confirmed beta ",
        "final gamma",
    ]


def test_confirmed_repeated_length_stops_at_snapshot_guard(
    initialized_database,
) -> None:
    with integration.open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_settings SET max_direct_continuations = 2 WHERE id = 'default'"
        )
        connection.commit()
    paused = integration._pause_external_continuation()
    flow_id = str(paused.flow_id)
    adapter = _ConfirmedLengthSequenceAdapter(final_finish_reason="length")

    outcome = run_confirmation_ticket(
        str(paused.egress_ticket_id),
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "external alpha confirmed beta final gamma"
    assert len(adapter.requests) == 2
    flow = get_flow(flow_id)
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_guard_exhausted"
    assert flow["attempt_count"] == 3
    assert flow["continuation_count"] == 2

    with integration.open_sqlite_connection() as connection:
        attempts = connection.execute(
            """
            SELECT flow_attempt_index, continuation_index
            FROM ai_jobs WHERE flow_id = ? ORDER BY flow_attempt_index
            """,
            (flow_id,),
        ).fetchall()
        segments = connection.execute(
            "SELECT segment_index FROM ai_flow_segments WHERE flow_id = ? ORDER BY segment_index",
            (flow_id,),
        ).fetchall()

    assert [tuple(row) for row in attempts] == [(0, None), (1, 1), (2, 2)]
    assert [row["segment_index"] for row in segments] == [0, 1, 2]
