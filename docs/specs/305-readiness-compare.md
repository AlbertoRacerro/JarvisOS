# 305 — READINESS-COMPARE-1: The comparison step of the readiness board, computed from a usable run and the published value

Status: merged
Depends on: 303, 304

## Goal

The fifth cell of the readiness board, "Confronto", stops being red by construction. For each case the board finds the
newest usable bench run of the case workspace whose outputs carry the case's compared quantity, converts that value
to the published unit, and computes the relative deviation from the published value. The cell is green when the
deviation is within the case tolerance and red otherwise, and in both states it shows the computed value, the
published value, the deviation, the tolerance and which run was used. Computed on read; nothing is stored or run.

## Why

Directive L20261116155611-930c point 2 (the board is the map of what is missing, then the test to pass) and directive
L20261116160140-7608 point 2 (comparison with the published value and a case verdict are needed by all nine cases and
least built). Today step 5 is red with `capability_missing` for every case, so no case can ever be fully green.
This is the smallest deterministic comparison that turns the cell into a real check; a stored case verdict as its own
record is left for when the guided path needs one (directive L20261116201429-63ee, compare step).

## Code facts checked at `e610621a`

- `backend/app/modules/engineering/readiness_board.py::_compute_case` sets step `compare` red with
  `REASON_CAPABILITY_MISSING`; `CaseDefinition.compare` (`CaseCompare`: `quantity`, `value`, `unit`, `rel_tol`) is
  already parsed from the pack case file and may be absent.
- `backend/app/modules/engineering/bench_runs_view.py::list_runs` lists the workspace bench runs newest first with
  `run_id` and `verdict`; `backend/app/modules/engineering/bench_runs_view.py::bench_root` gives the root; each run
  folder holds `result.json` whose `outputs` are `NamedQuantity` (`name`, `value` = `Quantity` with `value`, `unit`)
  per `backend/app/modules/engineering/evaluator_contracts.py::NamedQuantity` and
  `backend/app/modules/engineering/refs.py::Quantity`.
- `backend/app/modules/process_kernel/units.py::resolve_unit` and
  `backend/app/modules/process_kernel/units.py::unit_registry` are the single Pint owner (dimension and conversion).
- `frontend/src/pages/ReadinessBoard.tsx::ReadinessBoard` renders a cell detail from `StepResult` and maps reason
  codes to Italian sentences; `compare` has no link.

## Scope

In scope:
- `StepResult` gains an optional `comparison` object: `run_id`, `computed` (`value`, `unit` as stored), `computed_in_published_unit`,
  `published` (`value`, `unit`), `deviation` (relative, unsigned), `rel_tol`.
- The compare rule, in this order:
  1. No `compare` block in the case: red, reason `not_declared`.
  2. Published value zero, non-finite, or `rel_tol` not in (0, 1]: the case file entry is invalid and is reported as a
     board error at load; the cell is red with `not_declared`.
  3. Among the workspace runs from `list_runs` with verdict `usable` or `usable_with_warnings`, newest first, take the
     first whose `result.json` has an output named exactly `compare.quantity`. None: red, `missing_record`, missing
     names the quantity.
  4. Units not resolvable or of different dimension: red, reason `unit_mismatch`, with `comparison` filled except the
     converted value and deviation.
  5. Otherwise `deviation = |converted − published| / |published|`; green when `deviation <= rel_tol`, else red with
     reason `out_of_tolerance`; `comparison` filled in both cases.
- Screen: the "Confronto" detail shows "Calcolato", "Pubblicato", "Scarto" (percent, two significant figures),
  "Tolleranza" and the run, for green and red; Italian sentences for `not_declared` ("Il caso non dichiara un valore
  pubblicato da confrontare"), `unit_mismatch` ("Le unità del risultato e del valore pubblicato non sono confrontabili"),
  `out_of_tolerance` ("Il risultato è fuori dalla tolleranza del caso") and `missing_record` for compare ("Nessun
  risultato utilizzabile riporta la grandezza confrontata:"); red `missing_record`, `out_of_tolerance` and
  `unit_mismatch` link to Runs.
- The command `scripts/readiness_board.py` shows the deviation in the compare cell.

Out of scope (binding non-goals): storing a comparison or case verdict; choosing a run by hand; any tolerance other than
the case's relative one; fitting, or any judgement on how the run's inputs were obtained (a fitted run is not detected
here and the board says nothing about validity beyond the deviation); comparison of curves or series.

## Files likely touched

- `backend/app/modules/engineering/readiness_board.py`, `backend/tests/test_readiness_board_303.py` or a new
  `backend/tests/test_readiness_compare_305.py`, `scripts/readiness_board.py`
- `frontend/src/api/readinessBoard.ts`, `frontend/src/pages/ReadinessBoard.tsx`,
  `frontend/tests/304-readiness-board-screen.test.mjs` (or a new 305 test in the `build` chain)
- `e2e/readiness_board_walk.mjs`; `frontend/src/api/generated` and `docs/MODULES.md` if regenerated

## Design constraints

- Read only and deterministic; `result.json` read through the same bounded, root-confined path as `list_runs`; at most
  the 100 listed runs are inspected.
- Unit conversion only through `process_kernel/units.py`; no new unit table.
- Path-free payloads (run id is the folder name only).
- No case content in the repository: tests and walk use the fake pack's case and fixture runs.

## Acceptance criteria

1. A usable run whose output equals the published value within tolerance makes the cell green with the comparison
   filled; just outside the tolerance makes it red `out_of_tolerance`.
2. A run output in a convertible unit (for example g/cm3 against kg/m^3) is converted before the deviation.
3. An incompatible unit gives `unit_mismatch`; no usable run with the quantity gives `missing_record` naming it; a run
   with verdict `not_usable` or `unchecked` is never used.
4. The newest qualifying run is used when several qualify.
5. A case without `compare` gives `not_declared`; a zero published value or `rel_tol` outside (0, 1] is a board error.
6. The screen shows the four values for a green and for a red comparison and the Runs link on the red cell; the walk
   reaches a green "Confronto" from a fixture run placed in the temporary data root.
7. Board route and command still change nothing in the data root.

## Required tests

- Backend tests for criteria 1–5 and 7 with fixture run folders, offline, fake pack only.
- Frontend static test for the sentences and labels; the walk extended for criterion 6.

## Definition of done

Lint, focused tests and the frontend build pass; generated files current; CI green on the exact head; one severe
review of the diff (medium risk); browser walk passes at the exact head; STATUS row 305 moved to merged by the
implementation PR.
