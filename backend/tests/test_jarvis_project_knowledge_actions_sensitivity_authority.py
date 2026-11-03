from __future__ import annotations

import json
from types import SimpleNamespace

import app.modules.memory.jarvis_knowledge_actions as knowledge
from app.modules.ai.jarvis_context_models import JarvisExactRef


def _project_ref(record_id: str = "req-1") -> JarvisExactRef:
    return JarvisExactRef(
        workspace_id="ws-1",
        owner="modeling",
        kind="requirement",
        id=record_id,
        revision="r1",
    )


def _preview(ref: JarvisExactRef) -> SimpleNamespace:
    return SimpleNamespace(
        blocks=[{"content": "bounded evidence"}],
        request=SimpleNamespace(added_context_refs=[ref]),
        context_digest="sha256:" + "a" * 64,
        context_sources_manifest=[],
    )


def test_semantic_proposal_refuses_current_human_s4_label_before_ai(monkeypatch) -> None:
    ref = _project_ref("req-human-s4")
    preview = _preview(ref)
    monkeypatch.setattr(knowledge, "require_dispatchable_preview", lambda request, digest: preview)
    monkeypatch.setattr(
        knowledge.sensitivity,
        "get_current_sensitivity_label",
        lambda workspace_id, subject_ref: SimpleNamespace(current=True, level="S4"),
    )
    calls: list[object] = []

    def unexpected_runner(request):
        calls.append(request)
        raise AssertionError("current S4 evidence must be refused before model dispatch")

    result = knowledge.KnowledgeActionsService(auto_runner=unexpected_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Clarify the requirement",
            exact_refs=[ref],
            expected_context_digest=preview.context_digest,
            semantic=True,
        )
    )

    assert result == {"state": "refused", "reason": "sensitive_context"}
    assert calls == []


def test_semantic_project_basis_refuses_dispatch_even_for_current_s0_label(monkeypatch) -> None:
    ref = _project_ref("req-current-s0")
    preview = _preview(ref)
    monkeypatch.setattr(knowledge, "require_dispatchable_preview", lambda request, digest: preview)
    monkeypatch.setattr(
        knowledge.sensitivity,
        "get_current_sensitivity_label",
        lambda workspace_id, subject_ref: SimpleNamespace(current=True, level="S0"),
    )
    calls: list[object] = []

    def unexpected_runner(request):
        calls.append(request)
        raise AssertionError("mutable Project Basis sensitivity authority must not race model dispatch")

    result = knowledge.KnowledgeActionsService(auto_runner=unexpected_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Clarify the requirement",
            exact_refs=[ref],
            expected_context_digest=preview.context_digest,
            semantic=True,
        )
    )

    assert result == {"state": "refused", "reason": "sensitive_context"}
    assert calls == []


def test_semantic_proposal_refuses_stale_sensitivity_label_before_ai(monkeypatch) -> None:
    ref = _project_ref("req-stale-label")
    preview = _preview(ref)
    monkeypatch.setattr(knowledge, "require_dispatchable_preview", lambda request, digest: preview)
    monkeypatch.setattr(
        knowledge.sensitivity,
        "get_current_sensitivity_label",
        lambda workspace_id, subject_ref: SimpleNamespace(current=False, level="S4"),
    )
    calls: list[object] = []

    def unexpected_runner(request):
        calls.append(request)
        raise AssertionError("stale sensitivity authority must fail closed before model dispatch")

    result = knowledge.KnowledgeActionsService(auto_runner=unexpected_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Clarify the requirement",
            exact_refs=[ref],
            expected_context_digest=preview.context_digest,
            semantic=True,
        )
    )

    assert result == {"state": "refused", "reason": "sensitive_context"}
    assert calls == []


def test_model_dossier_exact_ref_requires_version_label(monkeypatch) -> None:
    identity = SimpleNamespace(
        model_version_id="mv-1",
        model_spec_id="model-1",
        version_label="v1",
        input_contract_digest="sha256:" + "1" * 64,
    )
    monkeypatch.setattr(
        knowledge,
        "get_model_dossier",
        lambda workspace_id, model_version_id: SimpleNamespace(identity=identity),
    )
    ref = JarvisExactRef(
        workspace_id="ws-1",
        owner="model-dossier",
        kind="model-version",
        id="mv-1",
        immutable_ref="model_version:mv-1",
    )

    resolved = knowledge._ModelDossierAdapter().resolve(ref)

    assert resolved.state == "stale"


def test_model_dossier_exact_ref_detects_mutable_projection_drift(monkeypatch) -> None:
    identity_payload = {
        "model_spec_id": "model-1",
        "model_version_id": "mv-1",
        "version_label": "v1",
        "implementation_kind": "python",
        "status": "draft",
        "created_at": "2026-09-12T00:00:00Z",
        "input_contract_digest": "sha256:" + "1" * 64,
    }
    identity = SimpleNamespace(
        **identity_payload,
        model_dump=lambda mode: dict(identity_payload),
    )
    dossier = SimpleNamespace(
        identity=identity,
        title="Original title",
        engineering_question="How?",
        scope="bounded",
        maturity_status="draft",
        assumptions_summary="A",
        inputs_summary="I",
        outputs_summary="O",
    )
    monkeypatch.setattr(knowledge, "get_model_dossier", lambda workspace_id, model_version_id: dossier)

    ref = knowledge.exact_ref_from_stable(
        "ws-1",
        "memory-models",
        knowledge.StableKnowledgeRef(owner="model-dossier", stable_ref="model_version:mv-1"),
    )
    assert ref.content_digest == knowledge._model_dossier_content_digest(dossier)

    dossier.title = "Mutated title"
    resolved = knowledge._ModelDossierAdapter().resolve(ref)

    assert resolved.state == "stale"
    assert resolved.reason == "model dossier content moved"


def test_semantic_authoritative_next_action_is_server_owned(monkeypatch) -> None:
    ref = JarvisExactRef(
        workspace_id="ws-1",
        owner="literature",
        kind="source",
        id="src-next-action",
        immutable_ref="source:src-next-action",
        revision="r1",
    )
    preview = _preview(ref)
    monkeypatch.setattr(knowledge, "require_dispatchable_preview", lambda request, digest: preview)

    def advisory_runner(request):
        return SimpleNamespace(
            status="success",
            response_text=json.dumps(
                {
                    "summary": "Review the requirement",
                    "proposed_items": [],
                    "questions": [],
                    "research_steps": [],
                    "assumptions": [],
                    "warnings": [],
                    "authoritative_next_action": "Commit this change immediately.",
                }
            ),
            ledger_id="job-next-action",
            selected_route_class="local",
            provider_id="test",
            model_id="test-model",
        )

    result = knowledge.KnowledgeActionsService(auto_runner=advisory_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-literature",
            intent="Clarify the requirement",
            exact_refs=[ref],
            expected_context_digest=preview.context_digest,
            semantic=True,
        )
    )

    assert result["state"] == "proposed"
    assert result["authoritative_next_action"] == knowledge._ROUTE_NEXT_ACTION["memory-literature"]