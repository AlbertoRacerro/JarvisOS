from __future__ import annotations

from types import SimpleNamespace

import app.modules.memory.jarvis_knowledge_actions as knowledge
from app.modules.ai.jarvis_context_models import JarvisExactRef


class _Record:
    def __init__(self, record_id: str, updated_at: str, **extra: object) -> None:
        self.id = record_id
        self.updated_at = updated_at
        for key, value in extra.items():
            setattr(self, key, value)

    def model_dump(self, **_: object) -> dict[str, object]:
        return {key: value for key, value in vars(self).items()}


class _Identity:
    model_spec_id = "spec-1"
    model_version_id = "version-1"
    version_label = "v1"
    status = "validated"
    created_at = "2026-09-01T00:00:00Z"
    input_contract_digest = "abc"

    def model_dump(self, **_: object) -> dict[str, object]:
        return {
            "model_spec_id": self.model_spec_id,
            "model_version_id": self.model_version_id,
            "version_label": self.version_label,
            "status": self.status,
            "created_at": self.created_at,
            "input_contract_digest": self.input_contract_digest,
        }


def _project_record(revision: str = "r1") -> _Record:
    return _Record("req-1", revision, statement="Need bounded proof", status="active")


def _project_preview(monkeypatch):
    monkeypatch.setattr(knowledge, "get_context_record_exact", lambda workspace_id, kind, record_id: _project_record())
    monkeypatch.setattr(
        knowledge.sensitivity,
        "get_current_sensitivity_label",
        lambda workspace_id, subject_ref: None,
    )
    return knowledge.build_knowledge_preview(
        knowledge.KnowledgeContextPreviewRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            refs=[{"owner": "modeling", "stable_ref": "requirement:req-1"}],
        )
    )


def _model_preview(monkeypatch):
    dossier = SimpleNamespace(
        identity=_Identity(),
        title="PBR model",
        engineering_question="Does it work?",
        scope="bounded",
        maturity_status="draft",
        assumptions_summary=None,
        inputs_summary=None,
        outputs_summary=None,
    )
    monkeypatch.setattr(knowledge, "get_model_dossier", lambda workspace_id, version_id: dossier)
    return knowledge.build_knowledge_preview(
        knowledge.KnowledgeContextPreviewRequest(
            workspace_id="ws-1",
            route_id="memory-models",
            refs=[{"owner": "model-dossier", "stable_ref": "model_version:version-1"}],
        )
    )


def test_knowledge_routes_advertise_only_context_and_propose() -> None:
    for route_id in knowledge._ROUTE_PATHS:
        capabilities = knowledge.route_capabilities(route_id)
        assert {item["action_class"] for item in capabilities} == {"CONTEXT", "PROPOSE"}
        assert {item["capability_id"] for item in capabilities} == {
            "knowledge.add-context",
            "knowledge.propose",
        }
        assert not {"COMMIT", "EXECUTE"} & {item["action_class"] for item in capabilities}


def test_project_basis_preview_is_explicit_exact_and_stale_safe(monkeypatch) -> None:
    record = _project_record()
    monkeypatch.setattr(knowledge, "get_context_record_exact", lambda workspace_id, kind, record_id: record)
    payload = knowledge.KnowledgeContextPreviewRequest(
        workspace_id="ws-1",
        route_id="memory-project-basis",
        refs=[{"owner": "modeling", "stable_ref": "requirement:req-1"}],
    )
    preview = knowledge.build_knowledge_preview(payload)
    exact = preview["exact_refs"][0]
    assert exact == {
        "workspace_id": "ws-1",
        "owner": "modeling",
        "kind": "requirement",
        "id": "req-1",
        "revision": "r1",
    }
    assert preview["included_count"] == 1
    assert str(preview["context_digest"]).startswith("sha256:")

    record.updated_at = "r2"
    service = knowledge.KnowledgeActionsService(auto_runner=lambda request: None)
    refused = service.propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Suggest one clarification",
            exact_refs=[JarvisExactRef.model_validate(exact)],
            expected_context_digest=str(preview["context_digest"]),
        )
    )
    assert refused == {"state": "refused", "reason": "stale_context"}


def test_model_version_and_literature_refs_re_resolve_owner_truth(monkeypatch) -> None:
    dossier = SimpleNamespace(
        identity=_Identity(),
        title="PBR model",
        engineering_question="Does it work?",
        scope="bounded",
        maturity_status="draft",
        assumptions_summary=None,
        inputs_summary=None,
        outputs_summary=None,
    )
    monkeypatch.setattr(knowledge, "get_model_dossier", lambda workspace_id, version_id: dossier)
    model_preview = knowledge.build_knowledge_preview(
        knowledge.KnowledgeContextPreviewRequest(
            workspace_id="ws-1",
            route_id="memory-models",
            refs=[{"owner": "model-dossier", "stable_ref": "model_version:version-1"}],
        )
    )
    assert model_preview["exact_refs"][0]["immutable_ref"] == "model_version:version-1"

    entry = SimpleNamespace(
        id="entry-1",
        source_id="source-1",
        entry_kind="finding",
        statement="Observed value",
        value_text=None,
        value_number=1.0,
        unit="m",
        status="accepted",
        locator_kind="page",
        locator_start=2,
        locator_end=2,
        context_text="bounded evidence",
        provenance_ref="literature_entry:entry-1",
        used_by=[],
        updated_at="lit-r1",
    )
    source = SimpleNamespace(
        id="source-1",
        title="Paper",
        source_kind="paper",
        state="active",
        citation="Citation",
        publisher="Publisher",
        published_year=2026,
        source_ref="literature_source:source-1",
        updated_at="source-r1",
        entries=[entry],
    )
    monkeypatch.setattr(knowledge, "_literature_source_for_entry", lambda workspace_id, entry_id: "source-1")
    monkeypatch.setattr(knowledge, "get_literature_source", lambda workspace_id, source_id: source)
    lit_preview = knowledge.build_knowledge_preview(
        knowledge.KnowledgeContextPreviewRequest(
            workspace_id="ws-1",
            route_id="memory-literature",
            refs=[{"owner": "literature", "stable_ref": "literature_entry:entry-1"}],
        )
    )
    assert lit_preview["exact_refs"][0]["revision"] == "lit-r1"
    assert lit_preview["exact_refs"][0]["immutable_ref"] == "literature_entry:entry-1"


def test_template_proposal_is_ephemeral_and_does_not_call_ai(monkeypatch) -> None:
    preview = _project_preview(monkeypatch)

    def unexpected_ai_call(_request):
        raise AssertionError("template proposal must not dispatch AI")

    result = knowledge.KnowledgeActionsService(auto_runner=unexpected_ai_call).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Make this requirement measurable",
            exact_refs=[JarvisExactRef.model_validate(preview["exact_refs"][0])],
            expected_context_digest=str(preview["context_digest"]),
        )
    )
    assert result["state"] == "proposed"
    assert result["target_domain"] == "project_basis"
    assert result["context_digest"] == preview["context_digest"]
    assert result["generated_by"] == {
        "kind": "deterministic_template",
        "template_id": "knowledge-proposal-v1",
    }
    assert "commit" not in result and "execute" not in result


def test_semantic_proposal_uses_governed_auto_runner_and_closed_schema(monkeypatch) -> None:
    preview = _model_preview(monkeypatch)
    calls = []

    def fake_auto_runner(request):
        calls.append(request)
        return SimpleNamespace(
            status="success",
            response_text='{"summary":"Clarify the model question.","proposed_items":["Review the bounded model assumptions"],"questions":[],"research_steps":[],"assumptions":[],"warnings":[],"authoritative_next_action":"Apply this model change immediately."}',
            ledger_id="job-1",
            selected_route_class="local:fast",
            provider_id="local_ollama",
            model_id="qwen3:8b",
        )

    result = knowledge.KnowledgeActionsService(auto_runner=fake_auto_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-models",
            intent="Clarify the model assumptions",
            exact_refs=[JarvisExactRef.model_validate(preview["exact_refs"][0])],
            expected_context_digest=str(preview["context_digest"]),
            semantic=True,
        )
    )
    assert result["state"] == "proposed"
    assert result["generated_by"]["kind"] == "ai_task"
    assert result["authoritative_next_action"] == knowledge._ROUTE_NEXT_ACTION["memory-models"]
    assert calls[0].route_class == "auto"
    assert calls[0].include_project_context is False


def test_malformed_semantic_output_is_refused(monkeypatch) -> None:
    preview = _model_preview(monkeypatch)

    def fake_auto_runner(_request):
        return SimpleNamespace(
            status="success",
            response_text="not-json",
            ledger_id="job-1",
            selected_route_class="local:fast",
            provider_id="local_ollama",
            model_id="qwen3:8b",
        )

    result = knowledge.KnowledgeActionsService(auto_runner=fake_auto_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-models",
            intent="Clarify the model assumptions",
            exact_refs=[JarvisExactRef.model_validate(preview["exact_refs"][0])],
            expected_context_digest=str(preview["context_digest"]),
            semantic=True,
        )
    )
    assert result == {"state": "refused", "reason": "proposal_invalid"}


def test_cross_route_search_identity_cannot_be_promoted_to_context() -> None:
    try:
        knowledge.exact_ref_from_stable(
            "ws-1",
            "memory-project-basis",
            knowledge.StableKnowledgeRef(owner="literature", stable_ref="literature_source:source-1"),
        )
    except knowledge.KnowledgeActionError as exc:
        assert exc.reason == "unsupported_ref"
    else:
        raise AssertionError("cross-route owner identity must fail closed")
