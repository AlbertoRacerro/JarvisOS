from __future__ import annotations

import pytest

from app.modules.ai.token_flow_evidence import AttemptEvidence, record_attempt_evidence
from app.modules.ai.token_flow_service import TokenFlowConflictError, TokenFlowError, create_flow

DIGEST = "sha256:" + "e" * 64


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _insert_job(
    job_id: str,
    *,
    selected_route_class: str | None = "local:fake",
    provider_id: str | None = "fake",
    model_id: str | None = "fake-modeling-draft-v1",
    fallback_index: int | None = 0,
    input_tokens: int | None = 10,
    output_tokens: int | None = 4,
    output_digest: str | None = DIGEST,
) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, selected_route_class,
                provider_id, model_id, route_reason_json, fallback_index,
                input_tokens, output_tokens, output_digest
            ) VALUES (?, ?, 'success', 'synthesis', ?, ?, ?, '{}', ?, ?, ?, ?)
            """,
            (
                job_id,
                utc_now(),
                selected_route_class,
                provider_id,
                model_id,
                fallback_index,
                input_tokens,
                output_tokens,
                output_digest,
            ),
        )
        connection.commit()


def _synthetic(**overrides) -> AttemptEvidence:
    values = {
        "execution_class": "synthetic",
        "adapter_invoked": True,
        "external_dispatch_state": "not_applicable",
        "normalized_usage_source": "estimated",
        "accounting_basis": "synthetic_not_economic",
        "accounted_provider_spend_usd_decimal": "0",
        "outcome_reason": "completed",
        "accounting_version": "token-flow-v0",
        "provider_id": "fake",
        "model_id": "fake-modeling-draft-v1",
        "selected_route_class": "local:fake",
        "fallback_index": 0,
        "normalized_finish_reason": "stop",
        "capability_version": "registry-v1",
    }
    values.update(overrides)
    return AttemptEvidence(**values)


def _none(**overrides) -> AttemptEvidence:
    values = {
        "execution_class": "none",
        "adapter_invoked": False,
        "external_dispatch_state": "not_applicable",
        "normalized_usage_source": "none",
        "accounting_basis": "no_execution",
        "accounted_provider_spend_usd_decimal": "0",
        "outcome_reason": "policy_denied",
        "accounting_version": "token-flow-v0",
    }
    values.update(overrides)
    return AttemptEvidence(**values)


def _flow_id(job_id: str) -> str | None:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        return connection.execute(
            "SELECT flow_id FROM ai_jobs WHERE id = ?", (job_id,)
        ).fetchone()["flow_id"]


def test_parent_and_continuation_require_existing_same_flow_attempt(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    other = create_flow(task_kind="synthesis")
    _insert_job("parent")
    _insert_job("other-parent")
    _insert_job("child")

    record_attempt_evidence(
        flow_id=str(flow["id"]), attempt_id="parent", evidence=_synthetic()
    )
    record_attempt_evidence(
        flow_id=str(other["id"]), attempt_id="other-parent", evidence=_synthetic()
    )

    with pytest.raises(TokenFlowConflictError, match="already belong"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="child",
            evidence=_synthetic(parent_attempt_id="other-parent", continuation_index=1),
        )

    result = record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="child",
        evidence=_synthetic(parent_attempt_id="parent", continuation_index=1),
    )
    assert result["ordered_attempt_ids"] == ["parent", "child"]


def test_continuation_index_cannot_exceed_creation_snapshot(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job("parent")
    _insert_job("child")
    record_attempt_evidence(
        flow_id=str(flow["id"]), attempt_id="parent", evidence=_synthetic()
    )

    with pytest.raises(TokenFlowConflictError, match="exceeds flow snapshot"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="child",
            evidence=_synthetic(parent_attempt_id="parent", continuation_index=9),
        )
    assert _flow_id("child") is None


def test_external_unknown_requires_conservative_positive_spend(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "external",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        input_tokens=100,
        output_tokens=20,
        output_digest=None,
    )
    common = {
        "execution_class": "external_provider",
        "adapter_invoked": True,
        "external_dispatch_state": "unknown",
        "normalized_usage_source": "estimated",
        "accounting_basis": "conservative_estimated_usage",
        "outcome_reason": "adapter_error",
        "accounting_version": "token-flow-v0",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "selected_route_class": "external:cheap",
        "fallback_index": 0,
        "capability_version": "registry-v1",
        "pricing_version": "pricing-v1",
    }

    with pytest.raises(TokenFlowError, match="positive provider spend"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="external",
            evidence=AttemptEvidence(
                **common, accounted_provider_spend_usd_decimal="0"
            ),
        )
    record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="external",
        evidence=AttemptEvidence(
            **common, accounted_provider_spend_usd_decimal="0.001200"
        ),
    )
    assert _flow_id("external") == flow["id"]


def test_none_pre_adapter_evidence_has_no_binding_or_usage(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "denied",
        selected_route_class=None,
        provider_id=None,
        model_id=None,
        fallback_index=None,
        input_tokens=None,
        output_tokens=None,
        output_digest=None,
    )

    record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="denied",
        evidence=_none(),
    )
    assert _flow_id("denied") == flow["id"]


def test_none_evidence_can_preserve_route_without_concrete_binding(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "unbound",
        selected_route_class="local:missing",
        provider_id=None,
        model_id=None,
        fallback_index=None,
        input_tokens=None,
        output_tokens=None,
        output_digest=None,
    )

    record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="unbound",
        evidence=_none(selected_route_class="local:missing"),
    )
    assert _flow_id("unbound") == flow["id"]


def test_none_evidence_can_preserve_selected_but_unexecutable_binding(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "adapter-missing",
        selected_route_class="local:general",
        provider_id="local_ollama",
        model_id="gemma4-12b",
        fallback_index=0,
        input_tokens=None,
        output_tokens=None,
        output_digest=None,
    )

    record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="adapter-missing",
        evidence=_none(
            provider_id="local_ollama",
            model_id="gemma4-12b",
            selected_route_class="local:general",
            fallback_index=0,
            outcome_reason="adapter_unavailable",
        ),
    )
    assert _flow_id("adapter-missing") == flow["id"]


@pytest.mark.parametrize(
    "evidence",
    [
        _none(provider_id="local_ollama", selected_route_class="local:general"),
        _none(model_id="gemma4-12b", selected_route_class="local:general"),
        _none(provider_id="local_ollama", model_id="gemma4-12b"),
        _none(selected_route_class="local:general", capability_version="registry-v1"),
        _none(selected_route_class="external:cheap", pricing_version="pricing-v1"),
    ],
)
def test_none_evidence_rejects_half_binding_or_executable_versions(
    initialized_database,
    evidence: AttemptEvidence,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "invalid-none",
        selected_route_class=evidence.selected_route_class,
        provider_id=evidence.provider_id,
        model_id=evidence.model_id,
        fallback_index=evidence.fallback_index,
        input_tokens=None,
        output_tokens=None,
        output_digest=None,
    )

    with pytest.raises(TokenFlowError):
        record_attempt_evidence(
            flow_id=str(flow["id"]), attempt_id="invalid-none", evidence=evidence
        )
    assert _flow_id("invalid-none") is None


def test_output_and_subset_evidence_is_fail_closed(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job("attempt-a", input_tokens=5, output_tokens=2)

    with pytest.raises(TokenFlowError, match="cache_read_tokens"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="attempt-a",
            evidence=_synthetic(cache_read_tokens=6),
        )
    with pytest.raises(TokenFlowError, match="output-bearing"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="attempt-a",
            evidence=_synthetic(normalized_finish_reason=None),
        )
    assert _flow_id("attempt-a") is None
