# 317 — RETRIEVAL-ANSWER-CHECK-1: A retrieval check that misses its expected answers is not green

Status: merged
Depends on: 300, 303, 316

## Goal

A `retrieval.eval` run whose expected answers are not all found among the first three results gets the verdict
`usable_with_warnings` instead of `usable`, through one new check `RETRIEVAL_EXPECTED_IN_TOP3`. The readiness
board's `answer` step, which already requires `usable`, therefore stays red until every question of a run finds its
expected source near the top.

## Why

Directive `L20261116160140-7608` point 2 makes "answering later with source" a Wave 0 capability, and the board
cell is its proof. Today the verdict rates only whether the measurement ran (non-empty corpus, non-empty questions,
no evaluation error): the spec 316 browser walk showed `Utilizzabile` beside a top-1 and top-3 hit rate of 0, and
that run turns the `answer` cell green. A green light that does not mean what it says makes every later result
less trustworthy (mission value question 2).

## Code facts checked at `b9278ddf`

- `backend/app/modules/ai/retrieval_bench.py::retrieval_eval_descriptor` declares three required checks and no
  advisory checks.
- `backend/app/modules/ai/retrieval_bench.py::evaluate_retrieval_request` computes `top1`, `top3`, `top10`, `mrr`
  and `zero_hit` from `first_matching_rank` per question and builds the three `ResultCheck` entries
  (`RETRIEVAL_CORPUS_NONEMPTY`, `RETRIEVAL_QUESTIONS_NONEMPTY`, `RETRIEVAL_EVAL_COMPLETE`).
- `backend/app/modules/ai/retrieval_bench.py::_matches` matches an expected id against the hit's `object_id`, its
  title, `owner:object_id`, and the `object_id` without a `#L` line suffix.
- `backend/app/modules/engineering/evaluator_contracts.py::ResultCheck` takes a `CheckFamily` among `input`,
  `numerical`, `conservation`, `discretisation`, `plausibility`, `reference`.
- `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` returns `not_usable` on any `fail`
  check, `usable_with_warnings` on any `warn` check, and `usable` otherwise when every required check passed.
- `backend/app/modules/engineering/readiness_board.py::compute_readiness_board` marks `answer` green only for a
  `retrieval.eval` run with `verdict == "usable"`.

## Scope

In scope:
- Add a fourth `ResultCheck` with id `RETRIEVAL_EXPECTED_IN_TOP3`, family `"reference"` (the expected ids are the
  reference the run is compared with), `actual` = top-3 hit rate, `limit` = `1.0`,
  unit `"1"`, source `"constant:retrieval_bench"`.
  - `pass` when every question's first matching rank is at most 3;
  - `warn` otherwise (including a top-3 rate of 0), with a plain hint naming how many questions missed;
  - `not_evaluated` when the corpus is empty, there are no questions or an evaluation error occurred (the other
    checks already fail).
- Register it in `retrieval_eval_descriptor` as an advisory check (not required), so a run whose answers are
  missing stays a valid, comparable measurement with visible outputs.
- Update existing tests whose fixtures miss expected answers and asserted `usable`
  (`backend/tests/test_retrieval_bench.py`) to assert `usable_with_warnings` and the new check status; do not
  change their metric assertions.
- Make the spec 316 backend test and browser walk use an expected id that the indexed fixture document actually
  matches (for example the document's `object_id` as returned by the memory search), so both still prove a green
  `usable` run; add one backend test case that a missed expected id yields `usable_with_warnings`.

Out of scope (binding non-goals):
- No change to `result_verdict`, the readiness board rule, the metric formulas, `_matches`, or the bench view.
- No new threshold configuration; the limit is the constant `1.0` on the top-3 rate.
- No frontend change beyond what the walk needs (the Memory screen already labels `usable_with_warnings` and says
  when the expected answer is missing from the top three).

## Files likely touched

- `backend/app/modules/ai/retrieval_bench.py`
- `backend/tests/test_retrieval_bench.py`
- `backend/tests/test_retrieval_eval_ui_316.py`
- `e2e/retrieval_eval_ui_walk.mjs`
- `docs/specs/STATUS.md`

## Design constraints

- The verdict vocabulary and `result_verdict` stay as they are; the change is one check plus one descriptor entry.
- Outputs stay visible for `usable_with_warnings` (only `not_usable` withholds them).
- Hints are English like the existing retrieval checks; the screen labels are unchanged.

## Acceptance criteria

1. A run in which every question finds an expected id within rank 3 has `RETRIEVAL_EXPECTED_IN_TOP3` = `pass`
   and verdict `usable`.
2. A run in which at least one question misses (rank > 3 or no match) has the check `warn`, verdict
   `usable_with_warnings`, and its outputs present.
3. A run with an empty corpus, no questions or an evaluation error keeps verdict `not_usable`; the new check is
   `not_evaluated`.
4. `retrieval_eval_descriptor().advisory_checks` contains `RETRIEVAL_EXPECTED_IN_TOP3`; `required_checks` is
   unchanged.
5. The spec 316 backend test and `e2e/retrieval_eval_ui_walk.mjs` reach a `usable` run with a matching expected
   id, and a backend test proves a missed id gives `usable_with_warnings` through
   `POST /workspaces/{workspace_id}/memory/retrieval-eval`.

## Required tests

- `backend/tests/test_retrieval_bench.py`: criteria 1–4 offline with inline documents.
- `backend/tests/test_retrieval_eval_ui_316.py`: criterion 5 through the route.
- `e2e/retrieval_eval_ui_walk.mjs` rerun with evidence.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
