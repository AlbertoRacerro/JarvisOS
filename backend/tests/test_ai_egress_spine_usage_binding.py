from datetime import UTC, datetime

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.contracts import AIResponse, AIUsage
from app.modules.ai.egress_spine import (
    EgressSpineStateError,
    create_queued_ai_job,
    finalize_queued_ai_job,
)

NOW = datetime(2026, 7, 14, 22, 0, tzinfo=UTC)


def test_queued_ai_job_rejects_usage_attributed_to_different_binding():
    initialize_database()
    queued = create_queued_ai_job(
        task_kind="general",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="bound:external:cheap",
        now=NOW,
    )
    mismatched_usage = AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id="request-1",
        correlation_id="correlation-1",
        text="Generic answer.",
        content="Generic answer.",
        usage=AIUsage(
            provider_id="glm",
            model_id="glm-5.2",
            input_tokens=1,
            output_tokens=1,
        ),
        finish_reason="stop",
        safety_status="allowed",
    )

    with pytest.raises(EgressSpineStateError, match="usage binding"):
        finalize_queued_ai_job(
            queued.ai_job_id,
            status="success",
            response=mismatched_usage,
            latency_ms=1,
        )

    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT status, output_digest, input_tokens, output_tokens FROM ai_jobs WHERE id = ?",
            (queued.ai_job_id,),
        ).fetchone()
    assert row["status"] == "queued"
    assert row["output_digest"] is None
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
