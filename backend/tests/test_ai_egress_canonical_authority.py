from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.contracts import AIRequest, AIResponse, AIUsage
from app.modules.ai.egress_authority import sanitize_canonical_sources_with_local_model
from app.modules.ai.egress_sanitizer import auto_approve_canonical_derivative
from app.modules.ai.egress_service import sha256_text
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import (
    FallbackEntry,
    ModelConfig,
    ProviderConfig,
    ProviderRegistry,
)
from app.modules.ai.sensitivity import (
    SensitivityPolicyError,
    create_sensitivity_label,
    get_sanitized_derivative,
    resolve_source_snapshot,
)
from app.modules.ai.sensitivity_models import SensitivityLabelCreate
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
CONFIG_DIGEST = sha256_text("canonical-sanitizer-test-config")


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
                input_tokens=12,
                output_tokens=6,
            ),
            finish_reason="stop",
            safety_status="allowed",
        )

    def health(self):  # pragma: no cover - protocol method unused here
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - protocol method unused here
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover - protocol method unused
        raise NotImplementedError


class MutatingLocalAdapter(FixedLocalAdapter):
    def __init__(self, output: str, *, decision_id: str) -> None:
        super().__init__(output)
        self.decision_id = decision_id

    def complete(self, request: AIRequest) -> AIResponse:
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE decisions SET decision_text = ?, updated_at = ? WHERE id = ?",
                (
                    "Zephyr proprietary geometry changed during sanitization",
                    utc_now(),
                    self.decision_id,
                ),
            )
            connection.commit()
        return super().complete(request)


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


def _decision(text: str, *, level: str = "S3") -> tuple[str, str]:
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
    source_ref = f"decision:{record_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level=level,
        )
    )
    return record_id, source_ref


def _registry_with_network_fallback() -> ProviderRegistry:
    local_provider = ProviderConfig(
        provider_id="fake",
        kind="fake",
        enabled=True,
        requires_network=False,
        base_url=None,
        api_key_ref=None,
        timeout_seconds=1.0,
        monthly_token_cap=0,
        monthly_cost_cap_usd=0.0,
    )
    external_provider = ProviderConfig(
        provider_id="deepseek",
        kind="openai_compatible",
        enabled=True,
        requires_network=True,
        base_url="https://example.invalid",
        api_key_ref="DEEPSEEK_API_KEY",
        timeout_seconds=1.0,
        monthly_token_cap=1000,
        monthly_cost_cap_usd=1.0,
    )
    local_model = ModelConfig(
        provider_id="fake",
        model_id="fake-deterministic-v1",
        provider_model_name="fake-deterministic-v1",
        route_classes=("local:fake",),
        max_output_tokens=512,
        pricing=None,
    )
    external_model = ModelConfig(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        provider_model_name="deepseek-v4-pro",
        route_classes=("local:fake",),
        max_output_tokens=512,
        pricing=None,
    )
    binding = ProviderBinding(
        route_class="local:fake",
        provider_id="fake",
        model_id="fake-deterministic-v1",
        requires_network=False,
        max_output_tokens=512,
    )
    return ProviderRegistry(
        providers={"fake": local_provider, "deepseek": external_provider},
        models={
            ("fake", "fake-deterministic-v1"): local_model,
            ("deepseek", "deepseek-v4-pro"): external_model,
        },
        bindings={"local:fake": binding},
        fallback_chains={
            "local:fake": (
                FallbackEntry("fake", "fake-deterministic-v1"),
                FallbackEntry("deepseek", "deepseek-v4-pro"),
            )
        },
    )


def _insert_valid_sanitizer_job(*, output: str) -> str:
    job_id = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, selected_route_class,
                provider_id, model_id, route_reason_json, output_digest
            ) VALUES (?, ?, 'success', 'sanitizer', 'local:fake',
                      'fake', 'fake-deterministic-v1', '{}', ?)
            """,
            (job_id, utc_now(), canonical_digest({"text": output})),
        )
        connection.commit()
    return job_id


def test_canonical_model_sanitizer_persists_exact_job_and_source_provenance():
    _bootstrap()
    _decision_id, source_ref = _decision("Zephyr proprietary geometry decision")
    before = resolve_source_snapshot(WORKSPACE_ID, source_ref)
    output = "Generic tubular photobioreactor concept."
    adapter = FixedLocalAdapter(output)

    approval = sanitize_canonical_sources_with_local_model(
        workspace_id=WORKSPACE_ID,
        source_refs=[source_ref],
        route_class="local:fake",
        adapters={"fake": adapter},
    )

    derivative = get_sanitized_derivative(WORKSPACE_ID, approval.derivative_id)
    assert adapter.calls == 1
    assert derivative.content == output
    assert derivative.source_digests == {source_ref: before.content_digest}
    with open_sqlite_connection() as connection:
        provenance = connection.execute(
            """
            SELECT sanitizer_kind, sanitizer_ai_job_id
            FROM sanitized_derivatives WHERE id = ?
            """,
            (approval.derivative_id,),
        ).fetchone()
        job = connection.execute(
            """
            SELECT status, task_kind, selected_route_class, provider_id, model_id,
                   output_digest, context_digest, context_sources_json
            FROM ai_jobs WHERE id = ?
            """,
            (approval.sanitizer_ai_job_id,),
        ).fetchone()
    assert provenance["sanitizer_kind"] == "model_local"
    assert provenance["sanitizer_ai_job_id"] == approval.sanitizer_ai_job_id
    assert job["status"] == "success"
    assert job["task_kind"] == "sanitizer"
    assert job["selected_route_class"] == "local:fake"
    assert job["provider_id"] == "fake"
    assert job["model_id"] == "fake-deterministic-v1"
    assert job["output_digest"] == canonical_digest({"text": output})
    assert job["context_digest"] is None
    assert job["context_sources_json"] is None


def test_canonical_source_mutation_during_model_call_blocks_derivative():
    _bootstrap()
    decision_id, source_ref = _decision("Zephyr proprietary geometry decision")
    adapter = MutatingLocalAdapter(
        "Generic tubular concept.",
        decision_id=decision_id,
    )

    with pytest.raises(SensitivityPolicyError, match="source snapshot changed"):
        sanitize_canonical_sources_with_local_model(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            route_class="local:fake",
            adapters={"fake": adapter},
        )

    assert adapter.calls == 1
    with open_sqlite_connection() as connection:
        derivative_count = connection.execute(
            "SELECT COUNT(*) AS count FROM sanitized_derivatives"
        ).fetchone()["count"]
        job_count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_jobs"
        ).fetchone()["count"]
    assert derivative_count == 0
    assert job_count == 1


def test_secret_canonical_source_is_rejected_before_local_model_call():
    _bootstrap()
    _decision_id, source_ref = _decision(
        "api_key=super-secret-value",
        level="S4",
    )
    adapter = FixedLocalAdapter("Generic output.")

    with pytest.raises(SensitivityPolicyError, match="Secret-bearing canonical source"):
        sanitize_canonical_sources_with_local_model(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            route_class="local:fake",
            adapters={"fake": adapter},
        )

    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        job_count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_jobs"
        ).fetchone()["count"]
    assert job_count == 0


def test_network_capable_sanitizer_fallback_closure_is_rejected_pre_call():
    _bootstrap()
    _decision_id, source_ref = _decision("Zephyr proprietary geometry decision")
    adapter = FixedLocalAdapter("Generic output.")

    with pytest.raises(SensitivityPolicyError, match="network-capable"):
        sanitize_canonical_sources_with_local_model(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            route_class="local:fake",
            adapters={"fake": adapter},
            registry=_registry_with_network_fallback(),
        )

    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        job_count = connection.execute(
            "SELECT COUNT(*) AS count FROM ai_jobs"
        ).fetchone()["count"]
    assert job_count == 0


def test_direct_model_canonical_approval_requires_pre_call_source_snapshot():
    _bootstrap()
    _decision_id, source_ref = _decision("Zephyr proprietary geometry decision")
    output = "Generic tubular concept."
    job_id = _insert_valid_sanitizer_job(output=output)

    with pytest.raises(SensitivityPolicyError, match="pre-call source digests"):
        auto_approve_canonical_derivative(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            derivative_content=output,
            final_level="S1",
            transformations=["Claimed model rewrite"],
            sanitizer_kind="model_local",
            sanitizer_version="canonical-local-sanitizer-v1",
            sanitizer_config_digest=CONFIG_DIGEST,
            sanitizer_ai_job_id=job_id,
        )

    with open_sqlite_connection() as connection:
        derivative_count = connection.execute(
            "SELECT COUNT(*) AS count FROM sanitized_derivatives"
        ).fetchone()["count"]
    assert derivative_count == 0
