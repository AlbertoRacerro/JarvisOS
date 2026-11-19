# 318 — CASE-DECISION-1: Record an accepted decision on a case verdict from the readiness board

Status: ready
Depends on: 303, 304, 312, 314

## Goal

A user can record a decision on a literature case directly in the readiness board's `Decisione` cell, linked to
the case's accepted verdict, and the cell turns green only for a decision linked to a verdict of that case. A
nullable column `decisions.case_verdict_id` (additive migration `0036_decision_case_verdict`) carries the link.
The guided flow's `decide` step counts only accepted decisions.

## Why

Directives `L20261116155611-930c` point 2 and `L20261116160140-7608` point 2 (Wave 0): the board is the map of what
each case lacks, and "a case verdict as a product object" leads to a recorded decision. Today the `decide` cell
cannot turn green through the interface at all:

- The cell requires an accepted decision with `linked_run_id`, but `decisions.linked_run_id` is a foreign key to
  `simulation_runs`, while the board's `run` and `compare` steps use bench runs, which are folders under the data
  root. A decision cannot reference the run that the case compared.
- No screen creates a decision: Project Basis (where the cell links) lists decisions read-only, and
  `createDecision` in `frontend/src/api/client.ts` has no caller.
- The rule is per workspace, not per case, so one decision would turn every case of a workspace green.
- The guided flow marks `decide` done when any decision exists, including a draft.

## Code facts checked at `c94c1a09`

- `backend/app/core/schema.py` creates `decisions` with `linked_run_id TEXT` and
  `FOREIGN KEY (linked_run_id) REFERENCES simulation_runs(id)`; `CURRENT_SCHEMA_MIGRATION_ID` is
  `0035_case_verdicts`; `backend/app/core/database.py` turns `PRAGMA foreign_keys` on.
- `backend/app/modules/engineering/bench_runs_view.py::list_runs` lists bench runs from folders under
  `bench_root(workspace_id)`, not from `simulation_runs`.
- `backend/app/modules/engineering/readiness_board.py::_compute_case` marks `decide` green when any decision of the
  workspace has `status = 'accepted'` and a non-empty `linked_run_id`, and otherwise red with
  `missing=["decision"]`.
- `backend/app/modules/engineering/case_verdicts.py::create_verdict` and
  `backend/app/modules/engineering/case_verdicts.py::update_verdict` store verdicts with `case_id`, `run_id`,
  `status` among `recorded`, `accepted`, `superseded`.
- `backend/app/modules/modeling/models.py::DecisionCreate` has `status` (default `draft`) and `linked_run_id`;
  `backend/app/modules/modeling/service.py::create_decision` calls
  `backend/app/modules/modeling/project_knowledge_owner.py::create_decision_in_transaction`, which inserts the row
  and logs `DecisionCreated` with actor `local-user`.
- `backend/app/modules/memory/service.py::_transition` is the AI-proposal path (`proposed` to `accepted` from the
  Review stage); user-authored decisions do not pass through it.
- `backend/app/modules/engineering/guided_flow.py::evaluate_step` marks `decide` done when the question has a
  `linked_decision_id` or any row exists in `decisions` for the workspace.
- `frontend/src/pages/ReadinessBoard.tsx` already records and accepts case verdicts in the `compare` cell
  (`Registra verdetto`, `Accetta verdetto`) and links the `decide` cell to `/memory/project-basis`.

## Scope

In scope:
- Migration `0036_decision_case_verdict`: `case_verdict_id TEXT` in the `decisions` CREATE statement and
  `ALTER TABLE decisions ADD COLUMN case_verdict_id TEXT` in the migration list, nullable, no foreign key (checked in
  code, so fresh and upgraded schemas stay equal), existing rows unchanged; `test_migration_discipline.py` passes.
- `DecisionCreate` and `DecisionRead` gain `case_verdict_id: str | None = None`; the insert writes it. When it is
  set, `create_decision_in_transaction` rejects it with `ProjectKnowledgeOwnerError("case_verdict_invalid")` (HTTP
  422 through the existing `_domain_error`) unless the verdict exists in the same workspace with `status = 'accepted'`. Regenerate `frontend/src/api/generated`.
- `_compute_case` `decide`: green iff an accepted decision of the workspace has a `case_verdict_id` whose verdict
  has this case's `case_id`, the same workspace and `status != 'superseded'`. Red detail:
  `No accepted decision linked to an accepted verdict of this case`. The `linked_run_id` rule is removed from the
  board (it does not apply to bench runs). Optionally the green cell carries the decision's title for display.
- `evaluate_step` `decide`: done iff the question's `linked_decision_id` names an accepted decision of the workspace,
  or any accepted decision exists in the workspace; drafts no longer count.
- `ReadinessBoard.tsx`, `decide` cell detail when red:
  - when the case has an accepted, non-superseded verdict: a form with `Titolo`, `Decisione` and optional
    `Motivazione`, and a button `Registra decisione` that calls `createDecision` with `status: "accepted"` and the
    verdict's id, then reloads the board;
  - otherwise the text `Prima registra e accetta il verdetto nel passo Confronto.`;
  - the `decide` entry of `STEP_LINKS` (Project Basis, read-only) is removed. Reason text for `missing_record` on
    `decide` becomes `Manca una decisione accettata collegata al verdetto di questo caso.`
- Tests: `backend/tests/test_case_decision_318.py`, updates to `test_migration_discipline.py`, the guided-flow and
  board tests whose expectations change, `frontend/tests/318-case-decision.test.mjs` added to the `build` chain,
  `frontend/tests/304-readiness-board-screen.test.mjs` literal updates.
- Browser walk: extend `e2e/case_verdict_walk.mjs` so that after accepting the verdict it opens the `Decisione`
  cell, fills the form, clicks `Registra decisione`, sees the cell turn green, and confirms it after a reload.

Out of scope:
- Decision alternatives, reversal trigger, evidence rung (directive `L20261116205211-8b3c` point 4, deferred by
  `L20261116205733-a728`).
- Editing, retiring or superseding a decision from the board.
- A decision form in Project Basis or in the Design stage.
- Any change to `linked_run_id` or to the AI proposal and Review path.

## Design constraints

- High risk (additive schema migration). Additive only: no table rebuild, no data rewrite.
- Rule 7: the form writes the user's own text; no model drafts or accepts the decision.
- Workspace isolation: the verdict must belong to the decision's workspace; the board query joins on both ids.
- Visible text in Italian; no ids, paths or digests in visible text (the run id row already shown by spec 305 stays).

## Acceptance criteria

1. On an upgraded copy of a database at `0035_case_verdicts`, `initialize_database` adds `case_verdict_id` and
   keeps every existing decision row; fresh and upgraded schemas match in `test_migration_discipline.py`.
2. `POST /workspaces/{id}/decisions` with a `case_verdict_id` of another workspace, of a `recorded` verdict, or
   unknown, returns 422 `case_verdict_invalid` and writes nothing; with an accepted verdict of the same workspace it returns 201 and
   `case_verdict_id` in the body.
3. The board's `decide` cell for case A is red when the only accepted decision links a verdict of case B, or a
   superseded verdict of A, or has no `case_verdict_id`; green when it links an accepted verdict of A.
4. The guided flow's `decide` step is `not_started` with only a draft decision and `done` with an accepted one.
5. The board renders the form only when the case has an accepted verdict, and the hint otherwise; submitting turns
   the cell green without a manual reload; `npm run build` passes with the new test file.
6. The extended `e2e/case_verdict_walk.mjs` passes from `/` in a clean profile with zero console and page errors.

## Required tests

- `backend/tests/test_case_decision_318.py`
- `backend/tests/test_migration_discipline.py`
- `frontend/tests/318-case-decision.test.mjs`
- `e2e/case_verdict_walk.mjs`

## Risk and evidence

High (additive schema migration). Evidence: focused backend tests, `test_migration_discipline.py`, `ruff`,
`npm run build`, gates (`check_spec_status.py`, `check_code_facts.py`, `generate_frontend_contracts.py --check`,
`gen_modules.py`, `check_doc_paths.py`, `check_architecture_enforcement.py`, `check_typecheck_ratchet.py`), CI green
on the exact head, a second-session review of the full diff, the migration applied to a temporary copy of a data
root at `0035`, and the extended browser walk.
