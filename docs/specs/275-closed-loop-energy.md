# 275 — CLOSED-LOOP-ENERGY-1: closed circulation loops converge or block with a typed finding

Status: ready
Depends on: —

## Code facts checked on `origin/master` at `a5230593`

- `backend/app/modules/process_stack/draft.py::validate_document` and `backend/app/modules/process_stack/draft.py::_cycle_members`: pre-run validation inspects material loops via `mixed.recycle_free_cycles`, `mixed.validation_findings`, `culture.culture_findings`, and `pbr_validation.pbr_findings`, but does not check whether a closed loop with shaft work (`Pump`) or heating closes its energy balance.
- `backend/app/modules/process_stack/draft_models.py::UNIT_REGISTRY`: defines `Pump`, `Heater`, `Cooler`, `HeatExchanger`, `PhotobioreactorT1`, and `Recycle` with their modes (`outlet_temperature`, `heat_removed`, `heat_added`, `temperature_change`, `energy_stream`).
- `backend/app/modules/process_stack/mixed.py::components` and `backend/app/modules/process_stack/mixed.py::validation_findings`: computes strongly connected components on the material graph and emits mixed-solve pre-run findings.
- `backend/app/modules/process_stack/mixed_runtime.py::_evaluate`, `backend/app/modules/process_stack/mixed_runtime.py::run`, and `backend/app/modules/process_stack/mixed_runtime.py::_pressure_diagnosis`: already diagnoses falling loop pressure (`pressure falls by ... Pa per pass around the loop; add a pump`), but has no symmetric diagnosis for rising loop temperature or property-range temperature failures.
- `backend/app/modules/process_stack/pbr_unit.py::evaluate_pbr` and `backend/app/modules/process_stack/pbr_validation.py::pbr_findings`: Tier 1 `PhotobioreactorT1` passes inlet temperature through unchanged (`T1 has no energy balance`) and reports `PBR_HYDRAULICS_OUTSIDE_DOMAIN` if `temperature_mean` leaves CoolProp bounds.

## Goal

Steady-state solves of closed circulation loops containing pumps either converge cleanly (with energy closed by a temperature specification or heat sink) or are blocked before solving with a clear, typed finding naming the loop.

## Why

A closed circulation loop with a `Pump` (which adds shaft work per pass) and no heat sink or temperature-fixing unit has no steady-state energy closure: each tear iteration raises the loop temperature until properties leave their valid range or the solve diverges. Turning this into an explicit pre-run blocker and supporting closed-loop temperature closure makes closed-loop process flowsheets reliable.

## Scope

In scope:
- **Pre-run loop energy-closure check** (`backend/app/modules/process_stack/mixed.py::validation_findings` / `draft.py::validate_document`):
  - For each closed material loop (SCC from `mixed.components`), if the loop contains a work/heat-adding unit (`Pump`, or `Heater` in `heat_added`, `heat_added_removed`, or positive `temperature_change` mode) and contains no heat sink or temperature-fixing unit (`Cooler`, `HeatExchanger`, or `Heater` in `outlet_temperature` mode) and no closed-loop temperature specification, emit a typed `blocker` finding `LOOP_ENERGY_UNCLOSED` naming the loop units.
- **Closed-loop temperature / heat-loss closure**:
  - Allow a closed culture loop to close energy via a temperature-fixing unit (`Cooler` / `HeatExchanger` / `Heater` in `outlet_temperature` mode) or an explicit loop heat-loss / temperature specification so the steady tear solver converges without temperature runaway.
- **Runtime temperature-runaway / property-range diagnosis** (`backend/app/modules/process_stack/mixed_runtime.py`):
  - Add `_temperature_diagnosis(history)` (symmetric to `_pressure_diagnosis`) to report monotonic temperature runaway around a loop.
  - Map out-of-range temperature failures during tear or unit evaluation to a typed loop failure diagnosis rather than a raw property-range error.

Out of scope:
- Full thermal energy balance inside Tier 1 `PhotobioreactorT1` or outdoor water-body thermal coupling (separate specs).
- Changes to DWSIM's internal solver.

## Design constraints

- Deterministic code owns pre-run validation, tear convergence, and mass/energy balance checks; no AI model involvement.
- Existing open-loop and converged-loop conformance tests remain unchanged and passing.

## Acceptance criteria

1. A minimal closed-loop fixture (`Pump`, `PhotobioreactorT1` or tube unit, `Recycle`) with a temperature specification or heat sink converges in fewer than 25 tear iterations, and its mass balance closes within `1e-9` relative tolerance (test).
2. The same fixture with the temperature specification / heat sink removed is blocked before solving by `validate_document` with the typed `blocker` finding `LOOP_ENERGY_UNCLOSED` naming the loop (test).
3. When a runtime solve encounters monotonic temperature runaway or a property-range temperature failure in a loop, `mixed_runtime` reports a clear typed diagnosis naming the loop temperature rise (test).
4. `ruff check app tests` and relevant `process_stack` tests pass offline.
