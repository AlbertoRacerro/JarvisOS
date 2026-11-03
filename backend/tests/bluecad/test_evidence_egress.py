from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.modules.ai.egress_authority as egress_authority_module
import app.modules.bluecad.evidence_egress as evidence_egress_module
import app.modules.bluecad.loop as loop_module
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai import sensitivity
from app.modules.ai.egress_persistence import prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_sanitizer import create_prompt_derivative
from app.modules.ai.egress_service import (
    EgressPacketMaterial,
    build_packet_projection,
    sha256_text,
)
from app.modules.ai.execution import ProviderBinding
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.bluecad.evidence_egress import (
    bind_evidence_lineage,
    enrich_authorized_evidence_manifest,
    validate_authorized_structural_prompt_authority,
)
from app.modules.bluecad.evidence_sight import EvidenceSight
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _bootstrap(monkeypatch: pytest.MonkeyPatch) -> str:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100.0,
            paid_ai_enabled=True,
            provider_mode="deepseek",
        )
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
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
    approval = create_prompt_derivative(
        raw_prompt="CONFIDENTIAL PROJECT GEOMETRY: test-only raw request.",
        derivative_content="Generic structural repair instruction.",
        final_level="S1",
        transformations=("test_fixture_generic_rewrite",),
        sanitizer_kind="deterministic",
        sanitizer_version="test-fixture-v1",
        sanitizer_config_digest="a" * 64,
        workspace_id=WORKSPACE_ID,
        now=NOW,
    )
    return approval.derivative_id


def _lineage(*, structural_attempt_id: str = "attempt-2") -> dict[str, object]:
    return {
        "schema_version": "bluecad_evidence_lineage_v0_1",
        "workspace_id": WORKSPACE_ID,
        "candidate_id": "candidate-1",
        "source_attempt_id": "attempt-1",
        "structural_attempt_id": structural_attempt_id,
        "ordered_source_refs": ["evidence:e1"],
        "source_effective_levels": ["S1"],
        "sight_digest": "sha256:" + "1" * 64,
        "renderer_id": "evidence_sight_v0",
        "renderer_version": "evidence_sight_v0",
        "max_lines": 6,
        "max_chars": 2000,
        "derivative_id": "derivative-1",
        "derivative_digest": "sha256:" + "2" * 64,
        "effective_level": "S1",
        "sanitizer_kind": "deterministic",
        "sanitizer_version": "bluecad_evidence_sight_derivative_v0_1",
        "sanitizer_config_digest": "3" * 64,
        "sanitizer_ai_job_id": None,
        "instruction_derivative_id": "prompt-derivative-1",
        "instruction_derivative_digest": "sha256:" + "4" * 64,
        "sensitivity_policy_version": "ip-egress-v1",
        "egress_policy_version": "ip-egress-v1",
    }


def _manifest() -> tuple[dict[str, object], ...]:
    return (
        {
            "source_ref": "derivative:derivative-1",
            "source_refs": ["evidence:e1"],
            "content_digest": "sha256:" + "2" * 64,
            "effective_level": "S1",
            "label_id": None,
            "derivative_id": "derivative-1",
            "inclusion_reason": "approved_derivative",
        },
    )


def _material(
    *,
    lineage: dict[str, object] | None = None,
    prompt_derivative_id: str = "prompt-derivative-1",
) -> EgressPacketMaterial:
    manifest = dict(_manifest()[0])
    if lineage is not None:
        manifest["evidence_lineage"] = lineage
    return EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="bluecad_cad_repair",
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
        prompt="Generic structural repair instruction.",
        context_blocks=(
            {
                "source": "derivative:derivative-1",
                "type": "sanitized_derivative",
                "id": "derivative-1",
                "content": "Generic structural evidence.",
            },
        ),
        prompt_level="S1",
        context_level="S1",
        final_level="S1",
        max_output_tokens=128,
        workspace_id=WORKSPACE_ID,
        prompt_derivative_id=prompt_derivative_id,
        included_manifest=(manifest,),
        source_digests=(("evidence:e1", sha256_text("source-body")),),
    )


def _matching_sight() -> EvidenceSight:
    return EvidenceSight(
        text="EVIDENCE_SIGHT_V0\nevidence:validation verdict=fail",
        digest="sha256:" + "1" * 64,
        record_ids=("e1",),
    )


def _matching_prompt_derivative() -> SimpleNamespace:
    return SimpleNamespace(
        id="prompt-derivative-1",
        derivative_digest="sha256:" + "4" * 64,
        workspace_id=WORKSPACE_ID,
        sanitizer_kind="model_local",
        status="approved",
    )


def _matching_authority_snapshot(
    *, sight: EvidenceSight | None = None, effective_level: str = "S1"
) -> dict[str, object]:
    return {
        "sight": sight or _matching_sight(),
        "derivative": {
            "source_refs_json": '["evidence:e1"]',
            "source_digests_json": '{"evidence:e1":"sha256:' + "5" * 64 + '"}',
            "effective_level": "S1",
            "content_digest": "sha256:" + "2" * 64,
            "sanitizer_kind": "deterministic",
            "sanitizer_version": "bluecad_evidence_sight_derivative_v0_1",
            "sanitizer_config_digest": "3" * 64,
        },
        "source_refs": tuple(f"evidence:{item}" for item in (sight or _matching_sight()).record_ids),
        "source_digests": {"evidence:e1": "sha256:" + "5" * 64},
        "effective_levels": (effective_level,),
    }


def test_lineage_enrichment_is_scoped_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_lineage_authority_snapshot",
        lambda _lineage: _matching_authority_snapshot(),
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_structural_prompt_derivative",
        lambda _lineage: _matching_prompt_derivative(),
    )

    assert enrich_authorized_evidence_manifest(manifest) == manifest
    with bind_evidence_lineage(_lineage()):
        enriched = enrich_authorized_evidence_manifest(manifest)
    assert enriched[0]["evidence_lineage"]["structural_attempt_id"] == "attempt-2"
    assert "evidence_lineage" not in manifest[0]

    bad = _lineage()
    bad["ordered_source_refs"] = ["evidence:other"]
    with bind_evidence_lineage(bad), pytest.raises(
        sensitivity.SensitivityPolicyError
    ):
        enrich_authorized_evidence_manifest(manifest)

    wrong_manifest = (dict(manifest[0], content_digest="sha256:" + "9" * 64),)
    with bind_evidence_lineage(_lineage()), pytest.raises(
        sensitivity.SensitivityPolicyError
    ):
        enrich_authorized_evidence_manifest(wrong_manifest)


def test_packet_authorization_rejects_prompt_derivative_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_lineage_authority_snapshot",
        lambda _lineage: _matching_authority_snapshot(),
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_structural_prompt_derivative",
        lambda _lineage: SimpleNamespace(
            id="prompt-derivative-other",
            derivative_digest="sha256:" + "9" * 64,
            workspace_id=WORKSPACE_ID,
            sanitizer_kind="model_local",
            status="approved",
        ),
    )

    with bind_evidence_lineage(_lineage()), pytest.raises(
        sensitivity.SensitivityPolicyError
    ):
        enrich_authorized_evidence_manifest(_manifest())


def test_selected_prompt_authority_rejects_substitution_before_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_calls: list[str] = []
    monkeypatch.setattr(
        evidence_egress_module,
        "get_prompt_derivative",
        lambda *_args, **_kwargs: adapter_calls.append("lookup"),
    )
    substituted = SimpleNamespace(
        prompt_derivative_id="prompt-derivative-other",
        prompt_derivative_digest="sha256:" + "9" * 64,
        sanitizer_kind="model_local",
    )
    with bind_evidence_lineage(_lineage()), pytest.raises(
        sensitivity.SensitivityPolicyError
    ):
        validate_authorized_structural_prompt_authority(substituted)
    assert adapter_calls == []


def test_packet_authorization_rejects_sight_insertion_order_or_digest_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_structural_prompt_derivative",
        lambda _lineage: _matching_prompt_derivative(),
    )
    current_sights = (
        EvidenceSight("changed", "sha256:" + "8" * 64, ("e1",)),
        EvidenceSight("inserted", "sha256:" + "1" * 64, ("e1", "e2")),
        EvidenceSight("reordered", "sha256:" + "1" * 64, ("e2", "e1")),
    )
    for sight in current_sights:
        monkeypatch.setattr(
            evidence_egress_module,
            "_current_lineage_authority_snapshot",
            lambda _lineage, current=sight: _matching_authority_snapshot(sight=current),
        )
        with bind_evidence_lineage(_lineage()), pytest.raises(
            sensitivity.SensitivityPolicyError
        ):
            enrich_authorized_evidence_manifest(manifest)


def test_packet_authorization_rejects_concurrent_effective_level_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_structural_prompt_derivative",
        lambda _lineage: _matching_prompt_derivative(),
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_lineage_authority_snapshot",
        lambda _lineage: _matching_authority_snapshot(effective_level="S2"),
    )
    with bind_evidence_lineage(_lineage()), pytest.raises(
        sensitivity.SensitivityPolicyError
    ):
        enrich_authorized_evidence_manifest(_manifest())


def test_packet_authorization_accepts_bound_sensitive_source_after_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lineage = _lineage()
    lineage.update(
        source_effective_levels=["S2"],
        sanitizer_kind="model_local",
        sanitizer_version="canonical-local-sanitizer-v1",
        sanitizer_config_digest="6" * 64,
    )
    snapshot = _matching_authority_snapshot(effective_level="S2")
    snapshot["derivative"].update(
        sanitizer_kind="model_local",
        sanitizer_version="canonical-local-sanitizer-v1",
        sanitizer_config_digest="6" * 64,
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_structural_prompt_derivative",
        lambda _lineage: _matching_prompt_derivative(),
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "_current_lineage_authority_snapshot",
        lambda _lineage: snapshot,
    )

    with bind_evidence_lineage(lineage):
        enriched = enrich_authorized_evidence_manifest(_manifest())
    assert enriched[0]["evidence_lineage"]["source_effective_levels"] == ["S2"]


def test_lineage_mutation_changes_packet_digest() -> None:
    first = build_packet_projection(_material(lineage=_lineage()))
    second = build_packet_projection(
        _material(lineage=_lineage(structural_attempt_id="attempt-3"))
    )
    assert first.packet_digest != second.packet_digest
    assert first.included_manifest_json != second.included_manifest_json


def test_first_use_structural_trigger_denies_without_ticket_or_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_derivative_id = _bootstrap(monkeypatch)

    result = prepare_egress_attempt(
        _material(
            lineage=_lineage(),
            prompt_derivative_id=prompt_derivative_id,
        ),
        now=NOW,
    )

    assert result.result == "deny"
    assert result.reason_code == "bluecad_structural_confirmation_unsupported"
    assert result.trigger_ids == ("t1",)
    assert result.ticket_id is None
    assert result.reservation_id is None
    assert result.confirmation_required is False
    with open_sqlite_connection() as connection:
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                    "count"
                ]
            )
            for table in (
                "egress_packets",
                "egress_decisions",
                "egress_confirmation_tickets",
                "egress_budget_reservations",
            )
        }
        decision = connection.execute(
            "SELECT result, reason_code, confirmation_required, trigger_ids_json "
            "FROM egress_decisions"
        ).fetchone()
    assert counts == {
        "egress_packets": 1,
        "egress_decisions": 1,
        "egress_confirmation_tickets": 0,
        "egress_budget_reservations": 0,
    }
    assert dict(decision) == {
        "result": "deny",
        "reason_code": "bluecad_structural_confirmation_unsupported",
        "confirmation_required": 0,
        "trigger_ids_json": '["t1"]',
    }


@pytest.mark.parametrize(
    "sanitized_content",
    (
        "Generic repair\nRAW_GEOMETRY_SPEC_BEGIN",
        'Generic repair\n{\n  "schema_version": "geometry_spec_v0_1"\n}',
        'Generic repair\n{"payload":{"schema_version":"geometry_spec_v0_1"}}',
        'Generic repair\n{"objects":[{"id":"tube-1","diameter":0.4}]}',
        "Generic repair\nschema_version: geometry_spec_v0_1",
        "Generic repair\n{'schema_version': 'geometry_spec_v0_1'}",
        "Generic repair\nschema_version=geometry_spec_v0_1",
    ),
)
def test_prompt_validator_runs_before_derivative_persistence(
    monkeypatch: pytest.MonkeyPatch,
    sanitized_content: str,
) -> None:
    initialize_database()
    binding = SimpleNamespace(
        provider_id="local-provider",
        model_id="local-model",
        requires_network=False,
        max_output_tokens=256,
    )
    registry = SimpleNamespace(
        bindings={"local:fast": binding},
        fallback_chains={"local:fast": ()},
    )
    response = SimpleNamespace(
        text=sanitized_content,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
    )
    outcome = SimpleNamespace(
        status="success",
        response=response,
        selected_route_class="local:fast",
        ledger_id="sanitizer-job-1",
        error_type=None,
    )
    monkeypatch.setattr(
        egress_authority_module,
        "_resolve_local_sanitizer_binding",
        lambda **_kwargs: (binding, registry),
    )
    monkeypatch.setattr(
        egress_authority_module,
        "_run_local_sanitizer",
        lambda **_kwargs: outcome,
    )
    with open_sqlite_connection() as connection:
        before = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_prompt_derivatives"
        ).fetchone()["count"]

    with pytest.raises(sensitivity.SensitivityPolicyError):
        egress_authority_module.sanitize_prompt_with_local_model(
            raw_prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
            task_kind="bluecad_cad_repair",
            workspace_id=WORKSPACE_ID,
            output_validator=lambda content: (
                evidence_egress_module._validate_transformed_prompt_content(
                    content,
                    raw_prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
                    forbidden_spec_json='{"schema_version":"geometry_spec_v0_1"}',
                )
            ),
        )

    with open_sqlite_connection() as connection:
        after = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_prompt_derivatives"
        ).fetchone()["count"]
    assert after == before


@pytest.mark.parametrize(
    ("sanitizer_version", "sanitizer_config_digest"),
    (
        ("prompt-local-sanitizer-v1", "e" * 64),
        ("bluecad_structural_abstraction_v0_1", "f" * 64),
    ),
)
def test_prompt_reuse_requires_exact_bluecad_sanitizer_identity(
    monkeypatch: pytest.MonkeyPatch,
    sanitizer_version: str,
    sanitizer_config_digest: str,
) -> None:
    existing = SimpleNamespace(
        id="prompt-generic",
        status="approved",
        workspace_id=WORKSPACE_ID,
        sanitizer_kind="model_local",
        sanitizer_version=sanitizer_version,
        sanitizer_config_digest=sanitizer_config_digest,
        derivative_content="Generic bounded repair request.",
        derivative_digest="sha256:" + "7" * 64,
    )
    replacement = SimpleNamespace(id="prompt-bluecad")
    calls: list[str] = []
    monkeypatch.setattr(
        evidence_egress_module,
        "resolve_approved_prompt_derivative",
        lambda **_kwargs: existing,
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "_expected_structural_prompt_config_digest",
        lambda: "e" * 64,
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "sanitize_prompt_with_local_model",
        lambda **_kwargs: calls.append("sanitize") or replacement,
    )

    result = evidence_egress_module._resolve_external_prompt_derivative(
        workspace_id=WORKSPACE_ID,
        raw_prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
        forbidden_spec_json='{"schema_version":"geometry_spec_v0_1"}',
        adapters=None,
    )
    assert result is replacement
    assert calls == ["sanitize"]


def test_prompt_reuse_accepts_exact_bluecad_sanitizer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(
        id="prompt-bluecad",
        status="approved",
        workspace_id=WORKSPACE_ID,
        sanitizer_kind="model_local",
        sanitizer_version="bluecad_structural_abstraction_v0_1",
        sanitizer_config_digest="e" * 64,
        derivative_content="Generic bounded repair request.",
        derivative_digest="sha256:" + "7" * 64,
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "resolve_approved_prompt_derivative",
        lambda **_kwargs: existing,
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "_expected_structural_prompt_config_digest",
        lambda: "e" * 64,
    )
    monkeypatch.setattr(
        evidence_egress_module,
        "sanitize_prompt_with_local_model",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must reuse")),
    )

    result = evidence_egress_module._resolve_external_prompt_derivative(
        workspace_id=WORKSPACE_ID,
        raw_prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
        forbidden_spec_json='{"schema_version":"geometry_spec_v0_1"}',
        adapters=None,
    )
    assert result is existing


def test_bluecad_prompt_sanitizer_binds_template_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_database()
    binding = SimpleNamespace(
        provider_id="local-provider",
        model_id="local-model",
        requires_network=False,
        max_output_tokens=256,
    )
    registry = SimpleNamespace(
        bindings={"local:fast": binding}, fallback_chains={"local:fast": ()}
    )
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        egress_authority_module,
        "_resolve_local_sanitizer_binding",
        lambda **_kwargs: (binding, registry),
    )

    def run_sanitizer(**kwargs):
        captured["input"] = kwargs["sanitizer_input"]
        return SimpleNamespace(
            status="success",
            response=SimpleNamespace(
                text="Generic bounded structural repair request.",
                provider_id=binding.provider_id,
                model_id=binding.model_id,
            ),
            selected_route_class="local:fast",
            ledger_id="sanitizer-job-1",
            error_type=None,
        )

    monkeypatch.setattr(egress_authority_module, "_run_local_sanitizer", run_sanitizer)
    monkeypatch.setattr(
        egress_authority_module,
        "create_prompt_derivative",
        lambda **kwargs: (
            captured.update(
                version=kwargs["sanitizer_version"],
                config_digest=kwargs["sanitizer_config_digest"],
            )
            or SimpleNamespace(derivative_id="prompt-derivative-1")
        ),
    )
    monkeypatch.setattr(
        egress_authority_module,
        "get_prompt_derivative",
        lambda *_args, **_kwargs: SimpleNamespace(id="prompt-derivative-1"),
    )
    evidence_egress_module._resolve_external_prompt_derivative(
        workspace_id=WORKSPACE_ID,
        raw_prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
        forbidden_spec_json='{"schema_version":"geometry_spec_v0_1"}',
        adapters=None,
    )
    assert captured["version"] == "bluecad_structural_abstraction_v0_1"
    assert captured["input"].startswith(
        "Transform the BLUECAD structural repair task into a bounded generic abstraction."
    )
    generic_digest = egress_authority_module._sanitizer_config_digest(
        policy=evidence_egress_module.load_default_egress_policy(),
        route_class="local:fast",
        template=egress_authority_module._LOCAL_SANITIZER_TEMPLATE,
        version=egress_authority_module._LOCAL_SANITIZER_VERSION,
        registry=registry,
    )
    assert captured["config_digest"] != generic_digest


def test_model_sanitizer_config_identity_binds_renderer_context() -> None:
    policy = SimpleNamespace(config_digest="policy-digest")
    binding = SimpleNamespace(
        provider_id="local-provider",
        model_id="local-model",
        requires_network=False,
        max_output_tokens=512,
    )
    registry = SimpleNamespace(
        bindings={"local:fast": binding},
        fallback_chains={"local:fast": ()},
    )
    base = {
        "workspace_id": WORKSPACE_ID,
        "candidate_id": "candidate-1",
        "source_attempt_id": "attempt-1",
        "ordered_source_refs": ["evidence:e1"],
        "effective_levels": ["S2"],
        "sight_digest": "sha256:" + "1" * 64,
        "renderer_id": "evidence_sight_v0",
        "renderer_version": "evidence_sight_v0",
        "max_lines": 6,
        "max_chars": 2000,
    }

    def digest(context: dict[str, object]) -> str:
        return egress_authority_module._sanitizer_config_digest(
            policy=policy,
            route_class="local:fast",
            template="canonical-template",
            version="canonical-v1",
            registry=registry,
            config_context=context,
        )

    variants = (
        base,
        dict(base, candidate_id="candidate-2"),
        dict(base, source_attempt_id="attempt-2"),
        dict(base, sight_digest="sha256:" + "2" * 64),
        dict(base, ordered_source_refs=["evidence:e2", "evidence:e1"]),
        dict(base, effective_levels=["S3"]),
    )
    assert len({digest(context) for context in variants}) == len(variants)


def test_preparation_failure_happens_before_structural_attempt_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sight = EvidenceSight(
        text="EVIDENCE_SIGHT_V0\nevidence:validation verdict=fail",
        digest="sha256:" + "4" * 64,
        record_ids=("e1",),
    )
    events: list[str] = []
    binding = ProviderBinding(
        "external:cheap",
        "deepseek",
        "deepseek-v4-pro",
        True,
        4000,
        execution_class="external",
        context_window_tokens=8192,
    )
    monkeypatch.setattr(loop_module, "render_evidence_sight", lambda *_args: sight)
    monkeypatch.setattr(
        loop_module,
        "resolve_binding",
        lambda *_args, **_kwargs: (binding, SimpleNamespace()),
    )

    def fail_preparation(**_kwargs):
        events.append("prepare")
        raise RuntimeError("stale evidence")

    def start_attempt(*_args, **_kwargs):
        events.append("start")
        raise AssertionError("attempt must not be inserted")

    monkeypatch.setattr(loop_module, "prepare_external_structural_repair", fail_preparation)
    monkeypatch.setattr(loop_module, "start_structural_attempt", start_attempt)

    loop_module._run_structural_repair_cycle(
        workspace_id=WORKSPACE_ID,
        candidate_id="candidate-1",
        initial_attempt_id="attempt-1",
        initial_attempt_no=1,
        initial_spec={"schema_version": "geometry_spec_v0_1"},
        route_class="external:cheap",
        loop_config=SimpleNamespace(max_structural_repairs=1, max_output_tokens=128),
        adapters=None,
        bindings={"external:cheap": binding},
    )

    assert events == ["prepare"]


def test_unresolved_binding_stops_before_preparation_attempt_or_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sight = EvidenceSight(
        text="EVIDENCE_SIGHT_V0\nevidence:validation verdict=fail",
        digest="sha256:" + "5" * 64,
        record_ids=("e1",),
    )
    events: list[str] = []
    monkeypatch.setattr(loop_module, "render_evidence_sight", lambda *_args: sight)
    monkeypatch.setattr(
        loop_module,
        "resolve_binding",
        lambda *_args, **_kwargs: (None, SimpleNamespace()),
    )
    monkeypatch.setattr(
        loop_module,
        "prepare_external_structural_repair",
        lambda **_kwargs: events.append("prepare"),
    )
    monkeypatch.setattr(
        loop_module,
        "start_structural_attempt",
        lambda *_args, **_kwargs: events.append("start"),
    )
    monkeypatch.setattr(
        loop_module,
        "run_ai_task",
        lambda **_kwargs: events.append("ai"),
    )

    loop_module._run_structural_repair_cycle(
        workspace_id=WORKSPACE_ID,
        candidate_id="candidate-1",
        initial_attempt_id="attempt-1",
        initial_attempt_no=1,
        initial_spec={"schema_version": "geometry_spec_v0_1"},
        route_class="external:cheap",
        loop_config=SimpleNamespace(max_structural_repairs=1, max_output_tokens=128),
        adapters=None,
        bindings=None,
    )

    assert events == []



def test_renderer_context_binds_ordered_effective_levels() -> None:
    sight = EvidenceSight(
        text="EVIDENCE_SIGHT_V0\nevidence:e1",
        digest="sha256:" + "1" * 64,
        record_ids=("e1",),
    )
    s2 = evidence_egress_module._renderer_config_context(
        workspace_id=WORKSPACE_ID,
        candidate_id="candidate-1",
        source_attempt_id="attempt-1",
        sight=sight,
        ordered_source_refs=("evidence:e1",),
        effective_levels=("S2",),
    )
    s3 = evidence_egress_module._renderer_config_context(
        workspace_id=WORKSPACE_ID,
        candidate_id="candidate-1",
        source_attempt_id="attempt-1",
        sight=sight,
        ordered_source_refs=("evidence:e1",),
        effective_levels=("S3",),
    )

    assert s2["effective_levels"] == ["S2"]
    assert s3["effective_levels"] == ["S3"]
    assert s2 != s3
