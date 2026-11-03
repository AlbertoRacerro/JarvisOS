from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.contracts import (
    AIPolicyMode,
    AIRequest,
    AIResponse,
    AIUsage,
)
from app.modules.ai.egress_authority import (
    authorize_manual_context,
    authorize_prompt,
    sanitize_prompt_with_local_model,
)
from app.modules.ai.egress_policy import load_default_egress_policy
from app.modules.ai.egress_sanitizer import (
    auto_approve_canonical_derivative,
    create_prompt_derivative,
    get_prompt_derivative,
)
from app.modules.ai.egress_service import sha256_text
from app.modules.ai.sensitivity import (
    SensitivityPolicyError,
    create_sensitivity_label,
)
from app.modules.ai.sensitivity_models import SensitivityLabelCreate
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
CONFIG_DIGEST = sha256_text("test-sanitizer-config")


class FixedLocalAdapter:
    provider_id = "fake"

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0
        self.requests: list[AIRequest] = []

    def complete(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        self.requests.append(request)
        return AIResponse(
            provider_id="fake",
            model_id="fake-deterministic-v1",
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=self.output,
            content=self.output,
            usage=AIUsage(
                provider_id="fake",
                model_id="fake-deterministic-v1",
                input_tokens=10,
                output_tokens=5,
            ),
            finish_reason="stop",
            safety_status="allowed",
        )

    def health(self):  # pragma: no cover - protocol method unused by this slice
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - protocol method unused by this slice
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover - protocol method unused
        raise NotImplementedError


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


def _decision(text: str) -> str:
    record_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO decisions (
                id, workspace_id, title, decision_text, rationale, status,
                linked_run_id, created_at, updated_at, notes
            ) VALUES (?, ?, 'Decision', ?, NULL, 'accepted', NULL, ?, ?, NULL)
            """,
            (record_id, WORKSPACE_ID, text, now, now),
        )
        connection.commit()
    return record_id


def _approved_canonical_derivative() -> tuple[str, str, dict[str, str]]:
    decision_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{decision_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    approval = auto_approve_canonical_derivative(
        workspace_id=WORKSPACE_ID,
        source_refs=[source_ref],
        derivative_content="Generic tubular photobioreactor concept.",
        final_level="S1",
        transformations=["Removed project-specific geometry"],
        sanitizer_kind="deterministic",
        sanitizer_version="canonical-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
    )
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT source_digests_json FROM sanitized_derivatives WHERE id = ?",
            (approval.derivative_id,),
        ).fetchone()
    import json

    return approval.derivative_id, approval.derivative_digest, json.loads(
        row["source_digests_json"]
    )


def test_fast_dev_marker_free_prompt_is_exact_s1_and_body_free_in_repr():
    raw_prompt = "  Explain a generic pump curve.  "

    authority = authorize_prompt(
        raw_prompt=raw_prompt,
        task_kind="general",
        policy_mode=AIPolicyMode.FAST_DEV,
    )

    assert authority.result == "eligible"
    assert authority.reason_code == "prompt_fast_dev_default_s1"
    assert authority.prompt_level == "S1"
    assert authority.effective_prompt == raw_prompt
    assert authority.raw_prompt_digest == sha256_text(raw_prompt)
    assert raw_prompt not in repr(authority)


def test_marker_free_prompt_pauses_outside_fast_dev():
    authority = authorize_prompt(
        raw_prompt="Explain a generic pump curve.",
        task_kind="general",
        policy_mode=AIPolicyMode.STRICT_IP,
    )

    assert authority.result == "pause"
    assert authority.reason_code == "prompt_classification_required"
    assert authority.effective_prompt is None


def test_secret_prompt_is_denied_before_any_sanitizer_attempt():
    _bootstrap()
    adapter = FixedLocalAdapter("Generic engineering task.")

    authority = authorize_prompt(
        raw_prompt="api_key=super-secret-value",
        task_kind="general",
        policy_mode=AIPolicyMode.FAST_DEV,
        workspace_id=WORKSPACE_ID,
        local_sanitizer_route="local:fake",
        adapters={"fake": adapter},
    )

    assert authority.result == "deny"
    assert authority.reason_code == "prompt_secret_detected"
    assert authority.effective_prompt is None
    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        count = connection.execute("SELECT COUNT(*) AS count FROM ai_jobs").fetchone()[
            "count"
        ]
    assert count == 0


def test_sensitive_prompt_pauses_without_existing_or_requested_sanitizer():
    _bootstrap()

    authority = authorize_prompt(
        raw_prompt="private project geometry question",
        task_kind="general",
        policy_mode=AIPolicyMode.FAST_DEV,
        workspace_id=WORKSPACE_ID,
    )

    assert authority.result == "pause"
    assert authority.reason_code == "prompt_sanitization_required"
    assert authority.prompt_level == "S2"
    assert authority.effective_prompt is None


def test_existing_prompt_derivative_is_reused_without_model_call():
    _bootstrap()
    raw_prompt = "private project geometry question"
    created = create_prompt_derivative(
        raw_prompt=raw_prompt,
        derivative_content="Generic engineering question.",
        final_level="S1",
        transformations=["Removed project identity"],
        sanitizer_kind="deterministic",
        sanitizer_version="prompt-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        workspace_id=WORKSPACE_ID,
    )
    adapter = FixedLocalAdapter("Should not be called.")

    authority = authorize_prompt(
        raw_prompt=raw_prompt,
        task_kind="general",
        policy_mode=AIPolicyMode.FAST_DEV,
        workspace_id=WORKSPACE_ID,
        local_sanitizer_route="local:fake",
        adapters={"fake": adapter},
    )

    assert authority.result == "eligible"
    assert authority.prompt_derivative_id == created.derivative_id
    assert authority.effective_prompt == "Generic engineering question."
    assert adapter.calls == 0


def test_model_sanitizer_uses_one_local_spine_job_and_persists_provenance():
    _bootstrap()
    raw_prompt = "private project geometry question"
    adapter = FixedLocalAdapter("Generic engineering task.")

    authority = authorize_prompt(
        raw_prompt=raw_prompt,
        task_kind="general",
        policy_mode=AIPolicyMode.FAST_DEV,
        workspace_id=WORKSPACE_ID,
        local_sanitizer_route="local:fake",
        adapters={"fake": adapter},
    )

    assert authority.result == "eligible"
    assert authority.effective_prompt == "Generic engineering task."
    assert authority.sanitizer_kind == "model_local"
    assert authority.sanitizer_ai_job_id is not None
    assert adapter.calls == 1
    assert raw_prompt in adapter.requests[0].prompt
    derivative = get_prompt_derivative(
        authority.prompt_derivative_id,
        workspace_id=WORKSPACE_ID,
    )
    assert derivative.sanitizer_ai_job_id == authority.sanitizer_ai_job_id
    with open_sqlite_connection() as connection:
        jobs = connection.execute(
            "SELECT id, status, selected_route_class, provider_id, model_id FROM ai_jobs"
        ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["id"] == authority.sanitizer_ai_job_id
    assert jobs[0]["status"] == "success"
    assert jobs[0]["selected_route_class"] == "local:fake"
    assert jobs[0]["provider_id"] == "fake"
    assert jobs[0]["model_id"] == "fake-deterministic-v1"


def test_model_sanitizer_rejects_external_route_before_spine_call():
    _bootstrap()
    adapter = FixedLocalAdapter("Generic engineering task.")

    with pytest.raises(SensitivityPolicyError, match="explicitly local"):
        sanitize_prompt_with_local_model(
            raw_prompt="private project geometry question",
            task_kind="general",
            workspace_id=WORKSPACE_ID,
            route_class="external:cheap",
            adapters={"deepseek": adapter},
        )

    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        count = connection.execute("SELECT COUNT(*) AS count FROM ai_jobs").fetchone()[
            "count"
        ]
    assert count == 0


def test_surviving_sensitive_output_is_not_persisted_as_derivative():
    _bootstrap()
    adapter = FixedLocalAdapter("This remains a private project response.")

    with pytest.raises(SensitivityPolicyError, match="external-ineligible"):
        sanitize_prompt_with_local_model(
            raw_prompt="private project geometry question",
            task_kind="general",
            workspace_id=WORKSPACE_ID,
            route_class="local:fake",
            adapters={"fake": adapter},
        )

    assert adapter.calls == 1
    with open_sqlite_connection() as connection:
        derivative_count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_prompt_derivatives"
        ).fetchone()["count"]
        job_count = connection.execute("SELECT COUNT(*) AS count FROM ai_jobs").fetchone()[
            "count"
        ]
    assert derivative_count == 0
    assert job_count == 1


def test_arbitrary_manual_context_is_withheld_and_exposes_no_body():
    _bootstrap()
    raw_body = "Caller supplied private context body."

    authority = authorize_manual_context(
        workspace_id=WORKSPACE_ID,
        raw_blocks=[{"source": "manual:1", "content": raw_body}],
        budget_chars=32_000,
    )

    assert authority.result == "pause"
    assert authority.reason_code == "manual_context_not_authorized"
    assert authority.blocks == ()
    assert authority.source_digests == ()
    assert authority.withheld_manifest[0]["reason"] == "manual_block_not_server_derivative"
    assert raw_body not in repr(authority)


def test_exact_current_manual_derivative_is_eligible_and_source_bound():
    _bootstrap()
    derivative_id, derivative_digest, source_digests = _approved_canonical_derivative()
    content = "Generic tubular photobioreactor concept."

    authority = authorize_manual_context(
        workspace_id=WORKSPACE_ID,
        raw_blocks=[
            {
                "source": f"derivative:{derivative_id}",
                "type": "sanitized_derivative",
                "id": derivative_id,
                "content": content,
            }
        ],
        budget_chars=32_000,
    )

    assert authority.result == "eligible"
    assert authority.context_level == "S1"
    assert authority.blocks[0]["content"] == content
    assert authority.included_manifest[0]["content_digest"] == derivative_digest
    assert dict(authority.source_digests) == source_digests
    assert all(digest.startswith("sha256:") for digest in source_digests.values())
    assert content not in repr(authority)


def test_tampered_manual_derivative_pauses_instead_of_using_partial_context():
    _bootstrap()
    derivative_id, _derivative_digest, _source_digests = _approved_canonical_derivative()

    authority = authorize_manual_context(
        workspace_id=WORKSPACE_ID,
        raw_blocks=[
            {
                "source": f"derivative:{derivative_id}",
                "type": "sanitized_derivative",
                "id": derivative_id,
                "content": "Tampered generic body.",
            }
        ],
        budget_chars=32_000,
    )

    assert authority.result == "pause"
    assert authority.blocks == ()
    assert authority.withheld_manifest[0]["reason"] == "derivative_content_digest_mismatch"


def test_invalid_sampling_override_does_not_change_prompt_authority_contract():
    policy = replace(load_default_egress_policy(), sample_rate_bps=10_000)

    authority = authorize_prompt(
        raw_prompt="Explain a generic pump curve.",
        task_kind="general",
        policy_mode=AIPolicyMode.FAST_DEV,
        policy=policy,
    )

    assert authority.result == "eligible"
    assert authority.prompt_level == "S1"
