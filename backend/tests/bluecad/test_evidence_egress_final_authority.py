from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.modules.ai.egress_runtime as egress_runtime
import app.modules.bluecad.evidence_egress as evidence_egress
from app.modules.ai import sensitivity
from app.modules.bluecad.evidence_sight import EvidenceSight


def _lineage() -> dict[str, object]:
    return {
        "schema_version": "bluecad_evidence_lineage_v0_1",
        "workspace_id": "main",
        "candidate_id": "candidate-1",
        "source_attempt_id": "attempt-1",
        "structural_attempt_id": "attempt-2",
        "ordered_source_refs": ["evidence:e2", "evidence:e1"],
        "source_effective_levels": ["S1", "S1"],
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


def test_renderer_order_can_differ_from_canonical_derivative_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_digests = {
        "evidence:e1": "sha256:" + "5" * 64,
        "evidence:e2": "sha256:" + "6" * 64,
    }
    sight = EvidenceSight(
        "EVIDENCE_SIGHT_V0\nevidence:e2\nevidence:e1",
        "sha256:" + "1" * 64,
        ("e2", "e1"),
    )
    snapshot = {
        "sight": sight,
        "derivative": {
            "source_refs_json": '["evidence:e1","evidence:e2"]',
            "source_digests_json": (
                '{"evidence:e1":"sha256:'
                + "5" * 64
                + '","evidence:e2":"sha256:'
                + "6" * 64
                + '"}'
            ),
            "effective_level": "S1",
            "content_digest": "sha256:" + "2" * 64,
            "sanitizer_kind": "deterministic",
            "sanitizer_version": "bluecad_evidence_sight_derivative_v0_1",
            "sanitizer_config_digest": "3" * 64,
        },
        "source_refs": ("evidence:e2", "evidence:e1"),
        "source_digests": source_digests,
        "effective_levels": ("S1", "S1"),
    }
    prompt_derivative = SimpleNamespace(
        id="prompt-derivative-1",
        derivative_digest="sha256:" + "4" * 64,
        workspace_id="main",
        sanitizer_kind="model_local",
        status="approved",
    )
    monkeypatch.setattr(
        evidence_egress,
        "_current_lineage_authority_snapshot",
        lambda _lineage: snapshot,
    )
    monkeypatch.setattr(
        evidence_egress,
        "_current_structural_prompt_derivative",
        lambda _lineage: prompt_derivative,
    )
    manifest = (
        {
            "source_ref": "derivative:derivative-1",
            "source_refs": ["evidence:e1", "evidence:e2"],
            "content_digest": "sha256:" + "2" * 64,
            "effective_level": "S1",
            "label_id": None,
            "derivative_id": "derivative-1",
            "inclusion_reason": "approved_derivative",
        },
    )

    with evidence_egress.bind_evidence_lineage(_lineage()):
        enriched = evidence_egress.enrich_authorized_evidence_manifest(manifest)

    assert enriched[0]["evidence_lineage"]["ordered_source_refs"] == [
        "evidence:e2",
        "evidence:e1",
    ]


def test_final_bluecad_authority_rejection_becomes_prepacket_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = SimpleNamespace(
        result="eligible",
        reason_code="prompt_approved_derivative",
        prompt_level="S1",
    )
    context = egress_runtime._ContextView((), "S0", None, (), (), (), ())
    monkeypatch.setattr(
        egress_runtime,
        "get_ai_settings",
        lambda: SimpleNamespace(policy_mode="FAST_DEV"),
    )
    monkeypatch.setattr(
        egress_runtime,
        "authorize_prompt",
        lambda **_kwargs: authority,
    )
    monkeypatch.setattr(
        evidence_egress,
        "validate_authorized_structural_prompt_authority",
        lambda _authority: (_ for _ in ()).throw(
            sensitivity.SensitivityPolicyError("concurrent authority drift")
        ),
    )

    with pytest.raises(egress_runtime._PrepacketStop) as caught:
        egress_runtime._authorize_prompt(
            user_prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
            task_kind="bluecad_cad_repair",
            workspace_id="main",
            has_context=False,
            context=context,
            adapters={},
            registry=SimpleNamespace(),
        )

    stop = caught.value
    assert stop.result == "deny"
    assert stop.reason_code == "bluecad_structural_prompt_authority_changed"
    assert stop.detail_reason == "bluecad_structural_prompt_authority_changed"
    assert stop.ai_error_type == "sensitivity_policy_error"



def test_sensitive_evidence_reuses_matching_derivative_before_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {"id": "derivative-existing"}
    monkeypatch.setattr(
        evidence_egress,
        "_resolve_reusable_model_evidence_derivative",
        lambda **_kwargs: existing,
    )
    monkeypatch.setattr(
        evidence_egress,
        "_validate_evidence_derivative",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        evidence_egress,
        "sanitize_canonical_sources_with_local_model",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching authority must be reused before model execution")
        ),
    )
    sight = EvidenceSight(
        "EVIDENCE_SIGHT_V0\nevidence:e1",
        "sha256:" + "1" * 64,
        ("e1",),
    )

    resolved = evidence_egress._resolve_evidence_derivative(
        workspace_id="main",
        candidate_id="candidate-1",
        source_attempt_id="attempt-1",
        sight=sight,
        ordered_source_refs=("evidence:e1",),
        source_digests={"evidence:e1": "sha256:" + "5" * 64},
        effective_levels=("S2",),
        adapters={},
    )

    assert resolved is existing


def test_lineage_accepts_unknown_raw_source_level() -> None:
    lineage = _lineage()
    lineage["source_effective_levels"] = ["unknown", "S1"]

    with evidence_egress.bind_evidence_lineage(lineage):
        pass



def test_late_evidence_drift_becomes_bluecad_blocked_prepacket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        egress_runtime,
        "authorize_manual_context",
        lambda **_kwargs: (_ for _ in ()).throw(
            sensitivity.SensitivityPolicyError("concurrent evidence authority drift")
        ),
    )

    with evidence_egress.bind_evidence_lineage(_lineage()):
        with pytest.raises(egress_runtime._PrepacketStop) as caught:
            egress_runtime._authorize_context(
                context_blocks=[
                    {
                        "source": "BLUECAD evidence",
                        "content": "sanitized structural evidence",
                    }
                ],
                workspace_id="main",
                policy=SimpleNamespace(max_context_chars=2000),
                prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
            )

    stop = caught.value
    assert stop.result == "deny"
    assert stop.reason_code == "bluecad_evidence_authority_changed"
    assert stop.detail_reason == "bluecad_evidence_authority_changed"
    assert stop.ai_error_type == "sensitivity_policy_error"



def test_deterministic_evidence_approval_binds_transaction_source_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def approve(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(derivative_id="derivative-transaction")

    monkeypatch.setattr(evidence_egress, "auto_approve_canonical_derivative", approve)
    monkeypatch.setattr(
        evidence_egress,
        "_canonical_derivative_row",
        lambda _workspace_id, derivative_id: {"id": derivative_id},
    )
    sight = EvidenceSight(
        "EVIDENCE_SIGHT_V0\nevidence:e2\nevidence:e1",
        "sha256:" + "1" * 64,
        ("e2", "e1"),
    )

    resolved = evidence_egress._resolve_evidence_derivative(
        workspace_id="main",
        candidate_id="candidate-1",
        source_attempt_id="attempt-1",
        sight=sight,
        ordered_source_refs=("evidence:e2", "evidence:e1"),
        source_digests={
            "evidence:e2": "sha256:" + "5" * 64,
            "evidence:e1": "sha256:" + "6" * 64,
        },
        effective_levels=("S0", "S1"),
        adapters={},
    )

    assert resolved == {"id": "derivative-transaction"}
    assert captured["expected_source_levels"] == {
        "evidence:e2": "S0",
        "evidence:e1": "S1",
    }


def test_noneligible_bluecad_context_authority_is_blocked_prepacket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = SimpleNamespace(
        result="pause",
        included_manifest=(),
        withheld_manifest=({"source_ref": "derivative:stale"},),
    )
    monkeypatch.setattr(
        egress_runtime,
        "authorize_manual_context",
        lambda **_kwargs: authority,
    )

    with evidence_egress.bind_evidence_lineage(_lineage()):
        with pytest.raises(egress_runtime._PrepacketStop) as caught:
            egress_runtime._authorize_context(
                context_blocks=[
                    {
                        "source": "BLUECAD evidence",
                        "content": "sanitized structural evidence",
                    }
                ],
                workspace_id="main",
                policy=SimpleNamespace(max_context_chars=2000),
                prompt="CONFIDENTIAL PROJECT GEOMETRY: raw task.",
            )

    stop = caught.value
    assert stop.result == "deny"
    assert stop.reason_code == "bluecad_evidence_authority_changed"
    assert stop.ai_error_type == "sensitivity_policy_error"



def test_model_evidence_approval_forwards_transaction_source_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        evidence_egress,
        "_resolve_reusable_model_evidence_derivative",
        lambda **_kwargs: None,
    )

    def sanitize(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(derivative_id="derivative-model-transaction")

    monkeypatch.setattr(
        evidence_egress,
        "sanitize_canonical_sources_with_local_model",
        sanitize,
    )
    monkeypatch.setattr(
        evidence_egress,
        "_canonical_derivative_row",
        lambda _workspace_id, derivative_id: {"id": derivative_id},
    )
    sight = EvidenceSight(
        "EVIDENCE_SIGHT_V0\nevidence:e2\nevidence:e1",
        "sha256:" + "1" * 64,
        ("e2", "e1"),
    )

    resolved = evidence_egress._resolve_evidence_derivative(
        workspace_id="main",
        candidate_id="candidate-1",
        source_attempt_id="attempt-1",
        sight=sight,
        ordered_source_refs=("evidence:e2", "evidence:e1"),
        source_digests={
            "evidence:e2": "sha256:" + "5" * 64,
            "evidence:e1": "sha256:" + "6" * 64,
        },
        effective_levels=("S2", "unknown"),
        adapters={},
    )

    assert resolved == {"id": "derivative-model-transaction"}
    assert captured["expected_source_levels"] == {
        "evidence:e2": "S2",
        "evidence:e1": "unknown",
    }
