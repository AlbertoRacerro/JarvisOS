"""Comprehensive offline tests for Spec 365: Local CPU embedder and cross-encoder reranker."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.core.config import get_settings
from app.core.database import initialize_database
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.ai.retrieval_bench import evaluate_retrieval_request
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.ai.retrieval_index import (
    FastEmbedEmbedder,
    FastEmbedReranker,
    HashingEmbedder,
    LoopbackHttpEmbedder,
    SQLiteIndexStore,
    _is_fastembed_model_cached,
    default_embedder,
    default_reranker,
)
from app.modules.ai.retrieval_owners import owner_catch_up
from app.modules.modeling.models import DecisionCreate
from app.modules.modeling.service import create_decision
from app.modules.workspaces.service import seed_default_workspace


class StubReranker:
    def __init__(self, scores: dict[str, float], identity: str = "stub-reranker-v1") -> None:
        self.scores = scores
        self.identity = identity

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [self.scores.get(p, 0.0) for p in passages]


# ---------------------------------------------------------------------------
# Criterion 1: default_embedder and default_reranker factories & fallback
# ---------------------------------------------------------------------------


def test_default_embedder_auto_fallback_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    # auto mode without fastembed or without model cache -> falls back to HashingEmbedder
    monkeypatch.setenv("JARVISOS_RETRIEVAL_EMBEDDER", "auto")
    embedder = default_embedder()
    assert isinstance(embedder, HashingEmbedder)
    assert embedder.identity == "hashing-v1:128"

    # explicit hashing override
    monkeypatch.setenv("JARVISOS_RETRIEVAL_EMBEDDER", "hashing")
    assert isinstance(default_embedder(), HashingEmbedder)

    # explicit loopback_http override
    monkeypatch.setenv("JARVISOS_RETRIEVAL_EMBEDDER", "loopback_http")
    monkeypatch.setenv("JARVISOS_LOOPBACK_EMBEDDER_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("JARVISOS_LOOPBACK_EMBEDDER_MODEL", "bge-small")
    http_emb = default_embedder()
    assert isinstance(http_emb, LoopbackHttpEmbedder)
    assert http_emb.model_name == "bge-small"
    assert "127.0.0.1:11434" in http_emb.base_url

    # invalid mode raises ValueError
    monkeypatch.setenv("JARVISOS_RETRIEVAL_EMBEDDER", "unknown_mode")
    with pytest.raises(ValueError, match="unsupported retrieval embedder"):
        default_embedder()


def test_default_reranker_auto_fallback_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    # auto mode without fastembed or without model cache -> None
    monkeypatch.setenv("JARVISOS_RETRIEVAL_RERANKER", "auto")
    assert default_reranker() is None

    # explicit none override
    monkeypatch.setenv("JARVISOS_RETRIEVAL_RERANKER", "none")
    assert default_reranker() is None

    # invalid mode raises ValueError
    monkeypatch.setenv("JARVISOS_RETRIEVAL_RERANKER", "unknown_reranker")
    with pytest.raises(ValueError, match="unsupported retrieval reranker"):
        default_reranker()


def test_fastembed_cache_check_helper(tmp_path: Path) -> None:
    cache_dir = tmp_path / "models" / "fastembed"
    assert not _is_fastembed_model_cached(cache_dir, "BAAI/bge-small-en-v1.5")

    # Direct directory format
    model_dir = cache_dir / "BAAI/bge-small-en-v1.5"
    model_dir.mkdir(parents=True)
    (model_dir / "model.onnx").write_bytes(b"dummy onnx")
    assert _is_fastembed_model_cached(cache_dir, "BAAI/bge-small-en-v1.5")

    # HF hub format
    hf_dir = cache_dir / "models--Xenova--ms-marco-MiniLM-L-6-v2"
    hf_dir.mkdir(parents=True)
    (hf_dir / "snapshots").mkdir()
    (hf_dir / "snapshots" / "dummy.onnx").write_bytes(b"dummy")
    assert _is_fastembed_model_cached(cache_dir, "Xenova/ms-marco-MiniLM-L-6-v2")


def test_fastembed_lazy_import_and_adapter_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without fastembed installed, instantiating FastEmbedEmbedder raises ImportError
    if "fastembed" not in sys.modules:
        with pytest.raises(ImportError):
            FastEmbedEmbedder()
        with pytest.raises(ImportError):
            FastEmbedReranker()

    # Now simulate fastembed module
    mock_fastembed = types.ModuleType("fastembed")
    mock_cross = types.ModuleType("fastembed.rerank.cross_encoder")

    class FakeTextEmbedding:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3] for _ in texts]

        def passage_embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.4, 0.5, 0.6] for _ in texts]

        def query_embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.7, 0.8, 0.9] for _ in texts]

    class FakeTextCrossEncoder:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
            return [0.85 - i * 0.1 for i in range(len(documents))]

    mock_fastembed.TextEmbedding = FakeTextEmbedding  # type: ignore[attr-defined]
    mock_cross.TextCrossEncoder = FakeTextCrossEncoder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", mock_fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", types.ModuleType("fastembed.rerank"))
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", mock_cross)

    embedder = FastEmbedEmbedder(cache_dir=tmp_path)
    assert embedder.identity == "fastembed:BAAI/bge-small-en-v1.5"
    assert embedder.embed("passage") == (0.4, 0.5, 0.6)
    assert embedder.embed_query("query") == (0.7, 0.8, 0.9)

    reranker = FastEmbedReranker(cache_dir=tmp_path)
    assert reranker.identity == "fastembed-reranker:Xenova/ms-marco-MiniLM-L-6-v2"
    scores = reranker.score("test query", ["p1", "p2"])
    assert scores == [0.85, 0.75]

    (tmp_path / "models--BAAI--bge-small-en-v1.5").mkdir()
    (tmp_path / "models--BAAI--bge-small-en-v1.5" / "model.onnx").write_bytes(b"fixture")
    (tmp_path / "models--Xenova--ms-marco-MiniLM-L-6-v2").mkdir()
    (tmp_path / "models--Xenova--ms-marco-MiniLM-L-6-v2" / "model.onnx").write_bytes(b"fixture")
    monkeypatch.setenv("JARVISOS_FASTEMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("JARVISOS_RETRIEVAL_EMBEDDER", "auto")
    monkeypatch.setenv("JARVISOS_RETRIEVAL_RERANKER", "auto")
    assert isinstance(default_embedder(), FastEmbedEmbedder)
    assert isinstance(default_reranker(), FastEmbedReranker)
    monkeypatch.setenv("JARVISOS_RETRIEVAL_EMBEDDER", "fastembed")
    monkeypatch.setenv("JARVISOS_RETRIEVAL_RERANKER", "fastembed")
    assert default_embedder().cache_dir == tmp_path
    assert default_reranker().cache_dir == tmp_path


# ---------------------------------------------------------------------------
# Criterion 2: LoopbackHttpEmbedder security validation & responses
# ---------------------------------------------------------------------------


def test_loopback_http_embedder_rejects_non_loopback() -> None:
    # SSRF / non-loopback protection: reject external / lan / cloud metadata IPs
    bad_urls = [
        "http://example.com/v1",
        "https://api.openai.com/v1",
        "http://192.168.1.50:8080/v1",
        "http://10.0.0.1:8080/v1",
        "http://172.16.0.1:8080/v1",
        "http://0.0.0.0:8000/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1.nip.io:8080/v1",
        "http://user:secret@127.0.0.1:8080/v1",
        "ftp://127.0.0.1:8080/v1",
    ]
    for url in bad_urls:
        with pytest.raises(ValueError, match="LoopbackHttpEmbedder"):
            LoopbackHttpEmbedder(base_url=url)

    # Allowed loopback URLs
    good_urls = [
        "http://127.0.0.1:8080/v1",
        "http://localhost:11434/v1",
        "http://[::1]:8080/v1",
    ]
    for url in good_urls:
        client = LoopbackHttpEmbedder(base_url=url)
        assert client.base_url == url.rstrip("/")


def test_loopback_http_embedder_mock_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LoopbackHttpEmbedder(base_url="http://127.0.0.1:8080/v1", model_name="local-embed")
    assert client.identity == "loopback_http:local-embed@http://127.0.0.1:8080/v1"

    response_payload = {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "embedding": [0.123, 0.456, 0.789],
                "index": 0,
            }
        ],
        "model": "local-embed",
    }

    class MockHttpResponse:
        def __enter__(self) -> MockHttpResponse:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(response_payload).encode("utf-8")

    def fake_open(req: Any, timeout: float = 30.0) -> MockHttpResponse:
        assert req.get_header("Content-type") == "application/json"
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "local-embed"
        assert body["input"] in {"passage to embed", "query to embed"}
        return MockHttpResponse()

    import urllib.request

    class MockOpener:
        open = staticmethod(fake_open)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: MockOpener())

    vec = client.embed("passage to embed")
    assert vec == (0.123, 0.456, 0.789)
    q_vec = client.embed_query("query to embed")
    assert q_vec == (0.123, 0.456, 0.789)

    local_client = LoopbackHttpEmbedder(base_url="http://localhost:8080/v1", model_name="local-embed")
    def assert_pinned_url(req: Any, timeout: float = 30.0) -> MockHttpResponse:
        assert req.full_url == "http://127.0.0.1:8080/v1/embeddings"
        return MockHttpResponse()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: type("Opener", (), {"open": staticmethod(assert_pinned_url)})())
    assert local_client.embed("passage to embed") == (0.123, 0.456, 0.789)


def test_loopback_http_embedder_refuses_redirect_without_external_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import BytesIO
    from urllib.request import HTTPHandler
    from urllib.response import addinfourl

    opened: list[str] = []

    def fake_http_open(handler: HTTPHandler, req: Any) -> Any:
        opened.append(req.full_url)
        assert req.full_url == "http://127.0.0.1:8080/v1/embeddings", "external URL was opened"
        response = addinfourl(
            BytesIO(b""), {"Location": "http://example.invalid/embeddings"},
            req.full_url, code=302,
        )
        response.msg = "Found"
        return response

    monkeypatch.setattr(HTTPHandler, "http_open", fake_http_open)
    embedder = LoopbackHttpEmbedder(base_url="http://127.0.0.1:8080/v1")
    with pytest.raises(RuntimeError, match="Loopback embedding request failed"):
        embedder.embed("local request")
    assert opened == ["http://127.0.0.1:8080/v1/embeddings"]


# ---------------------------------------------------------------------------
# Criterion 3: search_hybrid with Reranker, Hermes penalty & workspace isolation
# ---------------------------------------------------------------------------


def test_search_hybrid_promotes_true_passage_and_populates_rerank_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    initialize_database()

    doc_irr = IndexDocument(
        source_ref=SourceRef(
            authority_owner="modeling", object_type="decision", object_id="doc-irr",
            workspace_id="ws1", content_digest=canonical_digest("irrelevant"),
        ),
        title="Low pressure pump",
        text="Low pressure water circulation pump without valve",
    )
    doc_fair = IndexDocument(
        source_ref=SourceRef(
            authority_owner="modeling", object_type="decision", object_id="doc-fair",
            workspace_id="ws1", content_digest=canonical_digest("fair"),
        ),
        title="Centrifugal pumps overview",
        text="General pump sizing equations and head flow characteristics",
    )
    doc_best = IndexDocument(
        source_ref=SourceRef(
            authority_owner="modeling", object_type="decision", object_id="doc-best",
            workspace_id="ws1", content_digest=canonical_digest("best"),
        ),
        title="Ultra high pressure reactor titanium pump",
        text="Specific titanium alloy high pressure pump sizing and cavitation margins",
    )

    stub_scores = {
        doc_irr.text: 0.05,
        doc_fair.text: 0.35,
        doc_best.text: 0.98,
    }
    reranker = StubReranker(stub_scores)

    store = SQLiteIndexStore(
        db_path=tmp_path / "test_rerank.sqlite",
        embedder=HashingEmbedder(),
        reranker=reranker,
        documents=lambda: [doc_irr, doc_fair, doc_best],
    )
    store.rebuild()

    # With reranker enabled (default rerank=True): doc_best is ranked #1 with rerank_score populated
    hits = [
        h for h in store.search_hybrid("high pressure pump", limit=10, workspace_id="ws1", rerank=True)
        if h.source_ref.authority_owner == "modeling"
    ]
    assert len(hits) == 3
    assert hits[0].source_ref.object_id == "doc-best"
    assert hits[0].rerank_score == pytest.approx(0.98)
    assert hits[1].source_ref.object_id == "doc-fair"
    assert hits[1].rerank_score == pytest.approx(0.35)
    assert hits[2].source_ref.object_id == "doc-irr"
    assert hits[2].rerank_score == pytest.approx(0.05)

    # With rerank=False, rerank_score is None and RRF order is preserved
    unreranked = store.search_hybrid("high pressure pump", limit=3, workspace_id="ws1", rerank=False)
    assert all(hit.rerank_score is None for hit in unreranked)


def test_search_hybrid_preserves_hermes_penalty_and_workspace_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    initialize_database()

    doc_canonical = IndexDocument(
        source_ref=SourceRef(
            authority_owner="modeling", object_type="decision", object_id="canonical-1",
            workspace_id="ws-alpha", content_digest=canonical_digest("canonical text"),
        ),
        title="Reactor design parameter",
        text="Reactor core parameter specifications",
    )
    doc_hermes = IndexDocument(
        source_ref=SourceRef(
            authority_owner="hermes_operational_memory", object_type="memory_entry", object_id="MEMORY.md:0",
            workspace_id=None, content_digest=canonical_digest("hermes text"),
        ),
        title="Operational memory Hermes",
        text="Reactor core parameter operational note",
    )
    doc_other_ws = IndexDocument(
        source_ref=SourceRef(
            authority_owner="modeling", object_type="decision", object_id="canonical-other",
            workspace_id="ws-beta", content_digest=canonical_digest("beta text"),
        ),
        title="Beta reactor parameter",
        text="Beta parameter specification",
    )

    # Even when the reranker scores the Hermes memory equally high (0.90) to the canonical doc (0.90),
    # the canonical doc must outrank Hermes memory due to the 0.2 priority penalty.
    stub_scores = {
        doc_canonical.text: 0.90,
        doc_hermes.text: 0.90,
        doc_other_ws.text: 0.99,
    }
    reranker = StubReranker(stub_scores)

    store = SQLiteIndexStore(
        db_path=tmp_path / "test_hermes.sqlite",
        embedder=HashingEmbedder(),
        reranker=reranker,
        documents=lambda: [doc_canonical, doc_hermes, doc_other_ws],
    )
    store.rebuild()

    # 1. Workspace isolation: ws-alpha search must exclude ws-beta
    hits_alpha = [
        h for h in store.search_hybrid("reactor parameter", limit=10, workspace_id="ws-alpha", rerank=True)
        if h.source_ref.authority_owner != "retrieval"
    ]
    hit_ids = [h.source_ref.object_id for h in hits_alpha]
    assert "canonical-other" not in hit_ids
    assert "canonical-1" in hit_ids

    # 2. Hermes penalty: when search is unscoped (workspace_id=None), Canonical must outrank Hermes
    hits_all = [
        h for h in store.search_hybrid("reactor parameter", limit=10, workspace_id=None, rerank=True)
        if h.source_ref.authority_owner != "retrieval"
    ]
    idx_canonical = next(i for i, h in enumerate(hits_all) if h.source_ref.object_id == "canonical-1")
    idx_hermes = next(i for i, h in enumerate(hits_all) if h.source_ref.object_id == "MEMORY.md:0")
    assert idx_canonical < idx_hermes
    assert hits_all[idx_canonical].fused_score == pytest.approx(0.90)
    assert hits_all[idx_hermes].fused_score == pytest.approx(0.90 * 0.2)


# ---------------------------------------------------------------------------
# Criterion 4: owner_catch_up re-embedding on embedder identity mismatch
# ---------------------------------------------------------------------------


def test_owner_catch_up_reembeds_documents_when_embedder_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    initialize_database()
    seed_default_workspace()

    d1 = create_decision("main", DecisionCreate(title="Decision 1", decision_text="Decision details one"))
    create_decision("main", DecisionCreate(title="Decision 2", decision_text="Decision details two"))

    initial_embedder = HashingEmbedder(dimensions=128)
    store = SQLiteIndexStore(embedder=initial_embedder)

    # Initial sync
    res1 = owner_catch_up(store)
    assert res1["modeling"]["changed"] >= 2
    assert store.has_compatible_embedder()

    # Second sync without changes: fingerprints match and embedder matches -> 0 embedded
    res2 = owner_catch_up(store)
    assert res2["modeling"]["embedded"] == 0

    # Now change active embedder to a 32-dim hashing embedder (embedder_identity changes)
    new_embedder = HashingEmbedder(dimensions=32)
    store.embedder = new_embedder
    assert not store.has_compatible_embedder()

    # owner_catch_up MUST NOT short-circuit even though owner fingerprints are unchanged
    res3 = owner_catch_up(store)
    assert res3["modeling"]["embedded"] >= 2
    assert store.has_compatible_embedder()

    # Verify that vectors in docs table now have 32 dimensions
    with store._open_connection() as db:
        row = db.execute("SELECT vector FROM docs WHERE object_id=?", (str(d1.id),)).fetchone()
        assert row is not None
        vec = json.loads(row[0])
        assert len(vec) == 32


def test_owner_catch_up_never_writes_for_a_separate_or_read_only_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    initialize_database()
    seed_default_workspace()
    create_decision("main", DecisionCreate(title="Decision 1", decision_text="Decision details one"))
    default_store = SQLiteIndexStore(embedder=HashingEmbedder(dimensions=128))
    owner_catch_up(default_store)
    other = SQLiteIndexStore(embedder=HashingEmbedder(dimensions=128), documents=lambda: (),
                             db_path=tmp_path / "other.db")
    other.upsert([IndexDocument(
        source_ref=SourceRef(authority_owner="modeling", object_type="decision", object_id="other-1",
                             workspace_id="main", content_digest=canonical_digest("other")),
        title="Other", text="other store text")])

    # Owner data changes while a separate store with another embedder catches up.
    create_decision("main", DecisionCreate(title="Decision 2", decision_text="Decision details two"))
    other.embedder = HashingEmbedder(dimensions=32)
    changes = owner_catch_up(other)
    read_only = SQLiteIndexStore(embedder=HashingEmbedder(dimensions=16), documents=lambda: (),
                                 db_path=tmp_path / "other.db", read_only=True)
    owner_catch_up(read_only)

    assert all(item == {"changed": 0, "deleted": 0, "embedded": 0} for item in changes.values())
    assert default_store.has_compatible_embedder()
    with default_store._open_connection() as db:
        dims = {len(json.loads(row[0])) for row in db.execute("SELECT vector FROM docs")}
    assert dims == {128}
    with other._open_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1
        assert len(json.loads(db.execute("SELECT vector FROM docs").fetchone()[0])) == 128


def test_search_hybrid_with_reranker_keeps_limits_above_the_rerank_pool() -> None:
    docs = [IndexDocument(
        source_ref=SourceRef(authority_owner="modeling", object_type="decision", object_id=f"d{i:03d}",
                             workspace_id="ws1", content_digest=canonical_digest(f"pump {i}")),
        title=f"Pump {i}", text=f"pump sizing note {i}") for i in range(90)]
    plain = SQLiteIndexStore(embedder=HashingEmbedder(), reranker=None, documents=lambda: ())
    plain.upsert(docs)
    reranked = SQLiteIndexStore(embedder=HashingEmbedder(), reranker=StubReranker({}), documents=lambda: ())
    assert len(plain.search_hybrid("pump", limit=80)) == 80
    assert len(reranked.search_hybrid("pump", limit=80)) == 80


# ---------------------------------------------------------------------------
# Criterion 5: retrieval.eval recording embedder/reranker & rank improvement
# ---------------------------------------------------------------------------


def test_retrieval_eval_records_embedder_and_reranker_identities(tmp_path: Path) -> None:
    req = {
        "mode": "hybrid",
        "embedder": "hashing",
        "reranker": "none",
        "documents": [
            {"id": "doc1", "title": "Doc One", "text": "alpha text"},
            {"id": "doc2", "title": "Doc Two", "text": "beta text"},
        ],
        "questions": [
            {"id": "q1", "query": "alpha", "expected_ids": ["doc1"]},
        ],
    }

    result, verdict, details = evaluate_retrieval_request(req, root=tmp_path, out_dir=tmp_path)
    assert result.status == "succeeded"
    assert verdict == "usable"
    assert details["embedder_identity"] == "hashing-v1:128"
    assert details["reranker_identity"] == "none"
    assert len(details["questions"]) == 1


def test_retrieval_eval_reranker_improves_ranking(tmp_path: Path) -> None:
    # Construct a case where RRF alone puts doc-distractor at rank 1, but reranker promotes doc-target
    documents = [
        {"id": "doc-target", "title": "Specific Engineering Standard", "text": "Cryogenic liquid nitrogen valve specification"},
        {"id": "doc-distractor", "title": "Valve Overview", "text": "General valve overview with nitrogen mention"},
    ]
    # Query has keywords matching distractor well
    query = "valve nitrogen specification"

    # Base run without reranker
    req_no_rerank = {
        "mode": "hybrid",
        "embedder": "hashing",
        "reranker": "none",
        "documents": documents,
        "questions": [{"id": "q", "query": query, "expected_ids": ["doc-target"]}],
    }
    _, _, details_no_rerank = evaluate_retrieval_request(req_no_rerank, root=tmp_path, out_dir=tmp_path)
    rank_before = details_no_rerank["questions"][0]["first_matching_rank"]
    assert rank_before is None or rank_before >= 1

    # Run with a stub reranker that gives doc-target a top score
    stub_reranker = StubReranker(
        {
            "Cryogenic liquid nitrogen valve specification": 0.99,
            "General valve overview with nitrogen mention": 0.10,
        },
        identity="custom-precision-reranker",
    )
    req_rerank = {
        "mode": "hybrid",
        "embedder": "hashing",
        "reranker": stub_reranker,
        "documents": documents,
        "questions": [{"id": "q", "query": query, "expected_ids": ["doc-target"]}],
    }
    _, _, details_rerank = evaluate_retrieval_request(req_rerank, root=tmp_path, out_dir=tmp_path)
    assert details_rerank["reranker_identity"] == "custom-precision-reranker"
    rank_after = details_rerank["questions"][0]["first_matching_rank"]
    assert rank_after == 1
