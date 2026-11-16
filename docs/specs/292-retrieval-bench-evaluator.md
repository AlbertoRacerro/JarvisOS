# 292 — RETRIEVAL-BENCH-EVALUATOR-1: Headless retrieval evaluation in `bench`

Status: ready
Depends on: 282, 287, 289, 291

## Goal

`python -m app.modules.engineering.bench` exposes a `retrieval.eval` evaluator (`bench list`, `bench run retrieval.eval`, and `bench compare`) that runs a question set against a local corpus or SQLite index and emits a checked `EvaluationResult` with `top1_hit_rate`, `top3_hit_rate`, `top10_hit_rate`, `mrr`, `question_count`, and `zero_hit_count`, keeping private question sets outside the repository.

## Why

Maintainer directive `L20261116132306-5851` point 6 requires making the retrieval measurement method a `bench` evaluator (top 1, 3, 10 before and after any change to search) while keeping evaluation questions in the private pack.

## Code facts checked at `bab12852`

- `backend/app/modules/engineering/bench.py::list_evaluators` lists registry and `BLUECAD_EVALUATORS` descriptors for `bench list`.
- `backend/app/modules/engineering/bench.py::run_evaluation` dispatches `bench run <evaluator_id>` and computes `result_verdict(descriptor, result)`.
- `backend/app/modules/engineering/bench.py::_check_within_root` enforces root-confined paths for `--root`-relative inputs and `--out`.
- `backend/app/modules/engineering/evaluator_contracts.py::EvaluationResult`, `EvaluatorDescriptor`, `ResultCheck`, `NamedQuantity`, and `result_verdict` define the checked result envelope and verdict rule.
- `backend/app/modules/ai/retrieval_index.py::SQLiteIndexStore`, `SQLiteIndexStore.upsert`, `SQLiteIndexStore.search_lexical`, `SQLiteIndexStore.search_vector`, and `SQLiteIndexStore.search_hybrid` implement the retrieval index and search legs.
- `backend/app/modules/ai/document_intake.py::extract_passages` chunks markdown/text files into heading-bounded passages.

## Scope

In scope:
- Add `backend/app/modules/ai/retrieval_bench.py` implementing `retrieval_eval_descriptor()` and `evaluate_retrieval_request(...)` returning an `EvaluationResult` (and per-question breakdown dict) over `SQLiteIndexStore`:
  - Supports `mode`: `"lexical" | "vector" | "hybrid"` (default `"hybrid"`), `limit` (default `10`, minimum `10`), `workspace_id` (optional default).
  - Supports corpus input via inline `documents` (`[{id, title?, text, source_owner?, source_id?, workspace_id?}]`), `corpus_dir` (path within `root`, chunked with `extract_passages` into an isolated temporary `SQLiteIndexStore`), and/or `index_db_path` (existing SQLite index file within `root`, opened read-only/isolated).
  - Evaluates `questions`: `[{id, query, expected_ids, workspace_id?}]`, matching `expected_ids` against hit `source_ref.source_id`, `title`, or `f"{source_ref.source_owner}:{source_ref.source_id}"`.
  - Emits `NamedQuantity` outputs: `top1_hit_rate`, `top3_hit_rate`, `top10_hit_rate`, `mrr`, `question_count`, `zero_hit_count` (unit `"1"`).
  - Emits required `ResultCheck` items: `RETRIEVAL_CORPUS_NONEMPTY` (`input`), `RETRIEVAL_QUESTIONS_NONEMPTY` (`input`), and `RETRIEVAL_EVAL_COMPLETE` (`numerical`), with `verdict` computed by `result_verdict`.
- Register `retrieval.eval` in `backend/app/modules/engineering/bench.py` (`list_evaluators` and `run_evaluation`), writing `per_question.json` alongside `result.json` in `out_dir`.

Out of scope (binding non-goals):
- Exposing `retrieval.eval` on the HTTP engineering `/evaluators` route (`operator_service.py`), adding LLM query rewriting, installing external embedding models, or committing any private pack questions to the repository.

## Files likely touched

- `backend/app/modules/ai/retrieval_bench.py`
- `backend/app/modules/engineering/bench.py`
- `backend/tests/test_retrieval_bench.py`

## Design constraints

- All file/directory inputs (`index_db_path`, `corpus_dir`) must be validated with `_check_within_root` against `root` (no path traversal).
- No network access or external model downloads; default embedder uses the existing offline `SQLiteIndexStore` default (`HashingEmbedder`).
- Question text and private pack fixtures stay out of the repository; tests use small synthetic in-memory/temp fixtures only.

## Acceptance criteria

1. `bench list` includes `retrieval.eval` with `backend_kind="specialist"`, `availability_state="available"`, and its required checks.
2. `bench run retrieval.eval <request.json> --out <out_dir> --root <root>` writes `result.json` and `per_question.json` and prints a bounded view with `verdict: "usable"` and exact `top1_hit_rate`, `top3_hit_rate`, `top10_hit_rate`, `mrr`, `question_count`, and `zero_hit_count`.
3. `bench compare <run_a> <run_b>` compares two `retrieval.eval` runs cleanly.
4. Empty corpus or empty questions produce `failed` required checks and `not_usable` verdict (withholding outputs per Spec 288).
5. Path traversal in `corpus_dir` or `index_db_path` outside `--root` is refused with `BenchInputError`.

## Required tests

- `backend/tests/test_retrieval_bench.py`:
  - `bench list` includes `retrieval.eval`.
  - `retrieval.eval` with inline `documents` and `questions` computes exact `top1_hit_rate`, `top3_hit_rate`, `top10_hit_rate`, `mrr`, `zero_hit_count` across `lexical` and `hybrid` modes.
  - `retrieval.eval` with `corpus_dir` inside `--root` ingests documents and evaluates questions.
  - Empty questions / empty corpus yield `not_usable` verdict and withheld outputs.
  - `corpus_dir` / `index_db_path` outside `--root` is rejected.
  - `bench compare` works on two `retrieval.eval` runs.

## Definition of done

Test gate green (`ruff`, focused `pytest`, `check_spec_status.py`, `check_code_facts.py`, CI), acceptance criteria met, `STATUS.md` updated.
