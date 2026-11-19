# 315 — CORRELATION-CARDS-1: Correlation cards and bench run form on the Runs screen

Status: ready
Depends on: 299

## Goal

The Runs screen exposes a menu of correlation and property cards over the installed fluid-mechanics, heat-transfer and thermophysical libraries (`fluids.pipe_pressure_drop`, `ht.internal_convection`, `coolprop.pure_fluid_properties`), showing each card's formula, typed input fields, validity range and source, and lets the user run a card directly into the workspace's `Banco di prova` without writing code or CLI commands.

## Why

Maintainer directives `L20261116201429-63ee` (point 5 Level 2 and point 6) and `L20261116205733-a728` (point 8) require correlation cards wrapping the installed `fluids`, `ht` and `CoolProp` libraries with formula, fields, validity range and source, reachable from the interface so no capability is CLI- or code-only. Directive `L20261117071737-1216` and spec 299 left starting a bench run from a form on the Runs screen as the next slice after the read-only `Banco di prova` view.

## Code facts checked at `ff89a525`

- `backend/app/modules/process_stack/correlations.py::PipePressureDropEvaluator` evaluates `fluids.pipe_pressure_drop` with required SI inputs (`density`, `dynamic_viscosity`, `velocity`, `diameter`, `length`, `roughness`) and refuses transitional `Re` (`2100 <= Re < 4000`) and `roughness / diameter > 0.05`.
- `backend/app/modules/process_stack/correlations.py::InternalConvectionEvaluator` evaluates `ht.internal_convection` with required SI inputs (`reynolds_number`, `prandtl_number`, `thermal_conductivity`, `diameter`) and option `method` in `CONVECTION_METHODS` (`gnielinski`, `dittus_boelter_heating`, `dittus_boelter_cooling`) with explicit `(Re_min, Re_max, Pr_min, Pr_max)` domain checks.
- `backend/app/modules/process_stack/properties.py::CoolPropPropertyEvaluator` evaluates `coolprop.pure_fluid_properties` with `subject_ref.object_type == "material_state"`, option `fluid` in `FLUIDS`, and required inputs `temperature` (`K`) and `pressure` (`Pa`).
- `backend/app/modules/engineering/bench.py::run_evaluation` runs a registered evaluator from `request_data`, writes `result.json` into `out_dir`, applies the `not_usable` output withholding rule, and returns the result and bounded view.
- `backend/app/modules/engineering/bench_runs_view.py::bench_root` resolves the workspace bench directory; `backend/app/modules/engineering/bench_runs_view.py::show` returns a path-free detail view of a run.
- `backend/app/modules/engineering/operator_routes.py::list_bench_runs` exposes `GET /workspaces/{workspace_id}/engineering/bench/runs`.

## Scope

In scope:
- A static backend card catalog (`backend/app/modules/engineering/bench_cards.py`) describing the three installed specialist/property correlation cards (`fluids.pipe_pressure_drop`, `ht.internal_convection`, `coolprop.pure_fluid_properties`): `card_id`, `evaluator_id`, Italian title, formula summary, validity range text, source citation, typed numeric input fields (`name`, Italian `label`, `unit`, `default`), and option selectors (`name`, Italian `label`, `choices`, `default`).
- Two workspace-scoped routes on `backend/app/modules/engineering/operator_routes.py`:
  - `GET /workspaces/{workspace_id}/engineering/bench/cards` returning the card catalog (validating workspace existence, no paths).
  - `POST /workspaces/{workspace_id}/engineering/bench/runs` accepting `{ card_id, inputs, options }`, building a valid `EvaluationRequest` server-side, generating a unique safe `run_id`, calling `bench.run_evaluation` into `bench_root(workspace_id) / run_id`, and returning `bench_runs_view.show(workspace_id, run_id)` (path-free).
- Frontend API helpers in `frontend/src/api/benchRuns.ts` (`listBenchCards`, `createBenchRun`) and a compact `"Nuova esecuzione"` form in `frontend/src/components/BenchRunsPanel.tsx` where the user chooses a card, reads its formula, validity range and source, edits fields/options, clicks `"Esegui"`, and sees the new run selected and listed immediately.
- Unit/route tests (`backend/tests/test_correlation_cards_315.py`), frontend `.mjs` test (`frontend/tests/315-correlation-cards.test.mjs`), and a browser walk (`e2e/correlation_cards_walk.mjs`) verifying end-to-end execution from `/`.

Out of scope (binding non-goals):
- Authoring new custom correlation cards in Studio or persisting user-edited card definitions (deferred to a later slice).
- Running `bluecad.*` or `retrieval.eval` from this form (only the three installed correlation/property cards).
- SQLite schema changes or migrations.
- Modifying `backend/app/modules/process_stack/correlations.py` or `properties.py` physics/validity rules.

## Files likely touched

- `backend/app/modules/engineering/bench_cards.py` (new)
- `backend/app/modules/engineering/operator_routes.py`
- `backend/tests/test_correlation_cards_315.py` (new)
- `frontend/src/api/benchRuns.ts`
- `frontend/src/components/BenchRunsPanel.tsx`
- `frontend/tests/315-correlation-cards.test.mjs` (new)
- `frontend/package.json`
- `e2e/correlation_cards_walk.mjs` (new)
- `docs/MODULES.md` (regenerated if line count changes)

## Design constraints

- No filesystem paths in any request or response payload; `run_id` is generated server-side and validated via `SAFE_ID`.
- Every numeric input and option is validated against the selected card's declared fields/choices; unknown card ids, non-finite numbers, or missing required fields return HTTP 422.
- Domain refusals from the underlying evaluator (e.g., transitional Reynolds number) still produce a stored `result.json` with `status: "refused"` and are returned as a valid `BenchRunDetail` so the user sees the refusal reason in `"Banco di prova"` rather than an unhandled 500.
- Frontend labels, formula notes, validity range and error messages are in plain Italian; no raw backend codes as the only text.
- Reuses `bench.run_evaluation` and `bench_runs_view.show`; no duplicate evaluation or file-writing logic.

## Acceptance criteria

1. `GET /workspaces/{workspace_id}/engineering/bench/cards` returns all three cards (`fluids.pipe_pressure_drop`, `ht.internal_convection`, `coolprop.pure_fluid_properties`) with non-empty Italian title, formula, validity range, source, typed fields and options; unknown `workspace_id` returns 404.
2. `POST /workspaces/{workspace_id}/engineering/bench/runs` executes the chosen card via `bench.run_evaluation`, writes `result.json` under the workspace bench root, and returns a path-free `BenchRunDetail` with outputs when usable or refusal details when outside validity range.
3. Unknown `card_id`, unknown option value, or non-finite/missing numeric input returns HTTP 422 without creating a run directory.
4. On the Runs screen, `BenchRunsPanel` displays the card selector, formula, validity range, source, inputs, and `"Esegui"` button; submitting a valid card adds the run to the list and opens its detail.
5. `e2e/correlation_cards_walk.mjs` passes from `/` against a live server using visible controls only.

## Required tests

- `backend/tests/test_correlation_cards_315.py`: card catalog shape, 404 on missing workspace, 422 on invalid card/inputs/options, successful execution of `fluids.pipe_pressure_drop`, `ht.internal_convection`, and `coolprop.pure_fluid_properties`, domain refusal (transitional `Re`) stored and returned cleanly, and path-free response check.
- `frontend/tests/315-correlation-cards.test.mjs`: static assertions on `BenchRunsPanel.tsx` and `benchRuns.ts` card form, validity/source display, and Italian labels; added to `frontend/package.json`.
- `e2e/correlation_cards_walk.mjs`: browser walk from `/` to Runs, selecting a correlation card, running it, and verifying the result appears in `Banco di prova`.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, `e2e/correlation_cards_walk.mjs` passes, spec status updated, summary written.
