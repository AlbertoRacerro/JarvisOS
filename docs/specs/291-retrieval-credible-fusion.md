# 291 — Credible-leg retrieval fusion and readable record summaries

Status: ready
Depends on: 287, 289

## Goal

`SQLiteIndexStore.search_hybrid` no longer lets a noisy or synthetic vector leg degrade strong lexical matches, and indexed owner records carry a concise, deterministic one-line readable summary (`what`, `about what`, `outcome`) ahead of their canonical JSON without changing `content_digest`.

## Why

Measured retrieval runs (`L20261116132306-5851`) showed two immediate quality gaps on top of specs 287 and 289: unweighted reciprocal-rank fusion with a noisy leg scored worse than the good leg alone, and stored workspace owner records were indexed as raw JSON blobs with no human-readable summary line. Fixing both in the core index improves retrieval precision and small-context legibility with zero new dependencies.

## Code facts checked at `d44a37b1`

- `backend/app/modules/ai/retrieval_index.py::SQLiteIndexStore.search_hybrid` merges `search_lexical` and `search_vector` with unweighted reciprocal-rank fusion (`1.0 / (60 + rank)`).
- `backend/app/modules/ai/retrieval_index.py::HashingEmbedder` is the default offline embedder when no semantic embedder is supplied.
- `backend/app/modules/ai/retrieval_query.py::query_context` constructs `SQLiteIndexStore` without a semantic embedder when `store` is omitted.
- `backend/app/modules/ai/retrieval_index.py::_model_document` serializes `model_dump(mode="json")` directly as `IndexDocument.text` while computing `content_digest` from the dict.
- `backend/app/modules/ai/retrieval_index.py::_record_document` serializes `model_dump(mode="json")` directly as `IndexDocument.text` while computing `content_digest` from the dict.
- `backend/app/modules/ai/retrieval_owners.py::_doc` serializes owner records as `json.dumps(data, ...)` while computing `content_digest` from `data`.

## Scope

In scope:
- In `SQLiteIndexStore.search_hybrid`, weight each leg by credibility: when `search_lexical` returns hits and the vector leg uses `HashingEmbedder` (or a leg has no credible match above a minimum score floor), down-weight that leg's RRF contribution (e.g. `0.05 * 1 / (60 + rank)`) so it breaks ties without overriding strong lexical matches, while preserving `vector_score` and `lexical_score` metadata on returned hits.
- Add a deterministic, pure summary helper used by `_model_document`, `_record_document` (and `_evidence_document` via it), and `retrieval_owners._doc` to prepend one readable line (`<kind>: <subject> — <outcome/status>`) before the canonical JSON body, keeping `content_digest` computed strictly from `data` so authoritative reread and digest verification are unchanged.

Out of scope (binding non-goals):
- Installing or loading external embedding models, PDF/OCR intake, LLM query rewriting, or adding a retrieval bench evaluator (separate slice).

## Files likely touched

- `backend/app/modules/ai/retrieval_index.py`
- `backend/app/modules/ai/retrieval_owners.py`
- `backend/tests/test_retrieval_index.py`

## Design constraints

- `content_digest` on every `IndexDocument` must remain computed from `data` (never from the summary prefix) so `resolve_authoritative` and `rebuild` idempotency stay exact.
- Existing `test_retrieval_index.py` invariants must hold, including `vector_score is not None` when `search_vector` matches an exact token under `HashingEmbedder` and `modeling` outranking `hermes_operational_memory` on ties.
- Summary lines must be bounded in length, deterministic, secret-safe (no new fields exposed beyond what `data` already contains), and offline-only.

## Acceptance criteria

1. When `search_lexical` returns a strong match and `search_vector` (using `HashingEmbedder` or returning noisy low-similarity hits) ranks an unrelated document first, `search_hybrid` keeps the lexical match ahead of the noisy vector-only hit.
2. When both legs agree or only one leg has hits, `search_hybrid` still returns hits with `lexical_score`, `vector_score`, and `fused_score > 0` populated as expected.
3. Indexed modeling, bluecad, and owner records begin with a single readable summary line naming the record kind, subject/title, and status/outcome, followed by the canonical JSON, and `resolve_authoritative` still validates `state == "current"` with unchanged `content_digest`.
4. Existing retrieval tests pass unchanged.

## Required tests

- Offline unit tests in `backend/tests/test_retrieval_index.py` covering criteria 1–3 with `HashingEmbedder` and temporary SQLite data roots.

## Definition of done

Focused tests and lint pass; CI green on the exact head; one coordinator review of the diff; STATUS records the PR.
