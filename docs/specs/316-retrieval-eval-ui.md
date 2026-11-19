# 316 — RETRIEVAL-EVAL-UI-1: Run retrieval quality checks from Memory Search

Status: merged
Depends on: 292, 300, 303

## Goal

On the Memory `"Ricerca"` screen, the user can run a `retrieval.eval` check over the workspace's indexed memory with one or more `(query, expected_ids)` questions, inspect the resulting `BenchRunDetail` (verdict light, `top1_hit_rate`, `top3_hit_rate`, `mrr`, `zero_hit_count`), and persist the run under the workspace bench root so the readiness board's `"Risposta"` (`answer`) cell can turn green from the interface.

## Why

Maintainer directives `L20261116160140-7608` (Wave 0: answering later with source), `L20261116201429-63ee` (point 6: a capability reachable only by code or CLI is not counted as usable), and `L20261117071737-1216` (retrieval quality measure #145 has no screen; place under Memory) require exposing `retrieval.eval` in the Memory UI. Today `readiness_board.py` checks for a `usable` `retrieval.eval` run in the workspace bench root, but no route or screen lets the user create one.

## Code facts checked at `ca56e1a2`

- `backend/app/modules/ai/retrieval_bench.py::evaluate_retrieval_request` evaluates `retrieval.eval` with `mode` (`lexical`, `vector`, `hybrid`), `questions` (`query`, `expected_ids`, `workspace_id`), and optional `index_db_path` validated within `root`.
- `backend/app/modules/engineering/bench.py::run_evaluation` dispatches `"retrieval.eval"` to `evaluate_retrieval_request`, writes `result.json` and `per_question.json` into `out_dir`, and applies the `not_usable` output withholding rule.
- `backend/app/modules/engineering/bench_runs_view.py::bench_root` resolves the workspace bench root; `backend/app/modules/engineering/bench_runs_view.py::show` returns a path-free `BenchRunDetail`.
- `backend/app/modules/engineering/readiness_board.py::compute_readiness_board` marks the `"answer"` step green when a `retrieval.eval` run in `bench_root(workspace_id)` has `verdict == "usable"`.
- `backend/app/modules/ai/memory_routes.py::get_documents_folder` owns `/workspaces/{workspace_id}/memory` routes and workspace validation.

## Scope

In scope:
- Add `POST /workspaces/{workspace_id}/memory/retrieval-eval` in `backend/app/modules/ai/memory_routes.py` accepting `{ questions: [{ id?, query, expected_ids }], mode?: "lexical" | "hybrid" | "vector" }` (default `"lexical"`).
- Resolve the workspace's SQLite retrieval index path (via `retrieval_query` / `build_paths().data_root`), pass `root=build_paths().data_root.resolve()` so `evaluate_retrieval_request` accepts `index_db_path`, generate a safe `run_id` (`SAFE_ID`), call `bench.run_evaluation("retrieval.eval", ..., out_dir=bench_root(workspace_id) / run_id, root=build_paths().data_root.resolve())`, and return `bench_runs_view.show(workspace_id, run_id)` (path-free).
- Add frontend API helper `runRetrievalEval` in `frontend/src/api/memorySearch.ts` and a compact `"Verifica del recupero"` section in `frontend/src/pages/MemorySearch.tsx` with question input (`Domanda`), expected match input (`Titolo o ID atteso`), mode selector (`Lessicale`, `Ibrida`), `"Verifica recupero"` button, and result display (verdict light + hit rate / MRR / zero hits).
- Add tests: `backend/tests/test_retrieval_eval_ui_316.py`, `frontend/tests/316-retrieval-eval-ui.test.mjs` (registered in `frontend/package.json`), and `e2e/retrieval_eval_ui_walk.mjs`.

Out of scope (binding non-goals):
- Editing `retrieval_bench.py` scoring (except the amendment below), verdict thresholds, or `readiness_board.py` logic.
  Amendment at review of #186: `_matches` also accepts, for `workspace_documents` passages, the file path, location fragment and heading that the `"Ricerca"` results show; without it the expected value a user copies from the screen never matched and every check scored 0.
- Uploading or managing external benchmark JSON question sets on disk.
- SQLite schema changes or migrations.

## Files likely touched

- `backend/app/modules/ai/memory_routes.py`
- `backend/tests/test_retrieval_eval_ui_316.py` (new)
- `frontend/src/api/memorySearch.ts`
- `frontend/src/pages/MemorySearch.tsx`
- `frontend/tests/316-retrieval-eval-ui.test.mjs` (new)
- `frontend/package.json`
- `e2e/retrieval_eval_ui_walk.mjs` (new)
- `docs/MODULES.md` (regenerated if line count changes)

## Design constraints

- No filesystem paths in any request or response payload; `index_db_path` and `out_dir` are resolved server-side only.
- Empty `questions`, blank `query`, empty `expected_ids`, or invalid `mode` return HTTP 422 without creating a run directory.
- Unknown `workspace_id` returns HTTP 404.
- All UI labels, status text, and errors are in plain Italian.
- Reuses `bench.run_evaluation` and `bench_runs_view.show`; no duplicate scoring or bench storage logic.

## Acceptance criteria

1. `POST /workspaces/{workspace_id}/memory/retrieval-eval` returns 404 for unknown `workspace_id` and 422 for invalid/empty questions or invalid mode.
2. For a workspace with indexed records, `POST /workspaces/{workspace_id}/memory/retrieval-eval` with a matching question persists `result.json` under `bench_root(workspace_id)`, returns a path-free `BenchRunDetail` with `evaluator_id == "retrieval.eval"` and `verdict == "usable"`, and causes `compute_readiness_board()` `"answer"` step for that workspace's case to turn green when applicable.
3. On `MemorySearch.tsx`, `"Verifica del recupero"` submits a question, displays the verdict and metrics (`top1_hit_rate`, `top3_hit_rate`, `mrr`, `zero_hit_count`), and handles errors without crashing.
4. `e2e/retrieval_eval_ui_walk.mjs` passes from `/` against a live server using visible controls only.

## Required tests

- `backend/tests/test_retrieval_eval_ui_316.py`: 404/422 validation, real workspace index evaluation via `POST /workspaces/{workspace_id}/memory/retrieval-eval`, path-free response, and `readiness_board` `"answer"` step integration.
- `frontend/tests/316-retrieval-eval-ui.test.mjs`: static assertions on `MemorySearch.tsx` and `memorySearch.ts`; added to `frontend/package.json`.
- `e2e/retrieval_eval_ui_walk.mjs`: browser walk from `/` to Memory `"Ricerca"`, running a retrieval check and verifying the verdict and metrics appear.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, `e2e/retrieval_eval_ui_walk.mjs` passes, spec status updated, summary written.
