# 324 — MOTION-WAVES-1: Rigid-body motion in waves and mooring evaluator

Status: ready
Depends on: none

## Goal

In Wave 4 of the capability map (directives `L20261116160140-7608` and `L20261116205733-a728` B), add a deterministic 3-DOF (surge, heave, pitch) linear frequency-domain rigid-body motion and quasi-static mooring evaluator (`marine_motion.py`, evaluator id `marine.rigid_body_waves`), registered in `evaluator_registry` and exposed as a `BenchCard` in `bench_cards.py` so it is runnable from `bench`, agent tools, and the Runs screen (`Banco di prova`).

## Why

The floating-body literature case requires predicting surge, heave, pitch response amplitude operators (RAOs), natural periods, peak accelerations, and mooring restoring tension in regular waves. Exposing it through `evaluator_registry` and `bench_cards.py` makes it immediately reachable from the UI and usable for readiness-board comparisons without new dependencies.

## Code facts checked at `2baf47e8`

- `backend/app/modules/engineering/operator_service.py::evaluator_registry` returns the active `EngineeringEvaluator` mapping used by `backend/app/modules/engineering/bench.py::run_evaluation`.
- `backend/app/modules/engineering/evaluator_contracts.py::EngineeringEvaluator` defines the evaluator protocol (`descriptor`, `availability`, `evaluate`).
- `backend/app/modules/engineering/bench_cards.py::BenchCard`, `backend/app/modules/engineering/bench_cards.py::list_cards`, and `backend/app/modules/engineering/bench_cards.py::run_card` expose static correlation/engineering cards on the Runs screen.

## Scope

In scope:
- New pure-Python/NumPy module `backend/app/modules/engineering/marine_motion.py`:
  - `RigidBodyWavesEvaluator` implementing `EngineeringEvaluator` with `evaluator_id = "marine.rigid_body_waves"`.
  - Inputs (`EvaluationRequest.inputs`):
    - `mass_kg` ($> 0$), `added_mass_surge_kg` ($\ge 0$), `added_mass_heave_kg` ($\ge 0$), `pitch_inertia_kg_m2` ($> 0$), `waterplane_area_m2` ($> 0$), `gm_pitch_m` ($> 0$), `damping_surge_n_s_m` ($\ge 0$), `damping_heave_n_s_m` ($\ge 0$), `damping_pitch_n_m_s_rad` ($\ge 0$), `mooring_kx_n_m` ($\ge 0$), `mooring_kz_n_m` ($\ge 0$), `wave_amplitude_m` ($\ge 0$), `wave_period_s` ($> 0$), `water_density_kg_m3` ($> 0$, default `1025.0`), `gravity_m_s2` ($> 0$, default `9.80665`).
  - Physics (linear 1-DOF uncoupled frequency-response per mode with Froude–Krylov + restoring excitation):
    - $\omega = 2\pi / T$, $k = \omega^2 / g$, displaced volume $V_{\text{disp}} = m / \rho$.
    - Natural periods: $T_{n,\text{heave}} = 2\pi \sqrt{(m + m_{a,z}) / (\rho g A_w + k_z)}$ and $T_{n,\text{pitch}} = 2\pi \sqrt{I_{yy} / (\rho g V_{\text{disp}} GM_L)}$.
    - Complex transfer function $H(\omega) = 1 / ((K - \omega^2 M_{\text{tot}}) + i \omega B)$ for surge ($K_x = k_x$, $F_x = \rho g V_{\text{disp}} k A$, when $k_x > 0$ or $\omega > 0$), heave ($K_z = \rho g A_w + k_z$, $F_z = \rho g A_w A$), and pitch ($K_\theta = \rho g V_{\text{disp}} GM_L$, $M_\theta = \rho g V_{\text{disp}} GM_L k A$).
    - Outputs (`NamedQuantity` SI): `heave_rao` (1), `surge_rao` (1), `pitch_rao` (rad/m), `heave_amplitude` (m), `surge_amplitude` (m), `pitch_amplitude` (rad), `heave_natural_period` (s), `pitch_natural_period` (s), `heave_acceleration` (m/s^2), `mooring_tension` (N).
    - Required checks (`FINITE_RESPONSE`, `POSITIVE_STIFFNESS`) producing `usable` verdict on valid inputs.
- Register `"marine.rigid_body_waves"` in `operator_service.py::evaluator_registry`.
- Expose `"marine.rigid_body_waves"` as a `BenchCard` in `bench_cards.py` with Italian-friendly labels and SI defaults.

Out of scope (binding non-goals):
- 6-DOF coupled BEM/hydrodynamic diffraction panels or irregular sea-state spectral integration.
- Time-domain CFD / free-surface coupling.
- Frontend code changes (`BenchRunsPanel.tsx` renders `list_cards()` dynamically).

## Files likely touched

- `backend/app/modules/engineering/marine_motion.py` (new)
- `backend/app/modules/engineering/operator_service.py`
- `backend/app/modules/engineering/bench_cards.py`
- `backend/tests/test_motion_waves_324.py` (new)

## Design constraints

- No new Python package dependencies (`math` and standard library only).
- Deterministic, pure evaluation; all nonpositive mass/inertia/period/waterplane inputs fail cleanly with `EvaluationFailure` rather than NaN/Inf.
- Note: `bench_cards.py` and `test_bench_cards_*.py` tests that assert card counts must remain consistent (update or verify existing card-list assertions if any).

## Acceptance criteria

1. `evaluator_registry()` includes `"marine.rigid_body_waves"` and `bench.run_evaluation("marine.rigid_body_waves", ...)` returns `usable` with all 10 named outputs.
2. At resonance ($\omega = \omega_{n,\text{heave}}$), `heave_rao` equals $\frac{\rho g A_w}{\omega_{n,\text{heave}} B_z}$ within `1e-9` and peaks above off-resonance frequencies.
3. Increasing `mooring_kx_n_m` reduces `surge_amplitude` at low frequency ($\omega \ll \omega_n$) and increases `mooring_tension`; zero `wave_amplitude_m` yields zero motion and tension.
4. `list_cards()` includes `"marine.rigid_body_waves"` and `run_card()` executes it into a workspace bench run.

## Required tests

- `backend/tests/test_motion_waves_324.py`:
  - Exact resonance RAO check ($\omega = \omega_{n,\text{heave}}$) and quasi-static limit ($\omega \to 0$, `heave_rao -> rho*g*Aw / (rho*g*Aw + kz)`).
  - Mooring stiffness sensitivity check and zero-wave check.
  - `bench.run_evaluation("marine.rigid_body_waves", ...)` and `bench_cards.run_card(...)` integration test.
  - Validation test rejecting nonpositive `mass_kg` or `wave_period_s`.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
