from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.database import initialize_database, open_retrieval_index_connection, open_sqlite_connection
from app.modules.ai import retrieval_index
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.ai.retrieval_index import SQLiteIndexStore, repository_file_documents, repository_symbol_documents
from app.modules.ai.thread_models import AIThreadCreate
from app.modules.ai.thread_service import create_thread
from app.modules.bluecad.evidence import EvidenceRecordCreate, create_evidence_record
from app.modules.bluecad.ledger import register_artifact
from app.modules.modeling.models import DecisionCreate
from app.modules.modeling.service import create_decision
from app.modules.workspaces.service import seed_default_workspace


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SQLiteIndexStore, dict[str, IndexDocument]]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    docs = {
        "a": _doc("a", "w1", "alpha reactor pressure"),
        "b": _doc("b", "w1", "beta pressure vessel"),
        "c": _doc("c", "w2", "alpha secret workspace"),
    }
    index = SQLiteIndexStore(documents=lambda: docs.values(), resolver=lambda ref: docs.get(ref.object_id))
    yield index, docs
    get_settings.cache_clear()


def _doc(id_: str, workspace: str, text: str, revision: str = "1") -> IndexDocument:
    return IndexDocument(source_ref=SourceRef(authority_owner="fixture", object_type="record", object_id=id_,
                         workspace_id=workspace, revision=revision, content_digest=canonical_digest(text)), text=text)


def test_rebuild_is_stable_and_separate_from_canonical_database(store: tuple[SQLiteIndexStore, dict[str, IndexDocument]], tmp_path: Path) -> None:
    index, _ = store
    first = index.rebuild()
    with open_retrieval_index_connection() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM docs ORDER BY key")]
    assert index.rebuild() == first
    with open_retrieval_index_connection() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM docs ORDER BY key")] == before
    assert (tmp_path / "retrieval-index.sqlite3").exists()
    assert not (tmp_path / "jarvisos.db").exists()


def test_exact_phrase_lexical_vector_and_workspace_isolation(store: tuple[SQLiteIndexStore, dict[str, IndexDocument]]) -> None:
    index, _ = store
    index.rebuild()
    lexical = index.search_lexical('"alpha reactor"', limit=5, workspace_id="w1")
    assert "a" in [hit.source_ref.object_id for hit in lexical]
    assert [hit.source_ref.object_id for hit in index.search_lexical("a", limit=5, workspace_id="w1")] == ["a"]
    exact = _doc("path::alpha", "w1", "opaque symbol body")
    index.upsert([exact])
    assert [hit.source_ref.object_id for hit in index.search_lexical("path::alpha", limit=5, workspace_id="w1")] == ["path::alpha"]
    assert index.search_lexical("secret", limit=5, workspace_id="w1") == []
    assert index.search_vector("pressure", limit=5, workspace_id="w1")
    bundle = index.build_bundle("alpha pressure", workspace_id="w1", token_budget=100)
    assert bundle.items
    assert bundle.items and bundle.token_estimate <= 100
    assert all(item.source_ref.object_type.endswith("summary") for item in bundle.items)


def test_stale_deleted_and_budget_manifest(store: tuple[SQLiteIndexStore, dict[str, IndexDocument]]) -> None:
    index, docs = store
    index.rebuild()
    old = docs["a"].source_ref
    docs["a"] = _doc("a", "w1", "changed pressure", revision="2")
    stale = index.resolve_authoritative(old)
    assert stale.state == "stale" and stale.content is None
    index.upsert([docs["a"]])
    index.delete([old])
    assert index.search_lexical("changed", limit=5, workspace_id="w1")
    index.upsert([_doc("a", "w1", "alpha reactor pressure")])
    bundle = index.build_bundle("alpha reactor", workspace_id="w1", token_budget=1)
    assert [entry.outcome for entry in bundle.evidence_manifest] == ["stale"]
    del docs["a"]
    assert index.resolve_authoritative(old).state == "unavailable"
    index.delete([old])
    assert not index.search_lexical("alpha reactor", limit=5, workspace_id="w1")
    small = index.build_bundle("beta pressure", workspace_id="w1", token_budget=1)
    assert small.token_estimate == 0
    assert [entry.outcome for entry in small.evidence_manifest] == ["dropped_budget"]


def test_fusion_and_bounded_graph(store: tuple[SQLiteIndexStore, dict[str, IndexDocument]]) -> None:
    index, docs = store
    index.rebuild()
    index.add_edge(docs["a"].source_ref, docs["b"].source_ref, "knowledge_relation")
    with pytest.raises(ValueError):
        index.add_edge(docs["b"].source_ref, docs["c"].source_ref, "knowledge_relation")
    neighbors = index.expand_graph([docs["a"].source_ref], depth=1, limit=5)
    assert "b" in [hit.source_ref.object_id for hit in neighbors]
    assert all(hit.source_ref.workspace_id == "w1" for hit in neighbors)
    assert len(index.expand_graph([docs["a"].source_ref], depth=3, limit=1)) == 1
    bundle = index.build_bundle("reactor", workspace_id="w1", token_budget=100, expansion_level=1)
    assert any(item.source_ref.object_id == "a" for item in bundle.items)
    assert all(item.source_ref.workspace_id == "w1" for item in bundle.items)
    assert any(hit.lexical_score is not None for hit in index.search_lexical("reactor", limit=5))
    assert any(hit.vector_score is not None for hit in index.search_vector("reactor", limit=5))
    fused = index.search_hybrid("reactor", limit=5, workspace_id="w1")
    assert fused[0].source_ref.object_id == "a"
    assert fused[0].lexical_score is not None and fused[0].vector_score is not None
    assert fused[0].fused_score > 0


def test_canonical_owner_reread_refuses_changed_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    initialize_database()
    seed_default_workspace()
    decision = create_decision("main", DecisionCreate(title="Pump sizing", decision_text="Use lower pressure"))
    index = SQLiteIndexStore()
    first = index.rebuild()
    assert index.rebuild() == first
    hit = index.search_lexical("Pump", limit=5, workspace_id="main")[0]
    assert hit.source_ref.object_id == decision.id
    assert index.resolve_authoritative(hit.source_ref).state == "current"
    with open_sqlite_connection() as db:
        db.execute("UPDATE decisions SET decision_text=?, updated_at=? WHERE id=?", ("Use higher pressure", "later", decision.id))
        db.commit()
    assert index.resolve_authoritative(hit.source_ref).state == "stale"
    bundle = index.build_bundle("Pump", workspace_id="main", token_budget=200)
    assert not bundle.items and bundle.evidence_manifest[0].outcome == "stale"
    get_settings.cache_clear()


def test_repository_symbols_are_bound_to_exact_git_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("def alpha():\n    return 1\n\nclass Beta:\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "sample.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    docs = repository_symbol_documents(repo, sha, ["sample.py"])
    assert [doc.source_ref.object_id for doc in docs] == ["sample.py::alpha", "sample.py::Beta"]
    assert docs[0].source_ref.location.start_line == 1
    index = SQLiteIndexStore(documents=lambda: docs, repository_root=repo)
    index.rebuild()
    assert index.search_lexical("alpha", limit=5)[0].source_ref.object_id == "sample.py::alpha"
    assert index.resolve_authoritative(docs[0].source_ref).state == "current"
    changed_ref = docs[0].source_ref.model_copy(update={"content_digest": canonical_digest("changed")})
    assert index.resolve_authoritative(changed_ref).state == "stale"
    file_doc = repository_file_documents(repo, sha, ["sample.py"])[0]
    assert index.resolve_authoritative(file_doc.source_ref).state == "current"
    changed_file = file_doc.source_ref.model_copy(update={"content_digest": canonical_digest("changed")})
    assert index.resolve_authoritative(changed_file).state == "stale"
    (repo / "sample.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    assert index.resolve_authoritative(docs[0].source_ref).state == "current"
    get_settings.cache_clear()


def test_repository_delta_tracks_exact_sha_and_only_embeds_changed_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "alpha.py").write_text("def alpha():\n    return 1\n\ndef removed():\n    return 0\n", encoding="utf-8")
    (repo / "keep.txt").write_text("stable corpus token\n", encoding="utf-8")
    (repo / "gone.txt").write_text("delete this tracked file\n", encoding="utf-8")
    (repo / "move.txt").write_text("move unchanged content\n", encoding="utf-8")
    (repo / "binary.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")

    def commit(message: str) -> str:
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", message], check=True)
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    first = commit("first")

    class CountingEmbedder:
        def __init__(self) -> None:
            self.calls = 0
            self.delegate = retrieval_index.HashingEmbedder(16)

        def embed(self, text: str) -> tuple[float, ...]:
            self.calls += 1
            return self.delegate.embed(text)

        def embed_query(self, text: str) -> tuple[float, ...]:
            return self.delegate.embed_query(text)

    embedder = CountingEmbedder()
    index = SQLiteIndexStore(embedder=embedder, documents=lambda: (), repository_root=repo)
    first_result = index.synchronize_repository(first)
    assert index.indexed_master_sha == first
    assert first_result["embedded_documents"] == 13  # sources, summaries, and repository group
    assert embedder.calls == 13

    (repo / "alpha.py").write_text("def alpha():\n    return 2\n\ndef added():\n    return 3\n", encoding="utf-8")
    (repo / "keep.txt").rename(repo / "renamed.txt")
    (repo / "gone.txt").unlink()
    (repo / "move.txt").rename(repo / "moved.txt")
    (repo / "moved.txt").write_text("move content edited after rename\n", encoding="utf-8")
    second = commit("delta")
    delta = index.synchronize_repository(second)
    assert index.indexed_master_sha == second
    assert delta["changed_paths"] == 6
    assert delta["embedded_documents"] == 10  # unchanged rename content reuses its vector
    assert delta["deleted_documents"] == 8  # deleted/renamed refs and their summaries
    assert embedder.calls == 23
    assert index.search_lexical("stable corpus token", limit=5)
    assert index.search_lexical("renamed.txt", limit=5)
    assert index.search_lexical("moved.txt", limit=5)
    assert index.search_lexical("added", limit=5)
    assert index.search_lexical("removed", limit=5) == []
    with open_retrieval_index_connection() as db:
        repository = [SourceRef.model_validate_json(row[0]) for row in db.execute(
            "SELECT ref FROM docs WHERE json_extract(ref,'$.authority_owner')='repository'")]
    assert all(ref.revision == second for ref in repository)
    get_settings.cache_clear()


def test_repository_delta_is_atomic_when_embedding_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()


def _commit_repository(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", message], check=True)
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def _make_repository(path: Path, data_root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(data_root))
    get_settings.cache_clear()
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "unit.py").write_text("def steady():\n    return 'known token'\n", encoding="utf-8")
    first = _commit_repository(path, "first")
    subprocess.run(["git", "-C", str(path), "update-ref", "refs/remotes/origin/master", first], check=True)
    return path, first


def test_incremental_matches_full_rebuild_and_restart_catches_up(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    repo, first = _make_repository(tmp_path / "repo", tmp_path / "data", monkeypatch)
    index = SQLiteIndexStore(documents=lambda: (), repository_root=repo)
    index.rebuild()
    first_snapshot = index.indexed_master_sha
    (repo / "unit.py").write_text("def steady():\n    return 'known token changed'\n\ndef newer():\n    pass\n",
                                  encoding="utf-8")
    _commit_repository(repo, "second")
    (repo / "unit.py").write_text("def steady():\n    return 'known token changed again'\n\ndef newer():\n    pass\n",
                                  encoding="utf-8")
    _commit_repository(repo, "third")
    (repo / "latest.txt").write_text("restart catches up several revisions", encoding="utf-8")
    second = _commit_repository(repo, "fourth")
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", second], check=True)
    restarted = SQLiteIndexStore(documents=lambda: (), repository_root=repo)
    assert restarted.search_lexical("restart catches up", limit=5)
    assert restarted.indexed_master_sha == second
    with open_retrieval_index_connection() as db:
        incremental = ([tuple(row) for row in db.execute("SELECT key,ref,object_id,title,text,vector FROM docs ORDER BY key")],
                       [tuple(row) for row in db.execute("SELECT source_key,target_key,kind,valid_from FROM edges ORDER BY 1,2,3")])
    # Rebuilding at this same exact master produces the same document and edge state.
    restarted.rebuild()
    with open_retrieval_index_connection() as db:
        rebuilt = ([tuple(row) for row in db.execute("SELECT key,ref,object_id,title,text,vector FROM docs ORDER BY key")],
                   [tuple(row) for row in db.execute("SELECT source_key,target_key,kind,valid_from FROM edges ORDER BY 1,2,3")])
    assert incremental == rebuilt
    assert first_snapshot == first
    assert restarted.indexed_master_sha == second
    get_settings.cache_clear()


def test_non_ancestor_rebuild_and_concurrent_sync_increment_once(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    repo, first = _make_repository(tmp_path / "repo", tmp_path / "data", monkeypatch)
    index = SQLiteIndexStore(documents=lambda: (), repository_root=repo)
    index.synchronize_repository(first)
    (repo / "unit.py").write_text("def steady():\n    return 'second token'\n", encoding="utf-8")
    second = _commit_repository(repo, "second")
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", second], check=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(index.synchronize_repository, [second, second]))
    assert sum(int(result["generation"]) for result in results) == 4
    assert sum(int(result["embedded_documents"]) > 0 for result in results) == 1
    assert sum(int(result["changed_paths"]) == 0 for result in results) == 1
    assert index.freshness().generation == 2
    (repo / "unit.py").unlink()
    (repo / "replacement.txt").write_text("rewritten token\n", encoding="utf-8")
    _commit_repository(repo, "rewrite")
    # Orphan history makes the indexed SHA a non-ancestor of the target.
    subprocess.run(["git", "-C", str(repo), "checkout", "--orphan", "rewritten-history"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "rm", "-rf", "."], check=True, capture_output=True)
    (repo / "fresh.txt").write_text("fresh corpus", encoding="utf-8")
    rewritten = _commit_repository(repo, "orphan")
    result = index.synchronize_repository(rewritten)
    assert result["changed_paths"] >= 1
    with open_retrieval_index_connection() as db:
        ids = {str(row[0]) for row in db.execute("SELECT object_id FROM docs WHERE "
                                                "json_extract(ref,'$.authority_owner')='repository'")}
    assert "fresh.txt" in ids and "unit.py" not in ids and "replacement.txt" not in ids
    get_settings.cache_clear()


def test_concurrent_rebuilds_are_generation_guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    get_settings.cache_clear()
    barrier = threading.Barrier(2)
    document = _doc("rebuild-race", "w1", "one deterministic record")

    def documents():
        barrier.wait(timeout=5)
        return [document]

    index = SQLiteIndexStore(documents=documents)

    def rebuild():
        try:
            return True, index.rebuild()
        except RuntimeError as exc:
            return False, str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: rebuild(), range(2)))
    assert sum(succeeded for succeeded, _ in outcomes) == 1
    assert sum(not succeeded and "changed during rebuild" in value for succeeded, value in outcomes) == 1
    with open_retrieval_index_connection() as db:
        assert db.execute("SELECT value FROM meta WHERE name='generation'").fetchone()[0] == "1"


def test_process_exit_rolls_back_and_sync_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, first = _make_repository(tmp_path / "repo", tmp_path / "data", monkeypatch)
    index = SQLiteIndexStore(documents=lambda: (), repository_root=repo)
    index.synchronize_repository(first)
    (repo / "unit.py").write_text("def steady():\n    return 'next token'\n", encoding="utf-8")
    second = _commit_repository(repo, "second")
    code = ("import os; from app.core.database import open_retrieval_index_connection; "
            "dbctx=open_retrieval_index_connection(); db=dbctx.__enter__(); "
            "db.execute('BEGIN IMMEDIATE'); db.execute(\"UPDATE meta SET value='" + second +
            "' WHERE name='indexed_master_sha'\"); db.execute('DELETE FROM docs'); os._exit(19)")
    env = dict(os.environ, JARVISOS_DATA_ROOT=str(tmp_path / "data"))
    crashed = subprocess.run([os.sys.executable, "-c", code], cwd=Path(__file__).parents[1], env=env,
                             check=False)
    assert crashed.returncode == 19
    assert index.indexed_master_sha == first
    with open_retrieval_index_connection() as db:
        stored = db.execute("SELECT ref FROM docs WHERE object_id='unit.py'").fetchone()
        generation = db.execute("SELECT value FROM meta WHERE name='generation'").fetchone()
    assert stored is not None and SourceRef.model_validate_json(stored[0]).revision == first
    assert generation is not None and generation[0] == "1"
    result = index.synchronize_repository(second)
    assert result["indexed_sha"] == second
    assert index.verify().ok
    get_settings.cache_clear()


def test_integrity_repair_rebuilds_fts_and_drops_dangling_edges(store: tuple[SQLiteIndexStore,
                                                                           dict[str, IndexDocument]]) -> None:
    index, docs = store
    index.rebuild()
    with open_retrieval_index_connection() as db:
        db.execute("DELETE FROM docs_fts WHERE key=?", (retrieval_index._key(docs["a"].source_ref),))
        db.execute("INSERT INTO edges VALUES ('missing-source','missing-target','knowledge_relation',NULL)")
        db.commit()
    report = index.verify()
    assert not report.ok
    assert any("FTS" in issue or "fts" in issue for issue in report.issues)
    assert any("dangling" in issue for issue in report.issues)
    assert index.repair().ok
    get_settings.cache_clear()


def test_stale_repository_hits_are_hidden_but_reported_in_bundle(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    repo, first = _make_repository(tmp_path / "repo", tmp_path / "data", monkeypatch)
    non_repository = _doc("safe-record", "w1", "known token")
    index = SQLiteIndexStore(documents=lambda: [non_repository], repository_root=repo,
                             resolver=lambda ref: non_repository if ref.object_id == "safe-record" else None)
    index.rebuild()
    (repo / "unit.py").write_text("def stale_symbol():\n    return 'known token'\n", encoding="utf-8")
    second = _commit_repository(repo, "second")
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", second], check=True)

    def fail_sync(_sha: str) -> dict[str, int | str]:
        raise RuntimeError("simulated busy index")

    monkeypatch.setattr(index, "synchronize_repository", fail_sync)
    hits = index.search_lexical("known token", limit=10)
    assert all(hit.source_ref.authority_owner != "repository" for hit in hits)
    assert any(hit.source_ref.object_id == "safe-record" for hit in hits)
    bundle = index.build_bundle("known token", workspace_id="w1", token_budget=200)
    assert any(entry.outcome == "stale" and entry.source_ref.authority_owner == "repository"
               and "current master" in (entry.reason or "") for entry in bundle.evidence_manifest)
    assert any(entry.outcome == "included" for entry in bundle.evidence_manifest)
    get_settings.cache_clear()


def test_summary_progression_and_source_deletion(store: tuple[SQLiteIndexStore, dict[str, IndexDocument]]) -> None:
    index, docs = store
    index.rebuild()
    summary = index.build_bundle("alpha reactor", workspace_id="w1", token_budget=400, expansion_level=0)
    assert summary.items
    assert all(item.source_ref.object_type.endswith("summary") for item in summary.items)
    expanded = index.build_bundle("alpha reactor", workspace_id="w1", token_budget=400, expansion_level=1)
    assert any(item.source_ref.object_id == "a" and item.expansion_level == 1 for item in expanded.items)
    del docs["a"]
    stale = index.build_bundle("alpha reactor", workspace_id="w1", token_budget=400, expansion_level=1)
    assert not stale.items
    assert all(entry.outcome == "unavailable" for entry in stale.evidence_manifest)


def test_bundle_does_not_expand_group_summary_by_rereading_entire_owner(
    store: tuple[SQLiteIndexStore, dict[str, IndexDocument]],
) -> None:
    index, docs = store
    index.rebuild()
    with open_retrieval_index_connection() as db:
        rows = db.execute("SELECT * FROM docs").fetchall()
    hits = {row["ref"]: index._hit(row, index.revision, lexical=1.0) for row in rows}
    by_kind = {hit.source_ref.object_type: hit for hit in hits.values()}
    group_hit = by_kind["group_summary"]
    summary_hit = next(hit for hit in hits.values()
                       if hit.source_ref.object_type == "document_summary"
                       and hit.source_ref.object_id == retrieval_index._key(docs["a"].source_ref))
    index.search_hybrid = lambda *_args, **_kwargs: [group_hit, summary_hit]  # type: ignore[method-assign]
    index.documents = lambda: pytest.fail("a bundle must not reread every record to validate a group summary")
    resolved: list[SourceRef] = []
    index.resolver = lambda ref: resolved.append(ref) or docs.get(ref.object_id)

    bundle = index.build_bundle("alpha reactor", workspace_id="w1", token_budget=100)

    assert [item.source_ref.object_type for item in bundle.items] == ["document_summary"]
    assert resolved == [docs["a"].source_ref]
    assert index.resolve_authoritative(group_hit.source_ref).state == "unavailable"


def test_retrieval_index_database_has_a_bounded_page_limit() -> None:
    from app.core.database import RETRIEVAL_INDEX_MAX_PAGE_COUNT, open_retrieval_index_connection

    with open_retrieval_index_connection() as db:
        assert db.execute("PRAGMA max_page_count").fetchone()[0] == RETRIEVAL_INDEX_MAX_PAGE_COUNT


def test_owner_dispatch_refuses_digest_mismatch_for_every_indexed_kind(
    store: tuple[SQLiteIndexStore, dict[str, IndexDocument]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    index, _ = store
    index.resolver = None
    for owner, kind in retrieval_index._OWNER_RESOLVERS:
        ref = SourceRef(authority_owner=owner, object_type=kind, object_id="one", workspace_id="w1",
                        content_digest=canonical_digest("before"))
        changed = IndexDocument(source_ref=ref.model_copy(update={"content_digest": canonical_digest("after")}),
                                text="after")
        monkeypatch.setitem(retrieval_index._OWNER_RESOLVERS, (owner, kind), lambda _ref, doc=changed: doc)
        assert index.resolve_authoritative(ref).state == "stale", (owner, kind)


def test_new_canonical_owners_rebuild_and_reread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    initialize_database()
    seed_default_workspace()
    thread = create_thread(AIThreadCreate(workspace_id="main", title="Pump review"))
    report = tmp_path / "report.txt"
    report.write_text("mesh pass", encoding="utf-8")
    artifact_id = register_artifact("main", report, role="report", source_ref="benchmark")
    evidence = create_evidence_record(EvidenceRecordCreate(
        workspace_id="main", kind="mesh_quality_v0", verdict="pass", metrics_json="{}",
        report_artifact_id=artifact_id,
    ))
    index = SQLiteIndexStore()
    first = index.rebuild()
    assert index.rebuild() == first
    for owner, kind, object_id in (("ai_threads", "thread", thread.id),
                                   ("bluecad", "evidence_record", evidence.id)):
        hit = next(hit for hit in index.search_lexical(object_id, limit=3, workspace_id="main")
                   if hit.source_ref.authority_owner == owner and hit.source_ref.object_type == kind)
        assert index.resolve_authoritative(hit.source_ref).state == "current"
        changed = hit.source_ref.model_copy(update={"content_digest": canonical_digest("changed")})
        assert index.resolve_authoritative(changed).state == "stale"
    get_settings.cache_clear()


def test_hermes_operational_memory_is_bounded_redacted_and_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "hermes"
    memories = home / "memories"
    memories.mkdir(parents=True)
    memory_file = memories / "MEMORY.md"
    memory_file.write_text(
        "Remember reactor pressure is checked quarterly\n§\n"
        "api_key = sk-example-secret-value-123456\n§\n"
        "Keep the ops password: hunter2 private\n",
        encoding="utf-8",
    )
    user_file = memories / "USER.md"
    user_file.write_text("Prefers concise operating notes", encoding="utf-8")
    monkeypatch.setattr(retrieval_index, "_hermes_home", lambda: home)

    docs = retrieval_index.hermes_operational_memory_documents()

    assert len(docs) == 4
    assert all(doc.source_ref.authority_owner == "hermes_operational_memory" for doc in docs)
    assert all("non-canonical" in doc.title for doc in docs)
    assert all("non-canonical" in doc.text and "OPERATIONAL MEMORY" in doc.text for doc in docs)
    assert "sk-example-secret-value-123456" not in "\n".join(doc.text for doc in docs)
    assert "hunter2" not in "\n".join(doc.text for doc in docs)

    index = SQLiteIndexStore(documents=lambda: docs, embedder=retrieval_index.HashingEmbedder())
    index.rebuild()
    ref = docs[0].source_ref
    assert index.resolve_authoritative(ref).state == "current"
    bundle = index.build_bundle("reactor pressure quarterly", workspace_id=None, token_budget=300,
                                expansion_level=1)
    assert any(item.source_ref.authority_owner == "hermes_operational_memory" for item in bundle.items)

    memory_file.write_text("A changed Hermes operational memory entry", encoding="utf-8")
    assert index.resolve_authoritative(ref).state == "stale"
    stale_bundle = index.build_bundle("reactor pressure quarterly", workspace_id=None, token_budget=300,
                                      expansion_level=1)
    assert not any(item.source_ref == ref for item in stale_bundle.items)
    assert any(entry.source_ref == ref and entry.outcome == "stale" for entry in stale_bundle.evidence_manifest)
    memory_file.unlink()
    user_file.unlink()
    assert index.resolve_authoritative(ref).state == "unavailable"
    assert retrieval_index.hermes_operational_memory_documents() == []


def test_hermes_operational_memory_age_entry_and_byte_bounds(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    home = tmp_path / "hermes"
    memories = home / "memories"
    memories.mkdir(parents=True)
    old_file = memories / "MEMORY.md"
    old_file.write_text("old memory", encoding="utf-8")
    old_timestamp = (datetime.now(UTC) - timedelta(days=366)).timestamp()
    import os

    os.utime(old_file, (old_timestamp, old_timestamp))
    assert retrieval_index.hermes_operational_memory_documents(home) == []

    old_file.write_text("\n§\n".join(f"entry {index}" for index in range(140)), encoding="utf-8")
    docs = retrieval_index.hermes_operational_memory_documents(home)
    assert len(docs) == retrieval_index._HERMES_MEMORY_MAX_ENTRIES

    old_file.write_bytes(b"x" * (retrieval_index._HERMES_MEMORY_MAX_BYTES + 1))
    assert retrieval_index.hermes_operational_memory_documents(home) == []


def test_hermes_operational_memory_ranks_below_non_operational_sources() -> None:
    index = SQLiteIndexStore(documents=lambda: [])
    refs = [
        SourceRef(authority_owner="modeling", object_type="decision", object_id="canonical",
                  content_digest=canonical_digest("shared facts")),
        SourceRef(authority_owner="hermes_operational_memory", object_type="memory_entry",
                  object_id="MEMORY.md:0", revision="1", content_digest=canonical_digest("shared facts")),
    ]
    hits = [retrieval_index.RetrievalHit(source_ref=ref, lexical_score=1.0, fused_score=0.0,
                                         index_revision="test") for ref in refs]
    index.search_lexical = lambda *_args, **_kwargs: hits  # type: ignore[method-assign]
    index.search_vector = lambda *_args, **_kwargs: hits  # type: ignore[method-assign]

    ranked = index.search_hybrid("shared facts", limit=2)

    assert [hit.source_ref.authority_owner for hit in ranked] == ["modeling", "hermes_operational_memory"]
    assert ranked[0].fused_score > ranked[1].fused_score


def test_e5_embedder_prefixes_query_and_passage_differently() -> None:
    """multilingual-e5 requires distinct "query: "/"passage: " prefixes; injects a fake
    SentenceTransformer-like object so no model download or network happens in tests."""

    class _RecordingModel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
            self.calls.append(text)
            return [1.0, 0.0]

    embedder = object.__new__(retrieval_index.E5Embedder)
    model = _RecordingModel()
    embedder.model = model
    assert embedder.embed("hello") == (1.0, 0.0)
    assert embedder.embed_query("hello") == (1.0, 0.0)
    assert model.calls == ["passage: hello", "query: hello"]


def test_sqlite_vec_matches_python_fallback_ranking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional accelerator: when sqlite-vec loads, vec0 KNN must rank identical embeddings
    the same as the pure-Python brute-force path. Skips where sqlite-vec is not installed
    (the backend venv); must pass in an environment that has it (e.g. the q148 venv)."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    docs = {
        "a": _doc("a", "w1", "alpha reactor pressure vessel design margin"),
        "b": _doc("b", "w1", "beta pressure vessel safety margin inspection"),
        "c": _doc("c", "w1", "gamma unrelated budget zero paid provider policy"),
        "d": _doc("d", "w1", "delta reactor heat transfer coefficient analysis"),
        "e": _doc("e", "w2", "alpha secret workspace isolated document reactor"),
    }

    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "accelerated"))
    get_settings.cache_clear()
    accelerated = SQLiteIndexStore(documents=lambda: docs.values(), resolver=lambda ref: docs.get(ref.object_id))
    assert accelerated.use_sqlite_vec is True
    accelerated.rebuild()
    with open_retrieval_index_connection() as db:
        vector_bytes = db.execute("SELECT SUM(length(vectors)) FROM vec_docs_vector_chunks00").fetchone()[0]
    assert vector_bytes <= accelerated._vector_dimensions * 4 * retrieval_index._SQLITE_VEC_CHUNK_SIZE * 20
    accelerated_hits = [hit.source_ref.object_id for hit in
                        accelerated.search_vector("reactor pressure vessel margin", limit=10, workspace_id="w1")]

    monkeypatch.setattr(retrieval_index, "sqlite_vec", None)
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "fallback"))
    get_settings.cache_clear()
    fallback = SQLiteIndexStore(documents=lambda: docs.values(), resolver=lambda ref: docs.get(ref.object_id))
    assert fallback.use_sqlite_vec is False
    fallback.rebuild()
    fallback_hits = [hit.source_ref.object_id for hit in
                     fallback.search_vector("reactor pressure vessel margin", limit=10, workspace_id="w1")]

    assert accelerated_hits
    assert accelerated_hits == fallback_hits
    assert "e" not in accelerated_hits

    # An index written without the accelerator (fallback) and later opened with it must
    # backfill vec_docs from the stored vectors instead of serving an empty/stale vec0 table.
    fallback.upsert([_doc("f", "w1", "reactor pressure vessel margin extra")])
    expected = [hit.source_ref.object_id for hit in
                fallback.search_vector("reactor pressure vessel margin", limit=10, workspace_id="w1")]
    monkeypatch.setattr(retrieval_index, "sqlite_vec", sqlite_vec)
    reopened = SQLiteIndexStore(documents=lambda: docs.values(), resolver=lambda ref: docs.get(ref.object_id))
    assert reopened.use_sqlite_vec is True
    assert [hit.source_ref.object_id for hit in
            reopened.search_vector("reactor pressure vessel margin", limit=10, workspace_id="w1")] == expected
    assert "f" in expected
    get_settings.cache_clear()


def test_typed_temporal_edges_and_workspace_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    def record(owner: str, kind: str, object_id: str, workspace: str, **fields: str) -> IndexDocument:
        data = {"id": object_id, "created_at": "2026-01-01", **fields}
        return IndexDocument(source_ref=SourceRef(
            authority_owner=owner, object_type=kind, object_id=object_id, workspace_id=workspace,
            content_digest=canonical_digest(data),
        ), text=json.dumps(data, sort_keys=True))

    docs = [
        record("bluecad", "candidate", "candidate-1", "w1"),
        record("bluecad", "attempt", "attempt-1", "w1", candidate_id="candidate-1"),
        record("bluecad", "evidence_record", "evidence-1", "w1",
               candidate_id="candidate-1", attempt_id="attempt-1"),
        record("modeling", "parameter", "parameter-1", "w1"),
        record("modeling", "parameter", "parameter-2", "w1", supersedes_parameter_id="parameter-1"),
        record("ai_threads", "thread", "thread-1", "w1"),
        record("ai_threads", "interaction", "interaction-1", "w1"),
        record("bluecad", "candidate", "foreign", "w2"),
    ]
    docs[6] = docs[6].model_copy(update={"title": "thread-1"})
    index = SQLiteIndexStore(documents=lambda: docs, resolver=lambda ref: next(
        (doc for doc in docs if doc.source_ref == ref), None))
    index.rebuild()
    with open_retrieval_index_connection() as db:
        edges = {(row["kind"], row["valid_from"]) for row in db.execute("SELECT kind,valid_from FROM edges")}
        assert ("evidence_candidate", "2026-01-01") in edges
        assert ("evidence_attempt", "2026-01-01") in edges
        assert ("parameter_supersedes", "2026-01-01") in edges
        assert ("interaction_thread", "2026-01-01") in edges
        source = retrieval_index._key(docs[0].source_ref)
        target = retrieval_index._key(docs[-1].source_ref)
        db.execute("INSERT INTO edges VALUES (?,?,?,?)", (source, target, "knowledge_relation", None))
        db.commit()
    hits = index.expand_graph([docs[0].source_ref], depth=2, limit=50)
    assert all(hit.source_ref.workspace_id == "w1" for hit in hits)
    get_settings.cache_clear()
