# 319 — GUIDED-PROCESS-REACH-1: The guided path sees process flowsheets and their runs

Status: merged
Depends on: 312

## Goal

The guided flow's `model` and `run` steps count the workspace's process flowsheets (the Process editor's
drafts) and their runs, and the "Prossimo passo" card for a question with no model sends the user to the Process
editor. Today a user who builds a flowsheet from the existing units (pump, separator, culture units, heat
exchanger, recycle) and runs it to convergence still sees `Modello` and `Calcolo` as not started, because those
drafts and runs live as files under the data root and the step rules only read database tables.

## Why

Wave 1 of the case readiness work is reaching the existing units from the guided path: for the first five
literature cases the physics already exists in the Process editor's units. A path bar that cannot see a
converged flowsheet sends the user away from the screen that answers the question and makes the step rules
untrue. This slice adds no unit and no physics.

## Code facts checked at `a72404c7`

- `backend/app/modules/engineering/guided_flow.py::evaluate_step` decides `model` from parameters without a
  source, `design_components`, `model_specs` and `studio_drafts` rows only; it decides `run` from
  `bench_runs_view.list_runs` verdicts and `simulation_runs` statuses only.
- `backend/app/modules/engineering/guided_flow.py::compute_next_action` returns the step's own `route`
  (`/design/project` for `model`, `/runs` for `run`) as the card action.
- `backend/app/modules/process_stack/draft.py::list_drafts` lists a workspace's process drafts from
  `<workspaces_dir>/<workspace>/process/drafts/*/head.json`, path-free (`draft_id`, `name`, `revision`,
  `updated_at`); it raises `DraftError` when the workspace does not exist.
- `backend/app/modules/process_stack/draft.py::draft_dir` and
  `backend/app/modules/process_stack/draft.py::load_revision` give a draft's document; units are the entries of
  `document["objects"]` with `kind == "unit"`.
- `backend/app/modules/process_stack/draft.py::list_runs` returns a draft's runs newest first, each with an
  `outcome` dict (computed on read for older runs).
- `backend/app/modules/process_stack/results_view.py::run_outcome` gives `outcome["state"]` one of
  `validated`, `converged`, `non_converged`, `failed`, `cancelled` (and others for unknown statuses).
- `backend/app/modules/process_stack/draft.py::_converged` already treats a `run` action with status
  `completed` and outcome `converged` as the only usable answer.

## Scope

In scope:
- `model` step: also `done` when the workspace has at least one process draft whose head document holds at least
  one unit object. Unbound active parameters still make the step `blocked` first, as today.
- `run` step: also `done` when any draft of the workspace has a run with action `run` and outcome state
  `converged`. When nothing makes the step `done`, a latest draft run with state `non_converged` or `failed`
  makes it `blocked` with one item per such draft, in Italian, naming the draft (for example
  `Diagramma "Linea 1": non converge`). Existing bench and `simulation_runs` rules keep their meaning.
- "Prossimo passo" for `model` in state `not_started`: route `/design/process`, label `Apri il diagramma di
  processo`, reason naming the existing units in plain words (pompa, separatore, unità di coltura, scambiatore).
  The path bar's step route stays `/design/project`.
- "Prossimo passo" for `run` in state `blocked` from a draft: route `/design/process`.
- Reading drafts never raises out of the step evaluation: a missing drafts directory or an unreadable draft
  counts as no draft.

Out of scope (binding non-goals):
- No new unit, card, form, table or migration. No change to the Process editor screen.
- No solve started from the guided flow; reading only. Running solve jobs are not polled.
- No change to `frame`, `sources`, `explore`, `compare` or `decide` rules.
- No absolute path, draft directory or revision file name in any response.

## Files likely touched

- `backend/app/modules/engineering/guided_flow.py`
- `backend/tests/test_guided_flow_312.py` or a new `backend/tests/test_guided_process_reach_319.py`
- `frontend/tests/` only if a literal card label is asserted there

## Design constraints

- Use `draft.list_drafts`, `draft.draft_dir`, `draft.load_revision` and `draft.list_runs`; do not parse the
  revision files directly and do not duplicate the outcome rules of `results_view`.
- Bounded read: at most the 20 newest drafts of the workspace.
- Deterministic: same files, same states and items.

## Acceptance criteria

1. A workspace with a draft holding one unit and nothing else evaluates `model` as `done`.
2. A workspace with an empty draft (no unit objects) evaluates `model` as `not_started`, and its next action
   routes to `/design/process`.
3. A draft run with action `run`, status `completed` and a converged outcome makes `run` `done`.
4. A draft whose newest run is non-converged, with no other usable run, makes `run` `blocked` with an item naming
   the draft, and the next action routes to `/design/process`.
5. A workspace with no drafts directory evaluates exactly as before this spec (existing 312 and 318 tests pass
   unchanged).
6. No response field contains the data-root path.

## Required tests

- Offline tests writing draft heads, revisions and `run.json` files through `draft` module functions or the
  documented file layout in the isolated test data root, one per acceptance criterion.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written. Risk: medium
(read-only feature code with tests); the path-bar change is visible, so the guided-flow walk
(`e2e/guided_flow_walk.mjs`) is rerun with a draft present before asking the maintainer to look.
