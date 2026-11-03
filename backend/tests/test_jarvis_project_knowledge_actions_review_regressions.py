from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.modules.memory.jarvis_knowledge_actions as knowledge
from app.modules.ai.jarvis_context_models import JarvisExactRef


class _Record:
    def __init__(self, record_id: str, updated_at: str, **extra: object) -> None:
        self.id = record_id
        self.updated_at = updated_at
        for key, value in extra.items():
            setattr(self, key, value)

    def model_dump(self, **_: object) -> dict[str, object]:
        return dict(vars(self))


def _preview(monkeypatch, record: _Record) -> dict[str, object]:
    monkeypatch.setattr(knowledge, "get_context_record_exact", lambda workspace_id, kind, record_id: record)
    monkeypatch.setattr(knowledge.sensitivity, "get_current_sensitivity_label", lambda workspace_id, subject_ref: None)
    return knowledge.build_knowledge_preview(
        knowledge.KnowledgeContextPreviewRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            refs=[{"owner": "modeling", "stable_ref": f"requirement:{record.id}"}],
        )
    )


def test_semantic_proposal_refuses_secret_bearing_exact_context_before_ai(monkeypatch) -> None:
    record = _Record(
        "req-secret",
        "r1",
        statement="Need bounded proof",
        status="active",
        notes="api_key=sk-test-secret-12345678",
    )
    preview = _preview(monkeypatch, record)
    calls: list[object] = []

    def unexpected_runner(request):
        calls.append(request)
        raise AssertionError("secret-bearing context must be refused before model dispatch")

    result = knowledge.KnowledgeActionsService(auto_runner=unexpected_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Clarify the requirement",
            exact_refs=[JarvisExactRef.model_validate(preview["exact_refs"][0])],
            expected_context_digest=str(preview["context_digest"]),
            semantic=True,
        )
    )

    assert result == {"state": "refused", "reason": "sensitive_context"}
    assert calls == []


def test_semantic_proposal_refuses_generic_token_assignment_before_ai(monkeypatch) -> None:
    record = _Record(
        "req-token",
        "r1",
        statement="Need bounded proof",
        status="active",
        notes="token=abcdefghijklmnop",
    )
    preview = _preview(monkeypatch, record)
    calls: list[object] = []

    def unexpected_runner(request):
        calls.append(request)
        raise AssertionError("generic token assignments must be refused before model dispatch")

    result = knowledge.KnowledgeActionsService(auto_runner=unexpected_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Clarify the requirement",
            exact_refs=[JarvisExactRef.model_validate(preview["exact_refs"][0])],
            expected_context_digest=str(preview["context_digest"]),
            semantic=True,
        )
    )

    assert result == {"state": "refused", "reason": "sensitive_context"}
    assert calls == []


def test_secret_detector_rejects_prefixed_credential_assignments() -> None:
    for credential in (
        "GITHUB_TOKEN=abcdefghijklmnop",
        "OPENAI_API_KEY=abcdefghijklmnop",
        "DATABASE_PASSWORD=abcdefghijklmnop",
        "AWS_SECRET_ACCESS_KEY=abcdefghijklmnop",
    ):
        assert knowledge._contains_secret_material([{"content": credential}])


def test_secret_detector_rejects_double_quoted_credential_assignments() -> None:
    for credential in (
        'GITHUB_TOKEN="abcdefghijklmnop"',
        'OPENAI_API_KEY="abcdefghijklmnop"',
        'DATABASE_PASSWORD="abcdefghijklmnop"',
        'api_key="abcdefghijklmnop"',
    ):
        assert knowledge._contains_secret_material([{"content": credential}])


def test_operator_intent_secret_is_refused_for_template_path(monkeypatch) -> None:
    record = _Record("req-intent", "r1", statement="Need bounded proof", status="active")
    preview = _preview(monkeypatch, record)

    result = knowledge.KnowledgeActionsService().propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Use token=abcdefghijklmnop when drafting the proposal",
            exact_refs=[JarvisExactRef.model_validate(preview["exact_refs"][0])],
            expected_context_digest=str(preview["context_digest"]),
            semantic=False,
        )
    )

    assert result == {"state": "refused", "reason": "sensitive_context"}


def test_semantic_generated_secret_is_refused_before_response(monkeypatch) -> None:
    record = _Record("req-generated", "r1", statement="Need bounded proof", status="active")
    preview = _preview(monkeypatch, record)

    def secret_runner(request):
        return SimpleNamespace(
            status="success",
            response_text=json.dumps(
                {
                    "summary": "Keep token=abcdefghijklmnop for later",
                    "proposed_items": ["Review bounded evidence"],
                    "questions": [],
                    "research_steps": [],
                    "assumptions": [],
                    "warnings": [],
                    "authoritative_next_action": None,
                }
            ),
            ledger_id="job-1",
            selected_route_class="local",
            provider_id="test",
            model_id="test-model",
        )

    result = knowledge.KnowledgeActionsService(auto_runner=secret_runner).propose(
        knowledge.KnowledgeProposalRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            intent="Clarify the requirement",
            exact_refs=[JarvisExactRef.model_validate(preview["exact_refs"][0])],
            expected_context_digest=str(preview["context_digest"]),
            semantic=True,
        )
    )

    assert result == {"state": "refused", "reason": "sensitive_context"}


def test_duplicate_stable_refs_preserve_111_deduplication(monkeypatch) -> None:
    record = _Record("req-1", "r1", statement="Need bounded proof", status="active")
    monkeypatch.setattr(knowledge, "get_context_record_exact", lambda workspace_id, kind, record_id: record)

    preview = knowledge.build_knowledge_preview(
        knowledge.KnowledgeContextPreviewRequest(
            workspace_id="ws-1",
            route_id="memory-project-basis",
            refs=[
                {"owner": "modeling", "stable_ref": "requirement:req-1"},
                {"owner": "modeling", "stable_ref": "requirement:req-1"},
            ],
        )
    )

    assert preview["included_count"] == 1
    assert len(preview["exact_refs"]) == 1


def test_retired_requirement_resolves_stale(monkeypatch) -> None:
    record = _Record("req-retired", "r1", statement="Old requirement", status="retired")
    monkeypatch.setattr(knowledge, "get_context_record_exact", lambda workspace_id, kind, record_id: record)
    ref = JarvisExactRef(
        workspace_id="ws-1",
        owner="modeling",
        kind="requirement",
        id="req-retired",
        revision="r1",
    )

    resolved = knowledge._ProjectBasisAdapter().resolve(ref)

    assert resolved.state == "stale"


def test_superseded_assumption_resolves_stale(monkeypatch) -> None:
    record = _Record("assumption-old", "r1", statement="Old assumption", status="superseded")
    monkeypatch.setattr(knowledge, "get_context_record_exact", lambda workspace_id, kind, record_id: record)
    ref = JarvisExactRef(
        workspace_id="ws-1",
        owner="modeling",
        kind="assumption",
        id="assumption-old",
        revision="r1",
    )

    resolved = knowledge._ProjectBasisAdapter().resolve(ref)

    assert resolved.state == "stale"


def test_stale_preview_preserves_typed_refusal(monkeypatch) -> None:
    record = _Record("req-retired", "r1", statement="Old requirement", status="retired")
    monkeypatch.setattr(knowledge, "get_context_record_exact", lambda workspace_id, kind, record_id: record)

    with pytest.raises(knowledge.KnowledgeActionError) as caught:
        knowledge.build_knowledge_preview(
            knowledge.KnowledgeContextPreviewRequest(
                workspace_id="ws-1",
                route_id="memory-project-basis",
                refs=[{"owner": "modeling", "stable_ref": "requirement:req-retired"}],
            )
        )

    assert caught.value.reason == "stale_context"


def test_literature_unavailable_backing_fails_closed(monkeypatch) -> None:
    source = SimpleNamespace(
        id="source-1",
        source_ref="literature_source:source-1",
        updated_at="r1",
        backing=SimpleNamespace(availability="missing"),
        entries=[],
    )
    monkeypatch.setattr(knowledge, "get_literature_source", lambda workspace_id, source_id: source)
    ref = JarvisExactRef(
        workspace_id="ws-1",
        owner="literature",
        kind="source",
        id="source-1",
        revision="r1",
        immutable_ref="literature_source:source-1",
    )

    resolved = knowledge._LiteratureAdapter().resolve(ref)

    assert resolved.state == "unavailable"
    assert resolved.provenance["backing_availability"] == "missing"
