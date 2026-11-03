from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.database import initialize_database, open_retrieval_index_connection, open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.ai import retrieval_owners
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.ai.retrieval_index import HashingEmbedder, SQLiteIndexStore
from app.modules.ai.settings import ensure_ai_settings
from app.modules.ai.thread_models import AIThreadCreate, AIThreadSubmit
from app.modules.ai.thread_service import create_thread, submit_interaction
from app.modules.development.models import RoadmapItemCreate, RoadmapItemUpdate
from app.modules.development.service import (
    create_roadmap_item,
    delete_roadmap_item,
    update_roadmap_item,
)
from app.modules.engineering.evaluator_contracts import (
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
)
from app.modules.engineering.refs import EvaluationResultRef, Quantity
from app.modules.memory.literature_models import LiteratureEntryCreate, LiteratureSourceCreate
from app.modules.memory.literature_service import create_literature_entry, create_literature_source
from app.modules.modeling.models import DecisionCreate, RequirementCreate, RequirementUpdate, SimulationRunCreate
from app.modules.modeling.service import (
    create_decision,
    create_requirement,
    create_simulation_run,
    update_requirement,
)
from app.modules.process_stack import editor as process_editor
from app.modules.process_stack.correlations import PIPE_EVALUATOR_ID
from app.modules.project_knowledge.models import ApprovalRequest, DraftCreate, ProjectKnowledgeOperation
from app.modules.project_knowledge.service import approve_draft, create_draft, preview_impact
from app.modules.workspaces.service import seed_default_workspace


class CountingEmbedder(HashingEmbedder):
    def __init__(self) -> None:
        super().__init__(dimensions=128)
        self.calls = 0

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls += 1
        return super().embed(text)


class _StudyFixtureEvaluator:
    def descriptor(self):
        return EvaluatorDescriptor(evaluator_id=PIPE_EVALUATOR_ID, backend_kind="specialist",
                                   backend_name="retrieval test evaluator", backend_version="test",
                                   fidelity="screening")

    def availability(self):
        from datetime import UTC, datetime

        return EvaluatorAvailability(evaluator_id=PIPE_EVALUATOR_ID, state="available",
                                     checked_at=datetime.now(UTC), backend_version="test")

    def evaluate(self, request):
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        return EvaluationResult(
            result_ref=EvaluationResultRef(authority_owner="test", object_id=request.request_ref.object_id,
                                           workspace_id=request.request_ref.workspace_id, revision="1"),
            request_ref=request.request_ref, evaluator_id=PIPE_EVALUATOR_ID, backend_version="test",
            status="succeeded", fidelity="screening",
            outputs=(NamedQuantity(name="pressure_drop", value=Quantity(value=1.0, unit="Pa")),),
            numerical=NumericalDiagnostics(converged=True), started_at=now, completed_at=now,
        )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SQLiteIndexStore:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()
    initialize_database()
    return SQLiteIndexStore(documents=lambda: ())


def _owner_doc(workspace_id: str, kind: str, object_id: str, text: str) -> IndexDocument:
    return IndexDocument(
        source_ref=SourceRef(authority_owner="development", object_type=kind, object_id=object_id,
                             workspace_id=workspace_id, revision="1", content_digest=canonical_digest(text)),
        text=text,
    )


def test_owner_catch_up_is_idempotent_and_rolls_back_on_failure(
    store: SQLiteIndexStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = CountingEmbedder()
    store.embedder = embedder
    current = [_owner_doc("w", "roadmap_item", "raw-1", "Lifecycle: raw exploratory capture")]
    store.resolver = lambda ref: next((doc for doc in current if doc.source_ref.object_id == ref.object_id), None)
    monkeypatch.setattr(retrieval_owners, "canonical_owner_documents", lambda owners=None: iter(current))
    monkeypatch.setattr(retrieval_owners, "_owner_fingerprints",
                        lambda: {"development": canonical_digest([doc.text for doc in current])})

    first = retrieval_owners.owner_catch_up(store)
    initial_calls = embedder.calls
    with open_retrieval_index_connection() as db:
        original_row = tuple(db.execute("SELECT ref,text,vector FROM docs WHERE object_id='raw-1'").fetchone())
    assert first["development"]["changed"] == 1
    assert initial_calls == 3  # source document, its summary, and owner group summary
    assert retrieval_owners.owner_catch_up(store)["development"]["embedded"] == 0
    assert embedder.calls == initial_calls
    with open_retrieval_index_connection() as db:
        assert tuple(db.execute("SELECT ref,text,vector FROM docs WHERE object_id='raw-1'").fetchone()) == original_row

    original_upsert = store._upsert
    injected = True

    def fail_after_write(db, documents):
        nonlocal injected
        original_upsert(db, documents)
        if injected:
            injected = False
            raise RuntimeError("injected catch-up interruption")

    monkeypatch.setattr(store, "_upsert", fail_after_write)
    current[:] = [_owner_doc("w", "roadmap_item", "raw-1", "Lifecycle: changed exploratory capture")]
    with pytest.raises(RuntimeError, match="injected catch-up interruption"):
        retrieval_owners.owner_catch_up(store)
    with open_retrieval_index_connection() as db:
        old = db.execute("SELECT text FROM docs WHERE object_id='raw-1'").fetchone()[0]
        assert "raw exploratory" in old
        assert db.execute("SELECT COUNT(*) FROM owner_manifest").fetchone()[0] == 1

    monkeypatch.setattr(store, "_upsert", original_upsert)
    result = retrieval_owners.owner_catch_up(store)
    assert result["development"]["changed"] == 1
    assert store.resolve_authoritative(current[0].source_ref).state == "current"
    current.clear()
    deleted = retrieval_owners.owner_catch_up(store)
    assert deleted["development"]["deleted"] == 1
    assert not store.search_lexical("changed exploratory", limit=5, workspace_id="w")


def test_offline_owner_mutation_catch_up_matches_full_rebuild(
    store: SQLiteIndexStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_owner_doc("w", "roadmap_item", "raw-0", "Lifecycle: initial capture")]
    def fingerprints() -> dict[str, str]:
        return {"development": canonical_digest([doc.source_ref.content_digest for doc in current])}

    monkeypatch.setattr(retrieval_owners, "canonical_owner_documents", lambda owners=None: iter(current))
    monkeypatch.setattr(retrieval_owners, "_owner_fingerprints", fingerprints)
    retrieval_owners.owner_catch_up(store)
    current[:] = [
        _owner_doc("w", "roadmap_item", f"raw-{n}", f"Lifecycle: offline capture {n}")
        for n in range(20)
    ]
    result = retrieval_owners.owner_catch_up(store)
    assert result["development"]["changed"] == 20
    with open_retrieval_index_connection() as db:
        caught_up = [tuple(row) for row in db.execute("SELECT key,ref,title,text,vector,workspace_id FROM docs ORDER BY key")]
        caught_edges = [tuple(row) for row in db.execute("SELECT source_key,target_key,kind,valid_from FROM edges ORDER BY 1,2,3")]
    full_rebuild = SQLiteIndexStore(embedder=store.embedder, documents=lambda: iter(current))
    full_rebuild.rebuild()
    with open_retrieval_index_connection() as db:
        rebuilt = [tuple(row) for row in db.execute("SELECT key,ref,title,text,vector,workspace_id FROM docs ORDER BY key")]
        rebuilt_edges = [tuple(row) for row in db.execute("SELECT source_key,target_key,kind,valid_from FROM edges ORDER BY 1,2,3")]
    assert caught_up == rebuilt
    assert caught_edges == rebuilt_edges


def test_roadmap_mutation_and_delete_are_caught_up(store: SQLiteIndexStore) -> None:
    workspace = seed_default_workspace()
    item = create_roadmap_item(RoadmapItemCreate(
        workspace_id=workspace.id, title="Owner sync target", item_type="Task", created_by="test",
    ))
    retrieval_owners.owner_catch_up(store)
    with open_retrieval_index_connection() as db:
        old_ref = SourceRef.model_validate_json(db.execute(
            "SELECT ref FROM docs WHERE object_id=? AND json_extract(ref,'$.object_type')='roadmap_item'",
            (str(item["id"]),),
        ).fetchone()[0])
    update_roadmap_item(str(item["id"]), RoadmapItemUpdate(
        workspace_id=workspace.id, expected_revision=1, title="Owner sync changed", actor="test",
    ))
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(old_ref).state == "stale"
    delete_roadmap_item(workspace.id, str(item["id"]), 2, "test")
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(old_ref).state == "unavailable"


def test_legacy_modeling_owners_catch_up_without_events(store: SQLiteIndexStore) -> None:
    workspace = seed_default_workspace()
    decision = create_decision(workspace.id, DecisionCreate(
        title="Pump selection", decision_text="Use the small pump",
    ))
    requirement = create_requirement(workspace.id, RequirementCreate(
        statement="Pressure stays below 10 kPa", status="active",
    ))
    retrieval_owners.owner_catch_up(store)
    with open_retrieval_index_connection() as db:
        old_decision = SourceRef.model_validate_json(db.execute(
            "SELECT ref FROM docs WHERE object_id=? AND json_extract(ref,'$.object_type')='decision'",
            (decision.id,),
        ).fetchone()[0])
    with open_sqlite_connection() as db:
        db.execute("UPDATE decisions SET decision_text=? WHERE id=?", ("Use the variable speed pump", decision.id))
        db.commit()
    update_requirement(requirement.id, RequirementUpdate(statement="Pressure stays below 8 kPa"))

    changed = retrieval_owners.owner_catch_up(store)
    assert changed["modeling"]["changed"] >= 2
    assert store.resolve_authoritative(old_decision).state == "stale"
    with open_retrieval_index_connection() as db:
        texts = {str(row["object_id"]): str(row["text"]) for row in db.execute(
            "SELECT object_id,text FROM docs WHERE json_extract(ref,'$.authority_owner')='modeling' "
            "AND json_extract(ref,'$.object_type') IN ('decision','requirement')")}
    assert "variable speed pump" in texts[decision.id]
    assert "below 8 kPa" in texts[requirement.id]


def test_literature_entry_mutation_and_delete_are_caught_up(store: SQLiteIndexStore) -> None:
    workspace = seed_default_workspace()
    source = create_literature_source(workspace.id, LiteratureSourceCreate(
        title="Owner sync source", source_kind="paper", citation="Fixture citation",
    ))
    entry = create_literature_entry(workspace.id, source.id, LiteratureEntryCreate(
        entry_kind="claim", statement="Initial claim", status="raw",
    ))
    retrieval_owners.owner_catch_up(store)
    with open_retrieval_index_connection() as db:
        old_ref = SourceRef.model_validate_json(db.execute(
            "SELECT ref FROM docs WHERE object_id=? AND json_extract(ref,'$.object_type')='entry'",
            (entry.id,),
        ).fetchone()[0])
    with open_sqlite_connection() as db:
        db.execute("UPDATE literature_entries SET statement=?,updated_at=? WHERE id=?",
                   ("Updated claim", "2099-01-01T00:00:00Z", entry.id))
        db.commit()
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(old_ref).state == "stale"
    with open_sqlite_connection() as db:
        db.execute("DELETE FROM literature_entries WHERE id=?", (entry.id,))
        db.commit()
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(old_ref).state == "unavailable"


def test_thread_and_interaction_history_exceeds_old_caps(store: SQLiteIndexStore) -> None:
    workspace = seed_default_workspace()
    ensure_ai_settings()
    threads = [create_thread(AIThreadCreate(workspace_id=workspace.id, title=f"thread-{n}"))
               for n in range(60)]
    for n in range(120):
        submit_interaction(
            workspace_id=workspace.id,
            thread_id=threads[0].id,
            payload=AIThreadSubmit(request_id=f"owner-sync-{n}", prompt=f"Offline test prompt {n}"),
        )
    retrieval_owners.owner_catch_up(store)
    with open_retrieval_index_connection() as db:
        thread_count = db.execute(
            "SELECT COUNT(*) FROM owner_manifest WHERE owner='ai_threads' AND object_type='thread'"
        ).fetchone()[0]
        interaction_count = db.execute(
            "SELECT COUNT(*) FROM owner_manifest WHERE owner='ai_threads' AND object_type='interaction'"
        ).fetchone()[0]
    assert thread_count == 60
    assert interaction_count == 120


def test_project_knowledge_revision_and_simulation_run_are_indexed(store: SQLiteIndexStore) -> None:
    workspace = seed_default_workspace()
    draft = create_draft(DraftCreate(
        workspace_id=workspace.id, parent_kind="reconciled", parent_revision_id=None,
        operations=[ProjectKnowledgeOperation(
            owner_kind="requirement", operation_kind="create",
            fields={"statement": "Retrieval owner coverage", "status": "active",
                    "basis_kind": "requirement", "reconciliation_gate": "advisory"},
        )],
    ))
    preview = preview_impact(workspace.id, draft.id)
    revision = approve_draft(ApprovalRequest(
        workspace_id=workspace.id, approval_request_key="retrieval-owner-test",
        draft_id=draft.id, expected_draft_revision_token=draft.revision_token,
        expected_preview_digest=preview.digest,
    )).working_revision_id
    assert revision is not None
    run = create_simulation_run(workspace.id, SimulationRunCreate(
        status="completed", output_payload='{"pressure_bar": 10.0}',
    ))
    retrieval_owners.owner_catch_up(store)
    with open_retrieval_index_connection() as db:
        revision_ref = SourceRef.model_validate_json(db.execute(
            "SELECT ref FROM docs WHERE object_id=? AND json_extract(ref,'$.object_type')='revision'",
            (revision,),
        ).fetchone()[0])
        run_ref = SourceRef.model_validate_json(db.execute(
            "SELECT ref FROM docs WHERE object_id=? AND json_extract(ref,'$.object_type')='simulation_run'",
            (run.id,),
        ).fetchone()[0])
        revision_exists = db.execute(
            "SELECT 1 FROM owner_manifest WHERE owner='project_knowledge' AND object_type='revision' AND key=?",
            (canonical_digest(("project_knowledge", "revision", revision, workspace.id, None)),),
        ).fetchone()
        run_exists = db.execute(
            "SELECT 1 FROM owner_manifest WHERE owner='modeling' AND object_type='simulation_run' AND key=?",
            (canonical_digest(("modeling", "simulation_run", run.id, workspace.id, None)),),
        ).fetchone()
    assert revision_exists is not None
    assert run_exists is not None
    with open_sqlite_connection() as db:
        db.execute("UPDATE project_knowledge_revisions SET state='discarded' WHERE id=?", (revision,))
        db.commit()
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(revision_ref).state == "stale"
    with open_sqlite_connection() as db:
        db.execute("DELETE FROM simulation_runs WHERE id=?", (run.id,))
        db.commit()
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(run_ref).state == "unavailable"


def test_process_revision_metadata_indexes_without_opening_dwsim(store: SQLiteIndexStore) -> None:
    workspace = seed_default_workspace()
    case_id = str(uuid4())
    case_dir = build_paths().workspaces_dir / workspace.id / "process" / "dwsim" / case_id
    native = case_dir / "seed.dwxmz"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"offline opaque simulation fixture")
    revision = process_editor._write_revision(
        case_dir, native, command="create_case", parent=None, readback={"object_count": 0},
        version="10.2.9", mcp_sha="a" * 64,
    )
    retrieval_owners.owner_catch_up(store)
    key = canonical_digest(("process_stack", "case_revision", f"{case_id}:1", workspace.id, None))
    with open_retrieval_index_connection() as db:
        row = db.execute("SELECT ref FROM docs WHERE key=?", (key,)).fetchone()
    assert row is not None
    assert revision["case_sha256"] == process_editor.list_revisions(workspace.id, case_id)[0].case_sha256
    old_ref = SourceRef.model_validate_json(row[0])
    shutil.rmtree(case_dir)
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(old_ref).state == "unavailable"


def test_engineering_study_run_is_indexed_from_owner_record(store: SQLiteIndexStore) -> None:
    workspace = seed_default_workspace()
    app = create_app()
    app.state.engineering_evaluator_registry = {PIPE_EVALUATOR_ID: _StudyFixtureEvaluator()}
    with TestClient(app) as client:
        definition = {
            "study_ref": {"authority_owner": "engineering", "object_type": "study",
                          "object_id": "owner-study", "workspace_id": workspace.id, "revision": "1"},
            "evaluator_id": PIPE_EVALUATOR_ID,
            "subject_ref": {"authority_owner": "engineering", "object_type": "physics_case",
                            "object_id": "pipe", "workspace_id": workspace.id, "revision": "1"},
            "method": "grid",
            "variables": [
                {"name": "tube_inner_diameter", "domain": {"variable": "tube_inner_diameter",
                 "lower": {"value": 0.01, "unit": "m"}, "upper": {"value": 0.02, "unit": "m"}},
                 "step": {"value": 0.01, "unit": "m"}},
                {"name": "loop_length", "domain": {"variable": "loop_length",
                 "lower": {"value": 0.2, "unit": "m"}, "upper": {"value": 0.2, "unit": "m"}},
                 "step": {"value": 0.1, "unit": "m"}},
            ],
            "fixed_inputs": [
                {"name": "density", "value": {"value": 1000, "unit": "kg/m3"}},
                {"name": "dynamic_viscosity", "value": {"value": 0.001, "unit": "Pa*s"}},
                {"name": "velocity", "value": {"value": 0.01, "unit": "m/s"}},
                {"name": "roughness", "value": {"value": 0, "unit": "m"}},
            ],
            "objectives": [{"output": "pressure_drop", "sense": "minimize"}],
            "seed": 1, "budget": 3, "sample_count": 3,
        }
        response = client.post(f"/workspaces/{workspace.id}/engineering/studies", json=definition)
        assert response.status_code == 200, response.text
        run = response.json()
    retrieval_owners.owner_catch_up(store)
    with open_retrieval_index_connection() as db:
        row = db.execute(
            "SELECT ref FROM docs WHERE object_id=? AND json_extract(ref,'$.object_type')='study_run'",
            (f"owner-study:{run['content_digest']}",),
        ).fetchone()
    assert row is not None
    old_ref = SourceRef.model_validate_json(row[0])
    from app.modules.engineering.operator_service import digest_file_name

    run_path = (build_paths().workspaces_dir / workspace.id / "engineering" / "studies" /
                "owner-study" / "runs" / digest_file_name(run["content_digest"]))
    run_path.unlink()
    retrieval_owners.owner_catch_up(store)
    assert store.resolve_authoritative(old_ref).state == "unavailable"
