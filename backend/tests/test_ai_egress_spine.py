from datetime import UTC, datetime

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.contracts import AIResponse, AIUsage
from app.modules.ai.egress_service import EgressContractError, sha256_text
from app.modules.ai.egress_spine import (
    EgressSpineStateError,
    create_queued_ai_job,
    finalize_queued_ai_job,
    record_prepacket_egress_decision,
)
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


def _bootstrap() -> None:
    initialize_database()
    with open_sqlite_connection() as connection:
        now = utc_now()
        connection.execute(
            """
            INSERT OR IGNORE INTO workspaces (
                id, name, slug, description, status, created_at, updated_at
            ) VALUES (?, 'Zephyr', 'main', NULL, 'active', ?, ?)
            """,
            (WORKSPACE_ID, now, now),
        )
        connection.commit()


def _response(text: str = "Generic answer.") -> AIResponse:
    return AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id="request-1",
        correlation_id="correlation-1",
        text=text,
        content=text,
        usage=AIUsage(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            input_tokens=12,
            output_tokens=7,
            provider_cost_estimate=0.002,
        ),
        finish_reason="stop",
        safety_status="allowed",
    )


def test_prepacket_pause_persists_only_safe_metadata_and_no_packet_body():
    _bootstrap()
    raw_prompt = "Zephyr proprietary geometry must never reach a packet."
    prompt_digest = sha256_text(raw_prompt)
    context_digest = canonical_digest([])

    result = record_prepacket_egress_decision(
        result="pause",
        reason_code="prompt_sanitization_required",
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
        prompt_digest=prompt_digest,
        context_digest=context_digest,
        prompt_level="S3",
        context_level="S0",
        final_level="S3",
        source_count=0,
        included_count=0,
        withheld_count=0,
        workspace_id=WORKSPACE_ID,
        now=NOW,
    )

    with open_sqlite_connection() as connection:
        decision = connection.execute(
            "SELECT * FROM egress_decisions WHERE id = ?",
            (result.decision_id,),
        ).fetchone()
        packet_count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_packets"
        ).fetchone()["count"]
    assert decision["result"] == "pause"
    assert decision["reason_code"] == "prompt_sanitization_required"
    assert decision["packet_id"] is None
    assert decision["packet_digest"] is None
    assert decision["safe_input_digest"] == result.safe_input_digest
    assert decision["projected_input_tokens"] == 0
    assert decision["projected_output_tokens"] == 0
    assert decision["projected_cost_upper_usd"] == 0
    assert decision["trigger_ids_json"] == "[]"
    assert decision["confirmation_required"] == 0
    assert packet_count == 0
    assert raw_prompt not in str(dict(decision))


def test_prepacket_decisions_are_append_only_and_allow_is_forbidden():
    _bootstrap()
    kwargs = {
        "result": "deny",
        "reason_code": "prompt_secret_detected",
        "route_class": "external:cheap",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "fallback_index": 0,
        "prompt_digest": sha256_text("api_key=redacted"),
        "context_digest": None,
        "prompt_level": "S4",
        "context_level": "S0",
        "final_level": "S4",
        "workspace_id": WORKSPACE_ID,
        "now": NOW,
    }

    first = record_prepacket_egress_decision(**kwargs)
    second = record_prepacket_egress_decision(**kwargs)

    assert first.decision_id != second.decision_id
    assert first.safe_input_digest == second.safe_input_digest
    with open_sqlite_connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_decisions"
        ).fetchone()["count"]
    assert count == 2

    with pytest.raises(EgressContractError, match="deny or pause"):
        record_prepacket_egress_decision(**{**kwargs, "result": "allow"})


def test_prepacket_validation_rejects_reason_drift_and_incoherent_levels():
    _bootstrap()
    base = {
        "result": "pause",
        "reason_code": "prompt_classification_required",
        "route_class": "external:cheap",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "fallback_index": 0,
        "prompt_digest": sha256_text("generic prompt"),
        "context_digest": None,
        "prompt_level": "unknown",
        "context_level": "S0",
        "final_level": "unknown",
        "workspace_id": WORKSPACE_ID,
        "now": NOW,
    }

    with pytest.raises(EgressContractError, match="reason code"):
        record_prepacket_egress_decision(
            **{**base, "reason_code": "human prose is not control flow"}
        )
    with pytest.raises(EgressContractError, match="maximum pre-packet level"):
        record_prepacket_egress_decision(**{**base, "final_level": "S0"})


def test_ai_job_is_created_before_attempt_and_finalized_in_place_once():
    _bootstrap()
    queued = create_queued_ai_job(
        task_kind="general",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="bound:external:cheap",
        prompt_digest=canonical_digest({"prompt": "Generic prompt."}),
        context_digest=canonical_digest([]),
        context_sources=[
            {"source": "derivative:1", "type": "sanitized_derivative", "id": "1"}
        ],
        route_metadata={"fallback_attempt_index": 0},
        now=NOW,
    )

    with open_sqlite_connection() as connection:
        before = connection.execute(
            "SELECT * FROM ai_jobs WHERE id = ?",
            (queued.ai_job_id,),
        ).fetchone()
    assert before["status"] == "queued"
    assert before["output_digest"] is None

    response = _response()
    finalized = finalize_queued_ai_job(
        queued.ai_job_id,
        status="success",
        response=response,
        latency_ms=25,
    )

    with open_sqlite_connection() as connection:
        rows = connection.execute("SELECT * FROM ai_jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == queued.ai_job_id
    assert rows[0]["status"] == "success"
    assert rows[0]["output_digest"] == canonical_digest({"text": response.text})
    assert rows[0]["input_tokens"] == 12
    assert rows[0]["output_tokens"] == 7
    assert rows[0]["cost_estimate"] == pytest.approx(0.002)
    assert finalized.ai_job_id == queued.ai_job_id

    with pytest.raises(EgressSpineStateError, match="already finalized"):
        finalize_queued_ai_job(
            queued.ai_job_id,
            status="provider_error",
            response=None,
            latency_ms=30,
            error_type="timeout",
        )


def test_ai_job_safe_metadata_rejects_body_fields():
    _bootstrap()

    with pytest.raises(EgressContractError, match="forbidden body field prompt"):
        create_queued_ai_job(
            task_kind="general",
            requested_route_class="external:cheap",
            selected_route_class="external:cheap",
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            decision_reason="bound:external:cheap",
            route_metadata={"prompt": "must not enter route metadata"},
            now=NOW,
        )

    with open_sqlite_connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_jobs"
        ).fetchone()["count"]
    assert count == 0


def test_successful_finalization_requires_response_text():
    _bootstrap()
    queued = create_queued_ai_job(
        task_kind="general",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="bound:external:cheap",
        now=NOW,
    )

    with pytest.raises(EgressContractError, match="requires a text response"):
        finalize_queued_ai_job(
            queued.ai_job_id,
            status="success",
            response=None,
            latency_ms=1,
        )

    with open_sqlite_connection() as connection:
        status = connection.execute(
            "SELECT status FROM ai_jobs WHERE id = ?",
            (queued.ai_job_id,),
        ).fetchone()["status"]
    assert status == "queued"
