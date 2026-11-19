# 329 — CARD-CHECKS-1: Correlation and property cards record their validity checks, so their runs can be usable

Status: merged
Depends on: 280, 315, 328

## Goal

A correlation-card run made from the Runs screen (pipe pressure drop, internal convection, pure-fluid properties)
gets a verdict of `usable` when its inputs lie inside the card's published validity range and its outputs are
finite. Today these runs are always `unchecked`, so no card run can turn the readiness board's `Calcolo` cell green
or carry a case comparison and verdict.

## Why

The case walk of spec 328 (`e2e/case_walk.mjs`) reached `Ingressi legati` green on the fake case and stopped at
`Calcolo`: the CoolProp card returned the expected density, but the run was "Non verificato" and the board needs
`usable` or `usable_with_warnings`. These evaluators already refuse any input outside the published range, so every
succeeded result has passed a real range check. The check is simply never recorded. Recording it is honest evidence,
not relabelling: the verdict rule of spec 280 is unchanged, and an evaluator with no required checks stays
`unchecked`.

The same walk recorded one console error on the workspace creation form, which blocks its criterion 6.

## Code facts checked at `bd996269`

- `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` returns `unchecked` when
  `descriptor.required_checks` is empty, `usable_with_warnings` on any `warn`, otherwise `usable` when every required
  check is present and not `not_evaluated`. `ResultCheck` has `id`, `family` (`input`, `numerical`, ...), `status`,
  `actual`, `limit`, `unit`, `hint` and `source` (pattern `physical_limit`, `policy:...`, `constant:...` or
  `value_store:...`).
- `backend/app/modules/process_stack/correlations.py`: `PipePressureDropEvaluator` refuses non-positive inputs,
  negative roughness, the transitional regime `LAMINAR_TRANSITION_PIPE <= Re < TURBULENT_RE_MIN` and
  `e/D > MAX_RELATIVE_ROUGHNESS`. `InternalConvectionEvaluator` refuses non-positive inputs and (Re, Pr) outside
  `CONVECTION_METHODS[method]`. Neither descriptor declares checks; results carry none.
- `backend/app/modules/process_stack/properties.py::CoolPropPropertyEvaluator` refuses states outside
  `Tmin <= T <= Tmax`, `0 < p <= pmax`, two-phase or unknown phases and non-finite outputs; no checks declared.
- `backend/app/modules/engineering/marine_motion.py::RigidBodyWavesEvaluator.evaluate` is the existing pattern:
  `evaluate_with(...)`, then on `succeeded` attach `ResultCheck`s with `model_copy(update={"checks": ...})`.
- `backend/tests/test_engineering_bench.py::test_registry_evaluator_valid_and_invalid_run` asserts the pipe run is
  `unchecked` with exit code 1, with the comment that registry evaluators declare no required checks; it must change
  with this spec.
- `frontend/src/components/WorkspaceBootstrap.tsx` slug input has `pattern="[a-z0-9][a-z0-9-]*"`; browsers compile
  `pattern` with the `v` flag, where an unescaped trailing `-` in a class is a syntax error, logged to the console.

## Scope

In scope:
- **Two required checks on each of the three evaluators**, declared in the descriptor and recorded on every
  succeeded result:
  - `VALIDITY_RANGE` (family `input`, source `constant:<evaluator id>`): `pass`, with `actual` the governing input
    (Re for pipe and convection, T for properties), `limit` the nearest bound of the range and a hint naming the
    range, the same range the evaluator already enforces (no new or different bound).
  - `FINITE_OUTPUTS` (family `numerical`, source `physical_limit`): `pass` when every output is finite, else `fail`.
- One small helper shared by the three (for example in `backend/app/modules/process_stack/_common.py`) that builds
  the two checks; the range values come from the constants already in `correlations.py` and the CoolProp limits
  already read in `_properties`, not duplicated literals.
- Refused, failed, expired and cancelled results stay as they are (no checks, verdict `not_usable`).
- `WorkspaceBootstrap.tsx`: slug pattern written so it is valid under the `v` flag (for example
  `[a-z0-9][a-z0-9\-]*`), same accepted set.
- Update `test_engineering_bench.py` for the new verdict and exit code, and the frontend test that asserts the
  pattern literal if any.

Out of scope (binding non-goals):
- No change to `result_verdict`, to the verdict vocabulary or to any other evaluator.
- No new range, tolerance or physics; no warning band invented inside the published range.
- No migration; stored older runs keep their stored verdict.

## Files likely touched

- `backend/app/modules/process_stack/correlations.py`, `backend/app/modules/process_stack/properties.py`,
  `backend/app/modules/process_stack/_common.py`
- `backend/tests/test_engineering_bench.py`, a new `backend/tests/test_card_checks_329.py`
- `frontend/src/components/WorkspaceBootstrap.tsx` and its frontend test if one asserts the pattern

## Design constraints

- Checks are computed from the actual inputs and outputs of the run; never a constant `pass` without the
  comparison behind it.
- Same evaluator results otherwise (outputs, units, refusals byte-identical for the same inputs).

## Acceptance criteria

1. Each card with its default fields gives a succeeded result with `VALIDITY_RANGE` and `FINITE_OUTPUTS` both `pass`
   and verdict `usable`.
2. Inputs outside the range are still refused with the same backend codes as today, verdict `not_usable`.
3. A succeeded result whose output is non-finite (forced in a test by monkeypatching the library call) gives
   `FINITE_OUTPUTS` `fail` and verdict `not_usable`.
4. `bench.py run fluids.pipe_pressure_drop` on the existing valid request exits 0 with verdict `usable`.
5. The workspace creation form logs no console error; a slug with uppercase or a leading hyphen is still rejected
   by the server as today.
6. `e2e/case_walk.mjs` on the fake pack gets past `Calcolo` (rerun by the coordinator on the spec-328 branch).

## Required tests

- `backend/tests/test_card_checks_329.py`, one test per criterion 1–4 for each evaluator where it applies.
- Existing evaluator, bench, bench-card and readiness tests pass.

## Definition of done

Test gate green (see `AGENTS.md`), criteria met, spec status updated. Risk: medium (feature code with tests). The
case walk of spec 328 is the browser evidence for the changed verdicts.
