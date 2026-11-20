# 338 — TUBE-OXYGEN-CARD-1: bench card for dissolved oxygen build-up along a tube

Status: ready
Depends on: —

## Goal

The engineering bench offers a card "Accumulo di ossigeno in tubo": for a tube in plug flow with a constant volumetric
oxygen production rate and no gas exchange inside the tube, it computes the outlet dissolved oxygen, the residence
time, and the longest tube that keeps the outlet below a chosen limit. A literature case can then be walked through
the existing case walk with this card.

## Why

Directive `L20261120133717-6f0b` step 1 (walk the readiness cases) and step 4 (missing physics cards, ordered by how
many cases they unblock). The case walk (`e2e/case_walk.mjs`) runs bench cards only, and the bench has four cards
(pipe pressure drop, internal convection, pure-fluid properties, rigid-body waves). The next literature case in the
walk order, and the permeable-wall case after it, start from the classic tubular design rule that sizes the tube
length from the oxygen balance `L = u·(C_max − C_in)/R_O2` (Molina Grima et al. 2001 and later tubular design
literature). No card computes it, so those walks stop at the run step.

## Code facts checked at `7f299dc6`

- `backend/app/modules/engineering/bench_cards.py::CARDS` lists `BenchCard` entries (card id, evaluator id, Italian
  title, formula, validity, source, numeric fields with label/unit/default, options).
- `backend/app/modules/engineering/operator_service.py::evaluator_registry` registers evaluators by id with kind,
  backend name and fidelity.
- `backend/app/modules/process_stack/correlations.py::PipePressureDropEvaluator` is the pattern: `descriptor`,
  `availability`, `evaluate` through `backend/app/modules/process_stack/_common.py::evaluate_card` with
  `required_inputs`, `quantities` and a `ValidityPoint`; refusals are `EvaluationRefusal`.
- `backend/app/modules/engineering/marine_motion.py::RigidBodyWavesEvaluator` is the pattern for an in-house model
  with no third-party package (availability always `available`, version is a model version string).
- `frontend/src/components/BenchRunsPanel.tsx` keeps an evaluator display-name map (`EVALUATOR_NAMES`).
- Tests: `backend/tests/test_correlation_cards_315.py`, `backend/tests/test_card_checks_329.py`.

## Scope

In scope:
- Evaluator `bioprocess.tube_oxygen_buildup` (in-house, screening fidelity, required checks `VALIDITY_RANGE`,
  `FINITE_OUTPUTS`), in a new small module `backend/app/modules/process_stack/oxygen_tube.py` or next to the
  correlations, registered in `evaluator_registry`.
- Inputs (SI, all required): `velocity` m/s, `oxygen_production_rate` mol/(m3*s), `saturation_concentration`
  mol/m3 (dissolved O2 at air saturation for the culture conditions), `inlet_saturation_fraction` (1 = 100 % of air
  saturation), `max_saturation_fraction`, `length` m.
- Outputs: `outlet_saturation_fraction` = inlet + R·L/(u·C_sat); `outlet_concentration` mol/m3;
  `residence_time` s = L/u; `max_length` m = u·(max − inlet)·C_sat/R.
- Refusals: any of velocity, rate, saturation concentration, length not positive; inlet fraction negative; max
  fraction not greater than inlet fraction. Validity point: outlet fraction against the max fraction, hint stating
  the assumptions (plug flow, constant rate, no transfer to gas in the tube). The card uses `evaluate_card` unchanged.
- Card in `CARDS` titled `Accumulo di ossigeno in tubo`, Italian labels, defaults of a generic example
  (u 0.5 m/s, R 0.003 mol/(m3*s), C_sat 0.25 mol/m3, inlet 1.0, max 3.0, length 50 m).
- `EVALUATOR_NAMES` entry `"bioprocess.tube_oxygen_buildup": "Accumulo di ossigeno in tubo"`.

Out of scope (binding non-goals):
- Gas exchange along the tube, axial dispersion, light-dependent rates, temperature-dependent saturation (the
  saturation concentration is an input; the pure-fluid card or the literature gives it).
- Any process-flowsheet unit change.

## Files likely touched

- `backend/app/modules/process_stack/oxygen_tube.py` (new), `backend/app/modules/process_stack/module.toml` if it lists
  entrypoints
- `backend/app/modules/engineering/operator_service.py`, `backend/app/modules/engineering/bench_cards.py`
- `frontend/src/components/BenchRunsPanel.tsx` and the static test that checks evaluator names, if any
- `backend/tests/test_tube_oxygen_card_338.py` (new)

## Design constraints

- Pure arithmetic, no new dependency.
- Same run, check and verdict path as the other cards (no special case in routes).

## Acceptance criteria

1. The card catalog lists `bioprocess.tube_oxygen_buildup` with the six fields and the Italian title.
2. With u = 0.5, R = 0.003, C_sat = 0.25, inlet 1.0, max 3.0, L = 50: outlet fraction 2.2, outlet concentration
   0.55 mol/m3, residence time 100 s, max length 83.333 m (relative 1e-9); the run succeeds with checks passing.
3. A length beyond `max_length` still succeeds (it is a design outcome, not a model violation): the outlet fraction
   exceeds the max fraction in the outputs, and `VALIDITY_RANGE` reports actual = outlet fraction against limit = max
   fraction with the assumptions as hint, as `process_stack/_common.py::card_checks` does for every card.
4. Each refusal case above returns the refusal, not a number.
5. The run is reachable from the bench screen and the case walk: a generic run through the existing bench run route
   stores a run with this evaluator id (route test).

## Required tests

- `backend/tests/test_tube_oxygen_card_338.py` for criteria 1–5; existing card tests stay green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
