from __future__ import annotations

import json

import pytest
import test_token_flow_confirmed_length_resume as confirmed_length
import test_token_flow_external_runtime_integration as integration
import test_token_flow_local_runtime_integration as local

from app.core.database import open_sqlite_connection
from app.modules.ai.egress_confirmation import run_confirmation_ticket
from app.modules.ai.execution import run_ai_task
from app.modules.ai.flow_record_capture import capture_final_flow_records
from app.modules.ai.token_flow_service import get_flow, transition_flow_state
from app.modules.ai.token_flow_terminalization import terminalize_assembled_output
from app.modules.memory import service as memory_service

initialized_database = integration.initialized_database

RECORD_OUTPUT = (
    "assembled decision\n"
    "```jarvis-records\n"
    '{"record_version":"jarvis_records_v0","records":['
    '{"record_kind":"decision","title":"Use bounded continuation",'
    '"decision_text":"Keep exact-stop capture transactional"},'
    '{"record_kind":"assumption","statement":"The final assembly is canonical"}'
    "]}\n"
    "```"
)


def _complete_confirmed_capture():
    paused = integration._pause_external_continuation(task_kind="decision_support")
    adapter = integration.ConfirmedExternalAdapter(text=RECORD_OUTPUT, finish_reason="stop")
    outcome = run_confirmation_ticket(
        str(paused.egress_ticket_id),
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome
    assert outcome.response is not None
    return paused, outcome


def test_confirmed_final_assembly_captures_once_and_replays_same_ids(
    initialized_database,
) -> None:
    paused, outcome = _complete_confirmed_capture()

    assert outcome.status == "success"
    assert outcome.proposed_record_ids is not None
    assert len(outcome.proposed_record_ids) == 2
    assert outcome.records_parse_error is None
    flow = get_flow(str(paused.flow_id))
    assert flow["state"] == "complete"

    replay = capture_final_flow_records(
        task_kind="decision_support",
        response_text=outcome.response.text,
        terminal_attempt_id=outcome.ledger_id,
        workspace_id=integration.WORKSPACE_ID,
    )
    assert replay is not None
    assert replay.replayed is True
    assert list(replay.proposal_ids) == outcome.proposed_record_ids

    with open_sqlite_connection() as connection:
        receipt = connection.execute(
            "SELECT proposal_ids_json, parse_error FROM ai_flow_record_captures WHERE flow_id = ?",
            (paused.flow_id,),
        ).fetchone()
        decisions = connection.execute(
            "SELECT id, source_ai_job_id FROM decisions WHERE origin = 'ai_proposed'"
        ).fetchall()
        assumptions = connection.execute(
            "SELECT id, source_ai_job_id FROM assumptions WHERE origin = 'ai_proposed'"
        ).fetchall()
    assert json.loads(receipt["proposal_ids_json"]) == outcome.proposed_record_ids
    assert receipt["parse_error"] is None
    created = [*decisions, *assumptions]
    assert len(created) == 2
    assert {row["id"] for row in created} == set(outcome.proposed_record_ids)
    assert {row["source_ai_job_id"] for row in created} == {outcome.ledger_id}


class _ConfirmedLengthRecordAdapter(
    confirmed_length._ConfirmedLengthSequenceAdapter
):
    def complete(self, request):
        response = super().complete(request)
        if len(self.requests) == 2:
            return response.model_copy(
                update={"text": RECORD_OUTPUT, "content": RECORD_OUTPUT}
            )
        return response


def test_confirmed_repeated_length_captures_final_assembly_once(
    initialized_database,
) -> None:
    paused = integration._pause_external_continuation(task_kind="decision_support")
    adapter = _ConfirmedLengthRecordAdapter(final_finish_reason="stop")

    outcome = run_confirmation_ticket(
        str(paused.egress_ticket_id),
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text.startswith("external alpha confirmed beta ")
    assert len(adapter.requests) == 2
    assert outcome.proposed_record_ids is not None
    assert len(outcome.proposed_record_ids) == 2
    with open_sqlite_connection() as connection:
        receipt_count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_flow_record_captures WHERE flow_id = ?",
            (paused.flow_id,),
        ).fetchone()["count"]
    assert receipt_count == 1


def test_complete_output_without_records_is_receipted_once(
    initialized_database,
) -> None:
    paused = integration._pause_external_continuation(task_kind="decision_support")
    adapter = integration.ConfirmedExternalAdapter(
        text="plain complete answer",
        finish_reason="stop",
    )

    outcome = run_confirmation_ticket(
        str(paused.egress_ticket_id),
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome

    assert outcome.proposed_record_ids == []
    assert outcome.records_parse_error is None
    replay = capture_final_flow_records(
        task_kind="decision_support",
        response_text=outcome.response.text if outcome.response is not None else None,
        terminal_attempt_id=outcome.ledger_id,
        workspace_id=integration.WORKSPACE_ID,
    )
    assert replay is not None
    assert replay.replayed is True
    assert replay.proposal_ids == ()
    with open_sqlite_connection() as connection:
        receipt = connection.execute(
            "SELECT proposal_ids_json, parse_error FROM ai_flow_record_captures WHERE flow_id = ?",
            (paused.flow_id,),
        ).fetchone()
    assert tuple(receipt) == ("[]", None)


def test_capture_failure_rolls_back_terminalization_and_retry_converges(
    initialized_database, monkeypatch
) -> None:
    paused = integration._pause_external_continuation(task_kind="decision_support")
    adapter = integration.ConfirmedExternalAdapter(text=RECORD_OUTPUT, finish_reason="stop")
    original = memory_service._create_proposal_in_transaction
    calls = 0

    def fail_second(connection, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected capture failure")
        return original(connection, payload)

    monkeypatch.setattr(memory_service, "_create_proposal_in_transaction", fail_second)
    with pytest.raises(RuntimeError, match="injected capture failure"):
        run_confirmation_ticket(
            str(paused.egress_ticket_id),
            adapters={integration.BINDING.provider_id: adapter},
        )

    with open_sqlite_connection() as connection:
        latest = connection.execute(
            """
            SELECT id FROM ai_jobs
            WHERE flow_id = ?
            ORDER BY flow_attempt_index DESC
            LIMIT 1
            """,
            (paused.flow_id,),
        ).fetchone()
        segment = connection.execute(
            """
            SELECT sensitivity_level FROM ai_flow_segments
            WHERE flow_id = ?
            ORDER BY segment_index DESC
            LIMIT 1
            """,
            (paused.flow_id,),
        ).fetchone()
        receipt_count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_flow_record_captures WHERE flow_id = ?",
            (paused.flow_id,),
        ).fetchone()["count"]
        proposal_count = connection.execute(
            """
            SELECT (
                SELECT COUNT(*) FROM decisions WHERE origin = 'ai_proposed'
            ) + (
                SELECT COUNT(*) FROM assumptions WHERE origin = 'ai_proposed'
            ) AS count
            """
        ).fetchone()["count"]
    assert receipt_count == 0
    assert proposal_count == 0
    assert get_flow(str(paused.flow_id))["state"] == "running"

    monkeypatch.setattr(memory_service, "_create_proposal_in_transaction", original)
    flow, assembled = terminalize_assembled_output(
        flow_id=str(paused.flow_id),
        terminal_attempt_id=str(latest["id"]),
        new_state="complete",
        terminal_reason="completed",
        workspace_id=integration.WORKSPACE_ID,
        expected_sensitivity_level=str(segment["sensitivity_level"]),
    )
    assert flow["state"] == "complete"

    replay = capture_final_flow_records(
        task_kind="decision_support",
        response_text=assembled.body_text,
        terminal_attempt_id=str(latest["id"]),
        workspace_id=integration.WORKSPACE_ID,
    )
    assert replay is not None
    assert replay.replayed is True
    assert len(replay.proposal_ids) == 2
    with open_sqlite_connection() as connection:
        events = connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE event_type = 'MemoryProposalCreated'"
        ).fetchone()["count"]
    assert events == 2


def test_direct_local_completion_and_capture_share_one_transaction(
    initialized_database, monkeypatch
) -> None:
    adapter = local._SequenceAdapter(local._ResponseSpec(RECORD_OUTPUT, "stop"))
    original = memory_service._create_proposal_in_transaction

    def fail_capture(_connection, _payload):
        raise RuntimeError("injected direct capture failure")

    monkeypatch.setattr(
        memory_service, "_create_proposal_in_transaction", fail_capture
    )
    with pytest.raises(RuntimeError, match="injected direct capture failure"):
        run_ai_task(
            user_prompt="Return one candidate decision.",
            task_kind="decision_support",
            route_class="local:sequence",
            max_output_tokens=64,
            adapters={"sequence": adapter},
            bindings={"local:sequence": local._binding()},
            workspace_id=integration.WORKSPACE_ID,
        )

    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT flow.id AS flow_id, flow.state, job.id AS attempt_id
            FROM ai_flows AS flow
            JOIN ai_jobs AS job ON job.flow_id = flow.id
            ORDER BY flow.created_at DESC, job.flow_attempt_index DESC
            LIMIT 1
            """
        ).fetchone()
        receipt_count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_flow_record_captures"
        ).fetchone()["count"]
    assert row["state"] == "running"
    assert receipt_count == 0

    monkeypatch.setattr(memory_service, "_create_proposal_in_transaction", original)
    completed = transition_flow_state(
        flow_id=str(row["flow_id"]),
        new_state="complete",
        terminal_reason="completed",
        terminal_attempt_id=str(row["attempt_id"]),
        terminal_response_text=RECORD_OUTPUT,
    )
    assert completed["state"] == "complete"
    replay = capture_final_flow_records(
        task_kind="decision_support",
        response_text=RECORD_OUTPUT,
        terminal_attempt_id=str(row["attempt_id"]),
        workspace_id=integration.WORKSPACE_ID,
    )
    assert replay is not None
    assert replay.replayed is True
    assert len(replay.proposal_ids) == 2


def test_direct_external_completion_creates_atomic_receipt(
    initialized_database,
) -> None:
    adapter = integration.ConfirmedExternalAdapter(
        text=RECORD_OUTPUT,
        finish_reason="stop",
    )

    outcome = integration.run_external_task(
        user_prompt="Return one candidate decision.",
        task_kind="decision_support",
        selected_route_class=integration.BINDING.route_class,
        requested_route_class=integration.BINDING.route_class,
        context_blocks=None,
        max_output_tokens=64,
        adapters={integration.BINDING.provider_id: adapter},
        bindings={integration.BINDING.route_class: integration.BINDING},
        workspace_id=integration.WORKSPACE_ID,
        context_build_error=None,
        external_blocked_reason=None,
        task_type_for=integration._task_type,
    )

    assert get_flow(str(outcome.flow_id))["state"] == "complete"
    with open_sqlite_connection() as connection:
        receipt = connection.execute(
            "SELECT proposal_ids_json FROM ai_flow_record_captures WHERE flow_id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert len(json.loads(receipt["proposal_ids_json"])) == 2


def test_partial_confirmed_output_never_creates_capture_receipt(
    initialized_database,
) -> None:
    paused = integration._pause_external_continuation(task_kind="decision_support")
    adapter = integration.ConfirmedExternalAdapter(text=RECORD_OUTPUT, finish_reason=None)

    outcome = run_confirmation_ticket(
        str(paused.egress_ticket_id),
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome

    assert outcome.proposed_record_ids in (None, [])
    assert get_flow(str(paused.flow_id))["state"] == "partial_terminal"
    with open_sqlite_connection() as connection:
        receipt_count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_flow_record_captures WHERE flow_id = ?",
            (paused.flow_id,),
        ).fetchone()["count"]
        proposal_count = connection.execute(
            "SELECT COUNT(*) AS count FROM decisions WHERE origin = 'ai_proposed'"
        ).fetchone()["count"]
    assert receipt_count == 0
    assert proposal_count == 0
