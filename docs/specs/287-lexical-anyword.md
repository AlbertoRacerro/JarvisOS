# 287 — Useful lexical retrieval for ordinary questions

Status: ready
Depends on: none

## Goal

An ordinary multiword question can find documents containing any of its words, ranked by SQLite FTS5 BM25, while exact object IDs, single symbol names, quoted phrases, workspace isolation and freshness checks keep their existing behavior.

## Why

The current lexical search requires every query word to appear in one document. Ordinary questions often return no lexical result, leaving hybrid retrieval to the weak default hash vector. This small change makes the existing word index useful before adding an intake or another model.

## Code facts checked at `94ef8955`

- `backend/app/modules/ai/retrieval_index.py::SQLiteIndexStore.search_lexical` handles exact IDs and symbols first, then builds an FTS5 expression with `AND`, ranks by `bm25`, and filters by workspace.
- `backend/app/modules/ai/retrieval_index.py::SQLiteIndexStore.search_hybrid` fuses lexical and vector hits; it does not own lexical query parsing.
- `backend/app/modules/ai/retrieval_index.py::SQLiteIndexStore._fresh_hits` applies the current freshness policy to returned hits.

## Scope

In scope:
- Change unquoted multiword lexical queries to FTS5 any-word matching with existing BM25 ranking and deterministic key tie-break.
- Keep quoted whole-query phrases exact.
- Update offline retrieval tests for a partial-word hit, no-word hit, ranking, workspace isolation, exact shortcuts and phrase behavior.

Out of scope:
- Document intake, embeddings, language translation, fusion changes, index schema changes or new dependencies.
- A new relevance threshold or stop-word policy; those require measurement on a representative corpus.

## Files likely touched

- `backend/app/modules/ai/retrieval_index.py`
- `backend/tests/test_retrieval_index.py`
- `backend/tests/test_retrieval_owners.py` if its old all-word expectation changes

## Design constraints

- Search remains bounded by `limit`; preserve the workspace predicate and `_fresh_hits` filtering.
- Query tokens are escaped for FTS5. Empty or punctuation-only queries return no hits.
- Exact object IDs and symbol names retain priority over text matches.

## Acceptance criteria

1. A query with one indexed word and one absent word returns the indexed document; a query with no indexed words returns no documents.
2. An exact quoted phrase requires the adjacent words in order, while an unquoted query accepts a partial word match.
3. BM25 orders a stronger text match before a weaker one on a small controlled fixture, with deterministic ties.
4. Results remain limited to the requested workspace and limit; exact ID and symbol shortcuts still work.

## Required tests

- Focused offline tests in `backend/tests/test_retrieval_index.py`, including the changed prior all-word assertion and representative ranking cases.
- Existing `backend/tests/test_retrieval_owners.py` cases that depend on all-word matching are revised to assert their actual isolation or freshness purpose.

## Definition of done

Focused tests and lint pass, CI is green on the implementation head, coordinator review of the diff is complete, and STATUS records the merged implementation PR.
