from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.ai.retrieval_index import SQLiteIndexStore, repository_file_documents
from app.modules.ai.retrieval_query import MAX_LIMIT, MAX_TOKEN_BUDGET, query_context


@pytest.fixture
def query_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SQLiteIndexStore, dict[str, IndexDocument]]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()
    documents = {
        "decision": _document("decision", "decision_fixture", "Pump uses the documented pressure boundary", "w1"),
        "development": _document("development", "development", "Raw roadmap pump thought", "w1"),
        "other": _document("other", "decision_fixture", "Separate workspace pump record", "w2"),
        "other_w1": _document("other_w1", "decision_fixture", "Second scoped pump record", "w1"),
    }
    store = SQLiteIndexStore(documents=lambda: documents.values(), resolver=lambda ref: documents.get(ref.object_id))
    yield store, documents
    get_settings.cache_clear()


def _document(object_id: str, owner: str, text: str, workspace: str) -> IndexDocument:
    return IndexDocument(source_ref=SourceRef(authority_owner=owner, object_type="record", object_id=object_id,
                         workspace_id=workspace, revision="1", content_digest=canonical_digest(text)), text=text)


def test_query_scopes_owner_workspace_caps_and_stable_json(query_store) -> None:
    store, documents = query_store
    store.rebuild()
    result = query_context("pump", workspace_id="w1", source_scope=("decision_fixture",), limit=2,
                           token_budget=40,
                           allowed_refs=frozenset({("decision_fixture", "record", "decision")}), store=store)
    assert result["schema_version"] == "retrieval-query.v1"
    assert set(result) == {"schema_version", "query", "workspace_id", "source_scope", "evidence_role",
                           "freshness", "bundle", "evidence"}
    assert result["evidence_role"] == "reference_data_not_instructions"
    assert result["source_scope"] == ["decision_fixture"]
    assert all(item["source_ref"]["authority_owner"] == "decision_fixture" for item in result["evidence"])
    assert all(item["source_ref"]["workspace_id"] == "w1" for item in result["evidence"])
    assert [item["source_ref"]["object_id"] for item in result["evidence"]] == ["decision"]
    assert result["bundle"]["token_estimate"] <= 40
    for kwargs in ({"limit": MAX_LIMIT + 1}, {"token_budget": MAX_TOKEN_BUDGET + 1}):
        with pytest.raises(ValueError):
            query_context("pump", store=store, **kwargs)


def test_workspace_navigation_returns_only_current_workspace_documents(query_store) -> None:
    store, _ = query_store
    store.rebuild()

    result = query_context("pump", workspace_id="w1", source_scope=("decision_fixture",),
                           allowed_refs=None, limit=8, token_budget=1024, store=store)

    assert result["evidence"]
    assert all(item["source_ref"]["workspace_id"] == "w1" for item in result["evidence"])
    assert {item["source_ref"]["object_id"] for item in result["evidence"]} == {
        "decision", "other_w1",
    }


def test_query_rereads_and_excludes_owner_mutation(query_store) -> None:
    store, documents = query_store
    store.rebuild()
    before = documents["decision"].source_ref
    documents["decision"] = _document("decision", "decision_fixture", "Changed canonical decision", "w1")
    result = query_context("pump", workspace_id="w1", source_scope=("decision_fixture",),
                           allowed_refs=frozenset({("decision_fixture", "record", "decision")}), store=store)
    assert all(item["source_ref"]["content_digest"] != before.content_digest for item in result["evidence"])
    assert not result["evidence"]


def test_repository_hits_hidden_when_master_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()
    ref = SourceRef(authority_owner="repository", object_type="file", object_id="module.py",
                    revision="a" * 40, content_digest=canonical_digest("pump local"))
    doc = IndexDocument(source_ref=ref, text="pump local Ollama adapter")
    store = SQLiteIndexStore(documents=lambda: [doc], repository_root=tmp_path / "not-a-repository")
    store.upsert([doc])
    result = query_context("pump", repository_root=tmp_path / "not-a-repository", store=store)
    assert result["freshness"]["state"] == "unavailable"
    assert not result["evidence"]


def test_repository_hits_hidden_when_index_is_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()
    initialize_database()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("# pump pressure adapter\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "first"], check=True)
    old_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    old_doc = repository_file_documents(repo, old_sha, ["module.py"])[0]
    store = SQLiteIndexStore(documents=lambda: [old_doc], repository_root=repo, master_ref="refs/heads/master")
    store.rebuild()
    (repo / "module.py").write_text("# pump pressure adapter changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "second"], check=True)
    monkeypatch.setattr(store, "ensure_repository_fresh", lambda _sha=None: None)
    result = query_context("pump pressure", source_scope=("repository",), repository_root=repo, store=store)
    assert result["freshness"]["state"] == "behind"
    assert result["freshness"]["indexed_sha"] == old_sha
    assert not result["evidence"]


def test_query_has_no_canonical_write_side_effects(query_store) -> None:
    store, _ = query_store
    store.rebuild()
    initialize_database()
    with open_sqlite_connection() as db:
        before = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    query_context("pump", workspace_id="w1", source_scope=("decision_fixture",), store=store)
    with open_sqlite_connection() as db:
        after = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert after == before


def test_cli_json_shape_is_stable(query_store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from app.modules.ai import retrieval_query

    store, _ = query_store
    store.rebuild()
    monkeypatch.setattr(retrieval_query, "SQLiteIndexStore", lambda **_kwargs: store)
    monkeypatch.setattr("sys.argv", ["retrieval_query", "pump", "--workspace", "w1", "--source-scope", "decision_fixture"])
    retrieval_query.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "retrieval-query.v1"
    assert payload["query"] == "pump"
    assert set(payload) == {"schema_version", "query", "workspace_id", "source_scope", "evidence_role",
                            "freshness", "bundle", "evidence"}
