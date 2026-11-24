# 365 — RETRIEVAL-EMBEDDER-RERANKER-1: Local CPU embedder and cross-encoder reranker for project memory retrieval

Status: ready
Depends on: 291, 292

## Goal

Replace the placeholder `HashingEmbedder` default in the project memory retrieval pipeline with a real local CPU embedder (`FastEmbedEmbedder` via Apache-2.0 ONNX `fastembed`, plus optional loopback `/v1/embeddings` and existing `E5Embedder`) and add an optional local CPU cross-encoder reranker (`FastEmbedReranker`) to `SQLiteIndexStore.search_hybrid`, while preserving offline deterministic `HashingEmbedder` fallback in CI/tests and exposing embedder/reranker selection and identities in `retrieval.eval` for reproducible before/after benchmarking.

## Why

Directive `L20261124094804-a929` (item 7): Jarvis chat answers depend on project memory retrieval, which still defaults to `HashingEmbedder`. Adding a real local CPU embedder and cross-encoder reranker (running on CPU so they never contend with the single GPU running Inkling SQ-10) and wiring them into `retrieval.eval` directly improves chat grounding and lets us measure the gain on the existing retrieval benchmark.

## Code facts checked at `0198def8`

- `backend/app/modules/ai/retrieval_index.py::Embedder` defines the embedding protocol (`embed`, `embed_query`).
- `backend/app/modules/ai/retrieval_index.py::HashingEmbedder` is the deterministic offline fallback currently defaulted in `SQLiteIndexStore.__init__`.
- `backend/app/modules/ai/retrieval_index.py::E5Embedder` is an optional lazy `sentence_transformers` embedder adapter.
- `backend/app/modules/ai/retrieval_index.py::SQLiteIndexStore` owns the SQLite + FTS5 + optional `sqlite-vec` derived index, `has_compatible_embedder`, `_sync_vector_table`, `search_hybrid`, and `build_bundle`.
- `backend/app/modules/ai/retrieval_contracts.py::RetrievalHit` defines per-hit retrieval scores (`lexical_score`, `vector_score`, `graph_score`, `fused_score`).
- `backend/app/modules/ai/retrieval_owners.py::owner_catch_up` synchronizes canonical owners into `SQLiteIndexStore` and currently short-circuits when owner fingerprints match.
- `backend/app/modules/ai/retrieval_bench.py::evaluate_retrieval_request` implements the `retrieval.eval` bench evaluator over `SQLiteIndexStore`.
- `backend/app/modules/ai/retrieval_query.py::query_context` constructs `SQLiteIndexStore` for read-only navigation queries.

## Scope

In scope:
- **Reranker protocol and local CPU adapters** (`backend/app/modules/ai/retrieval_index.py` or sibling `backend/app/modules/ai/retrieval_models.py`):
  - Define `Reranker` protocol (`identity: str`, `score(query: str, passages: Sequence[str]) -> list[float]`).
  - Add lazy-imported CPU-only `FastEmbedEmbedder` (`fastembed.TextEmbedding`, default `BAAI/bge-small-en-v1.5`, configurable model name and local cache dir under `JARVISOS_DATA_ROOT`) and `FastEmbedReranker` (`fastembed.rerank.cross_encoder.TextCrossEncoder`, default `Xenova/ms-marco-MiniLM-L-6-v2`, CPU-only). Never silently download weights from the network during tests or normal requests when `local_files_only` / cache-absent; fail closed or fall back cleanly in `auto` mode.
  - Add optional `LoopbackHttpEmbedder` (loopback-only `127.0.0.1`/`localhost` OpenAI-compatible `/v1/embeddings` client for a user-run local CPU `llama-server --embedding`, rejecting non-loopback URLs fail-closed).
  - Add `default_embedder()` and `default_reranker()` factories driven by `JARVISOS_RETRIEVAL_EMBEDDER` (`auto` | `fastembed` | `e5` | `loopback_http` | `hashing`, default `auto`) and `JARVISOS_RETRIEVAL_RERANKER` (`auto` | `fastembed` | `none`, default `auto`). In `auto` mode, select `fastembed` only when `fastembed` is importable and its local model cache is already present on disk, else fall back to `HashingEmbedder` / `None`.
- **Hybrid reranking and embedder-change re-embedding** (`retrieval_index.py`, `retrieval_contracts.py`, `retrieval_owners.py`):
  - Add optional `rerank_score: float | None = Field(default=None, allow_inf_nan=False)` to `RetrievalHit` (backward-compatible default `None`).
  - Update `SQLiteIndexStore.__init__` to accept optional `reranker: Reranker | None = None` and use `default_embedder()` / `default_reranker()` when omitted (preserving explicit `embedder=HashingEmbedder()` in tests).
  - Update `SQLiteIndexStore.search_hybrid` to retrieve a bounded candidate pool (`min(max(limit * 3, 24), 64)`), apply the configured reranker to the top candidates when present, preserve `hermes_operational_memory` priority penalty and workspace isolation, and populate `rerank_score`.
  - Update `owner_catch_up` so an embedder identity mismatch (`not store.has_compatible_embedder()`) does not short-circuit on unchanged owner fingerprints; instead it re-embeds stored documents with the active embedder and updates `meta.embedder_identity` and `vec_docs`.
- **Benchmark wiring (`retrieval.eval`)** (`backend/app/modules/ai/retrieval_bench.py`):
  - Accept optional `embedder` and `reranker` selectors in `retrieval.eval` requests (defaulting to the store defaults) and record `embedder_identity` and `reranker_identity` in the returned evaluation details so A/B runs (`hashing` vs real embedder + reranker) are directly comparable.

Out of scope (binding non-goals):
- No GPU allocation or interference with the single hot local LLM (embedder and reranker run strictly on CPU).
- No mandatory PyTorch dependency in `backend/requirements.txt`; `fastembed` / ONNX Runtime remains an optional local runtime installed on the host.
- No SQLite schema migration (existing `docs`, `vec_docs`, and `meta` tables already store variable-dimension vectors and `embedder_identity`).
- No network downloads during offline unit tests or CI.

## Files likely touched

- `backend/app/modules/ai/retrieval_contracts.py`
- `backend/app/modules/ai/retrieval_index.py`
- `backend/app/modules/ai/retrieval_owners.py`
- `backend/app/modules/ai/retrieval_bench.py`
- `backend/tests/test_retrieval_embedder_reranker_365.py` (new)

## Design constraints

- Offline CI tests must pass with zero network access and without `fastembed` installed, using fake/stub embedder and reranker implementations.
- `LoopbackHttpEmbedder` must reject any non-loopback host (SSRF / egress invariant).
- `RetrievalHit` change is strictly additive (`rerank_score: float | None = None`); existing frozen contract invariants remain intact.
- Medium risk (feature code with tests; no migration, no auth/egress weakening, loopback-only HTTP option): coordinator severe review and CI green on exact head.

## Acceptance criteria

1. `default_embedder()` and `default_reranker()` select `FastEmbedEmbedder`/`FastEmbedReranker` in `auto` mode when available with local cache, fall back cleanly to `HashingEmbedder`/`None` when absent, and honor explicit env overrides (`hashing`, `none`, `fastembed`, `loopback_http`).
2. `LoopbackHttpEmbedder` rejects non-loopback URLs fail-closed and parses valid loopback `/v1/embeddings` responses.
3. `SQLiteIndexStore.search_hybrid` applies a configured `Reranker` to top RRF candidates, populates `rerank_score`, improves ranking when the reranker promotes the true passage, and preserves `hermes_operational_memory` tie/priority penalty and workspace isolation.
4. `owner_catch_up` detects an `embedder_identity` mismatch even when owner fingerprints are unchanged and re-embeds documents cleanly.
5. `retrieval.eval` reports `embedder_identity` and `reranker_identity` and supports comparing `hashing` vs a real/stub embedder + reranker.
6. All existing retrieval and `retrieval.eval` tests remain green.

## Required tests

- Offline backend tests in `backend/tests/test_retrieval_embedder_reranker_365.py` covering criteria 1–5 (no network, no model downloads).
- Existing retrieval test suite (`test_retrieval_*.py`, `test_memory_search*.py`) remains green.

## Definition of done

Test gate green (see `AGENTS.md`), coordinator severe review complete, CI green on exact head, STATUS row reconciled.
