# 314 — CASE-VERDICT-1: Stored case comparison and verdict product object

Status: ready
PR: —
Depends on: 305

## Goal

Persist a **case comparison and verdict** as a first-class workspace product object (`case_verdicts`) instead of computing comparisons only ephemerally on the readiness board. From the Prontezza screen (`ReadinessBoard.tsx`), the engineer can record a verdict for a case's comparison (computed bench run output vs published value), see its deterministic pass/fail status in Italian (`Entro tolleranza` / `Fuori tolleranza`), and accept it with an optional rationale.

## Why

Directive `L20261116160140-7608` item 2 (Wave 0): *"comparison with the published value and a case verdict as a product object"*. Spec 305 (`READINESS-COMPARE-1`) added on-read Pint conversion and deviation calculation for the readiness board, but no durable, linkable verdict record exists yet.

## Code facts checked at `ff89a525`

- `backend/app/core/schema.py::SCHEMA_STATEMENTS` and `backend/app/core/database.py::initialize_database` manage additive SQLite tables and migrations.
- `backend/tests/test_migration_discipline.py::BASE_CREATE_ONLY_SHA256` pins the `CREATE`-only column baseline and verifies fresh == upgraded.
- `backend/app/modules/engineering/readiness_board.py::_run_output`, `backend/app/modules/engineering/readiness_board.py::_convert`, and `backend/app/modules/engineering/readiness_board.py::_compare_step` extract a bench run's named output, convert units via Pint, and compute relative deviation.
- `backend/app/modules/engineering/bench_runs_view.py::list_runs` and `backend/app/modules/engineering/bench_runs_view.py::bench_root` list workspace bench runs and resolve the workspace bench root safely.
- `backend/app/modules/engineering/operator_routes.py::router` mounts workspace-scoped engineering routes under `/workspaces/{workspace_id}/engineering`.
- `frontend/src/pages/ReadinessBoard.tsx` renders the Prontezza board and comparison values (`computed`, `published`, `deviation`, `rel_tol`, `run_id`).

## Scope

In scope:
- Additive SQLite table `case_verdicts` in `backend/app/core/schema.py` (`id`, `workspace_id`, `case_id`, `run_id`, `quantity`, `computed_value`, `computed_unit`, `published_value`, `published_unit`, `rel_tol`, `deviation`, `verdict` in `('pass', 'fail')`, `status` in `('recorded', 'accepted', 'superseded')`, `rationale`, `accepted_by`, `accepted_at`, `created_at`, `updated_at`), index `idx_case_verdicts_workspace`, new migration ID (`0035_case_verdicts` after 312 merges, or next sequential ID), and updated `BASE_CREATE_ONLY_SHA256` in `backend/tests/test_migration_discipline.py`.
- `backend/app/modules/engineering/case_verdicts.py`:
  - `list_verdicts(workspace_id)`: lists stored verdicts for the workspace, newest first (`404` if workspace does not exist).
  - `create_verdict(workspace_id, payload)`:
    - validates `case_id`, `run_id`, `quantity`, finite non-zero `published_value`, `published_unit`, `0 < rel_tol <= 1.0`.
    - verifies `run_id` belongs to `workspace_id` via `bench_runs_view.list_runs(workspace_id)` and has `verdict` in `("usable", "usable_with_warnings")`; returns `404` if run not found, `422` if run is not usable or lacks `quantity`.
    - extracts `(computed_value, computed_unit)` via `readiness_board._run_output`, converts `computed_value` to `published_unit` via `readiness_board._convert` (`422` on unit mismatch).
    - deterministically computes `deviation = abs(converted - published_value) / abs(published_value)` and `verdict = "pass" if deviation <= rel_tol else "fail"` (never caller-supplied).
    - inserts row with `status = "recorded"` and returns the created verdict record.
  - `update_verdict(workspace_id, verdict_id, payload)`:
    - updates `status` (`recorded`, `accepted`, `superseded`) and/or `rationale`; sets `accepted_at` and `accepted_by = "operator"` when transitioning to `accepted`; returns `404` if `verdict_id` does not exist in `workspace_id`.
- Routes in `backend/app/modules/engineering/operator_routes.py`:
  - `GET /workspaces/{workspace_id}/engineering/case-verdicts`
  - `POST /workspaces/{workspace_id}/engineering/case-verdicts`
  - `PATCH /workspaces/{workspace_id}/engineering/case-verdicts/{verdict_id}`
- `readiness_board.py`: attach optional `stored_verdict` summary (`id`, `verdict`, `status`, `run_id`, `deviation`, `rationale`) to `StepResult` for `compare` when a non-superseded `case_verdicts` row exists for `(workspace_id, case_id)` matching the compared `run_id`.
- `frontend/src/api/caseVerdicts.ts` and regenerated `frontend/src/api/generated` (`scripts/generate_frontend_contracts.py`).
- `frontend/src/pages/ReadinessBoard.tsx`:
  - Fetch stored verdicts per case workspace; in the `Confronto` detail panel, when a comparison is present, show either:
    - `"Registra verdetto"` button when no stored verdict matches the current run, or
    - the stored verdict badge (`"Entro tolleranza"` / `"Fuori tolleranza"`), status (`"Registrato"` / `"Accettato"`), optional rationale input, and `"Accetta verdetto"` button when `status === "recorded"`.
- Tests: `backend/tests/test_case_verdicts_314.py`, `backend/tests/test_migration_discipline.py`, `frontend/tests/314-case-verdicts.test.mjs` (in `frontend/package.json` `build` chain), and `e2e/case_verdict_walk.mjs`.

Out of scope (binding non-goals):
- Caller-supplied or model-overridden `verdict` (`pass`/`fail` is always computed deterministically from Pint conversion and `rel_tol`).
- Modifying `cases.toml` or adding pack-specific case names to the core repo.

## Design constraints

- High risk for additive schema migration (`test_migration_discipline.py` fresh == upgraded).
- Workspace isolation: every query filters by `workspace_id`; cross-workspace `PATCH` returns `404`; `run_id` must belong to the target workspace.
- No internal IDs or filesystem paths in visible UI text; Italian labels (`Registra verdetto`, `Accetta verdetto`, `Entro tolleranza`, `Fuori tolleranza`, `Registrato`, `Accettato`).

## Acceptance criteria

1. `POST /workspaces/{id}/engineering/case-verdicts` computes `deviation` and `verdict` (`pass`/`fail`) deterministically from the workspace's usable bench run output via Pint, rejecting unknown runs (`404`), non-usable runs (`422`), and unit mismatches (`422`).
2. `GET` lists verdicts for that workspace only; `PATCH` accepts/updates rationale (`200`) and returns `404` across workspaces.
3. `readiness_board.py` surfaces `stored_verdict` on the `compare` step when a matching verdict exists.
4. `test_migration_discipline.py` passes with the new migration.
5. `ReadinessBoard.tsx` renders `"Registra verdetto"`, records a verdict, displays `"Entro tolleranza"` / `"Fuori tolleranza"`, and accepts it; `frontend/tests/314-case-verdicts.test.mjs` passes in `npm run build`.
6. Browser walk `e2e/case_verdict_walk.mjs` from `/` opens Prontezza, records and accepts a verdict on a synthetic fixture case, reloads to confirm persistence, with no console or page errors.

## Required tests

- `backend/tests/test_case_verdicts_314.py`
- `backend/tests/test_migration_discipline.py`
- `frontend/tests/314-case-verdicts.test.mjs`
- `e2e/case_verdict_walk.mjs`

## Risk and evidence

High (additive schema migration). Evidence: backend tests and `test_migration_discipline.py`, `ruff`, `npm run build`, gates (`check_spec_status.py`, `check_code_facts.py`, `generate_frontend_contracts.py --check`, `gen_modules.py`, `check_doc_paths.py`, `check_architecture_enforcement.py`), CI green on exact head, second-session review of the diff, and browser walk `e2e/case_verdict_walk.mjs`.
