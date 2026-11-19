# 320 — CASE-PROCESS-COMPARE-1: A converged process run can carry a case's comparison and verdict

Status: in_review
Depends on: 303, 305, 314, 319

## Goal

The readiness board's `run` and `compare` cells, and the stored case verdict of spec 314, accept a converged run of
the Process editor as well as a bench run. A case whose compared quantity is a result of a flowsheet built from the
existing units (a KPI, a quantity a unit reports, or a stream value) can then go green on `Calcolo` and `Confronto`
and get an accepted verdict and decision, without first re-expressing the same physics as a bench evaluator.

## Why

Wave 1 of the case readiness work is reaching the existing units from the guided path. Spec 319 made the guided
flow's `model` and `run` steps see process drafts and their runs, but the board, which is the acceptance map for the
literature cases, still reads bench runs only: a user who reproduces a published figure in the Process editor sees
`Calcolo` and `Confronto` red and cannot record a verdict on it. For the first walkable cases the physics lives in
process units, so this is the remaining gap between "the engine exists" and "the case can be closed in the
interface". No unit, card or physics is added.

## Code facts checked at `7ce3cffd`

- `backend/app/modules/engineering/readiness_board.py::_compute_case` decides `run` green only from
  `bench_runs_view.list_runs` verdicts `usable`/`usable_with_warnings`; `_compare_step` reads the named output of the
  first such bench run through `_run_output(bench_root, run_id, quantity)` and converts with `_convert` (single Pint
  owner). The stored verdict lookup matches `case_verdicts.run_id` to `Comparison.run_id`.
- `backend/app/modules/engineering/case_verdicts.py::create_verdict` accepts only a run id found in
  `bench_runs_view.list_runs` with a usable verdict, and reads the value with the same `_run_output`.
  `case_verdicts.run_id` is free text; no migration is needed to store another kind of reference.
- `backend/app/modules/process_stack/draft.py::list_drafts`, `draft_dir` (draft ids match `[a-f0-9-]{36}`),
  `list_runs` (newest first, `outcome` computed on read for older runs), `get_run` (run ids match `[a-f0-9]{32}`)
  and `run_results` (returns `results_view.build(run, document)` at the run's own revision).
- `backend/app/modules/process_stack/results_view.py::build` returns, for a converged run, `kpis` (list of
  `{id, value, unit, status}`), `units[<tag>]["quantities"][<key>]` and `streams[<tag>][<property>]` as
  `{value, unit}` items; `outcome["state"] == "converged"` together with `action == "run"` and
  `status == "completed"` is the only usable answer (`backend/app/modules/process_stack/draft.py::_converged`, spec 319).
- `frontend/src/pages/ReadinessBoard.tsx` shows `comparison.run_id` in the comparison table and posts it back as
  `run_id` when recording a verdict.

## Scope

In scope:
- **Process run reference.** A process run is referred to as `process:<draft_id>:<run_id>`. It is usable when the
  run has action `run`, status `completed` and outcome state `converged`, as in spec 319.
- **Named value of a process run.** For a usable process run, the case quantity resolves, in this order, to:
  1. a KPI whose `id` equals the quantity and whose status is `available`;
  2. `<unit tag>.<key>`: `units[<unit tag>]["quantities"][<key>]` with a finite value;
  3. `<stream tag>.<property>`: `streams[<stream tag>][<property>]` with a finite value.
  Anything else is "absent". One helper serves the board and the verdict route.
- **`run` cell**: also green when any of the 20 newest drafts of the case workspace has a usable process run.
  The red detail names both kinds ("Nessun calcolo utilizzabile: né banco di prova né diagramma di processo
  convergente" or the English equivalent used by the existing detail strings).
- **`compare` cell**: bench runs are tried first, exactly as today; when no usable bench run carries the quantity,
  the newest usable process run (across the 20 newest drafts, by `started_at`) that carries it is used.
  `Comparison.run_id` is then the process reference, and a new optional `Comparison.run_label` gives a path-free
  label naming the draft (for example `Diagramma "Linea 1"`). The red detail when nothing carries the quantity
  names both kinds.
- **Verdict route**: `create_verdict` accepts a process reference, refuses it with the existing codes
  (`run_not_found`, `run_not_usable`, `run_lacks_quantity`, `unit_mismatch`) under the same conditions, and stores
  the reference as `run_id`. Bench references behave exactly as today.
- **Board screen**: the comparison table shows `run_label` when present instead of the raw reference; the verdict
  form posts `comparison.run_id` unchanged.

Out of scope (binding non-goals):
- No new table, column or migration; no new unit, card, form or physics.
- No solve started from the board or the verdict route; reading only.
- No change to the `paper`, `facts`, `bound`, `decide` or `answer` rules, or to the guided-flow step rules.
- No absolute path, drafts directory or revision file name in any response.
- No change to the pack case file format: the case's `compare.quantity` simply may name a process value.

## Files likely touched

- `backend/app/modules/engineering/readiness_board.py`
- `backend/app/modules/engineering/case_verdicts.py`
- one small shared helper (in `readiness_board.py` or a new `backend/app/modules/engineering/process_run_values.py`)
- `frontend/src/pages/ReadinessBoard.tsx` and `frontend/tests/304-readiness-board-screen.test.mjs`
- generated frontend contracts (`python scripts/generate_frontend_contracts.py`) for `Comparison.run_label`
- a new `backend/tests/test_case_process_compare_320.py`

## Design constraints

- Use `draft.list_drafts`, `draft.draft_dir`, `draft.list_runs` and `draft.run_results` (or `results_view.build`
  through `load_revision`); do not parse revision files or duplicate the outcome rules of `results_view`.
- Bounded read: at most the 20 newest drafts and, per draft, the runs `list_runs` returns. A missing drafts
  directory, an unreadable draft or run counts as none; nothing raises out of the board.
- The process reference is parsed with the draft and run id patterns above; a malformed reference is
  `run_not_found` (404), never a path lookup.
- Deterministic: same files, same cells, same comparison.

## Acceptance criteria

1. A case workspace with no bench run and one converged process run evaluates `run` green.
2. A case whose `compare.quantity` equals an available KPI id of that converged run gets a `compare` cell with
   `run_id` `process:<draft_id>:<run_id>`, a `run_label` naming the draft, the KPI value and unit, and green or
   `out_of_tolerance` by the same deviation rule as today.
3. `<unit tag>.<key>` and `<stream tag>.<property>` quantities resolve as stated; an unknown name leaves `compare`
   red with `missing_record`.
4. A non-converged or failed process run never makes `run` or `compare` green.
5. When a usable bench run carries the quantity, the bench run is used, as before (existing 303, 305, 314 and 318
   tests pass unchanged).
6. `POST` of a verdict with a process reference from criterion 2 stores it; the board then shows that stored verdict
   on the `compare` cell, and accepting it plus an accepted decision turns `decide` green (spec 318 flow).
7. A malformed or unknown process reference is refused with `run_not_found`; a non-converged one with
   `run_not_usable`; a missing quantity with `run_lacks_quantity`.
8. No response field contains the data-root path.

## Required tests

- Offline backend tests writing draft heads, revisions and `run.json` files through `draft` module functions or the
  documented file layout in the isolated test data root, one per acceptance criterion.
- Frontend `.mjs` test asserting that the comparison table prefers `run_label`.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated. Risk: medium (feature code with
tests, no migration). The board screen changes, so `e2e/readiness_board_walk.mjs` is rerun with a converged process run
present before the maintainer is asked to look.
