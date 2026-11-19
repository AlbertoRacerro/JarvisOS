# 321 — CULTURE-TEMPERATURE-STATE-1: Culture temperature as a dynamic state

Status: ready
Depends on: none

## Goal

In Wave 2 of the capability map (directives `L20261116160140-7608` and `L20261116205733-a728` B), `PhotobioreactorT1` and `HoldupTank` gain an opt-in energy-balance mode (`thermal_mode = "state"`) where culture temperature $T$ (in K) is integrated as a dynamic state variable in the existing ODE state vector rather than prescribed from `temperature_source` or fixed at `unit["params"]["temperature"]`. The computed temperature feeds `Growth.rates_at`, advects and mixes by flow-weighted enthalpy across streams and algebraic units (`Mixer`, `Splitter`, `Pump`, `Recycle`, `SpecifiedSeparator`), exchanges heat with the environment ($U A (T_{\text{env}}(t) - T) + Q_{\text{ext}}$), and is emitted in the result series.

## Why

The first literature cases assumed a prescribed culture temperature. Cases with a water bath, outdoor heat exchange, or thermal transients need culture temperature to evolve as a balance state coupled to flow, ambient forcing, and biological growth kinetics, without breaking existing prescribed-temperature scenarios.

## Code facts checked at `7ddd8a0e`

- `backend/app/modules/process_stack/dynamic_models.py::Scenario` defines `temperature_source`, `initial`, `feed_flows`, and `circulation` with `ConfigDict(extra="forbid", strict=True)`.
- `backend/app/modules/process_stack/dynamic_engine.py::_slot` indexes `STATE_STRIDE = 7` slots per culture unit; slot 6 is currently unused (`0.0` in `rhs`).
- `backend/app/modules/process_stack/dynamic_engine.py::_profile_temperature` evaluates the prescribed temperature at `elapsed_s` from `sea_temperature`, `air_temperature`, or `unit_mean`.
- `backend/app/modules/process_stack/dynamic_engine.py::_network_concentrations` propagates `(X, N, O2, quota*X)` across `PhotobioreactorT1`, `HoldupTank`, `Mixer`, `Splitter`, `Pump`, `Recycle`, and `SpecifiedSeparator`.
- `backend/app/modules/process_stack/dynamic_engine.py::_valid_state` validates finite states and nonnegative `(X, N, O2)`.
- `backend/app/modules/process_stack/dynamic_engine.py::_result_series` builds time-series outputs per unit and per `culture_loops` entry.
- `backend/app/modules/process_stack/dynamic_engine.py::prepare` and `backend/app/modules/process_stack/dynamic_engine.py::run` build the pinned `Snapshot` and integrate the ODE system.
- `backend/app/modules/process_stack/pbr_unit.py::Growth.rates_at` evaluates hourly biological growth and loss rates at a given `(par, temperature, biomass, nitrogen)`.

## Scope

In scope:
- Extend `Scenario` in `dynamic_models.py` with backward-compatible defaults:
  - `thermal_mode: Literal["prescribed", "state"] = "prescribed"`
  - `thermal_ua: dict[str, float] = Field(default_factory=dict)` (overall heat transfer coefficient × area $UA$ in W/K per unit tag, $\ge 0$)
  - `thermal_heat_w: dict[str, float] = Field(default_factory=dict)` (constant external heat input $Q_{\text{ext}}$ in W per unit tag)
  - `water_density_kg_m3: float = Field(default=1000.0, gt=0)`
  - `water_cp_j_kg_k: float = Field(default=4184.0, gt=0)`
- In `dynamic_engine.py`, when `thermal_mode == "state"`:
  - Initialize slot 6 (`_slot(i, 6)`) for each culture unit from `scenario.initial[tag]["T"]` (must be `> 0` K if given) or fall back to `_profile_temperature(snapshot, 0.0, i)` for `PhotobioreactorT1` and `item["params"]["temperature"]` for `HoldupTank`.
  - Resolve feed stream temperature from `stream["spec"]["temperature"]["si"]` (defaulting to `298.15` K when absent; must be `> 0` K).
  - Propagate temperature as the 5th concentration channel in `_network_concentrations` (flow-weighted average in `Mixer`; preserved across `Splitter`, `Pump`, `Recycle`, `SpecifiedSeparator`).
  - Integrate `dT_i/dt = (Q_in / V_i) * (T_in - T_i) + (UA_i * (T_env(t) - T_i) + Q_ext_i) / (rho * cp * V_i)` in `rhs`, where `T_env(t)` is `_profile_temperature(snapshot, elapsed, i)`.
  - Pass `T_i = state[_slot(i, 6)]` to `growth.rates_at` for both `PhotobioreactorT1` and `HoldupTank`.
  - Validate `T_i > 0` in `_valid_state` (raising `DynamicError("STATE_NONPHYSICAL", ...)` if `<= 0`).
  - Emit `<unit_tag>_T_K` and `<loop_id>_T_mean_K` (volume-weighted) in `_result_series`.
- When `thermal_mode == "prescribed"` (the default), behaviour is unchanged (`dT/dt = 0.0`, `Growth.rates_at` uses `_profile_temperature` / `HoldupTank` parameter).

Out of scope (binding non-goals):
- Carbon speciation / pH state (separate Wave 2 spec).
- Dynamic `HeatExchanger` UA/LMTD coupling inside the ODE (future slice if needed).
- Changes to `PhotobioreactorT1` required-parameter schema in `draft_models.py` or database migrations.
- Frontend UI changes.

## Files likely touched

- `backend/app/modules/process_stack/dynamic_models.py`
- `backend/app/modules/process_stack/dynamic_engine.py`
- `backend/tests/test_culture_temperature_state_321.py`

## Design constraints

- Default `thermal_mode = "prescribed"` must preserve bit-for-bit behaviour for all existing dynamic scenarios and tests.
- All thermal parameters on `Scenario` (`thermal_ua`, `thermal_heat_w`, `water_density_kg_m3`, `water_cp_j_kg_k`) must be validated in `prepare` (negative `thermal_ua` rejected).
- Never mutate `STATE_STRIDE` or break `_clip_tiny_negatives` (temperature is not clipped to 0; nonpositive temperature is an integration failure in `_valid_state`).

## Acceptance criteria

1. `Scenario` accepts `thermal_mode="prescribed"` (default) and `thermal_mode="state"` with `thermal_ua`, `thermal_heat_w`, `water_density_kg_m3`, and `water_cp_j_kg_k`.
2. In `thermal_mode="state"` with zero flow and `UA > 0`, a unit initialized at $T_0 \neq T_{\text{env}}$ relaxes exponentially toward constant $T_{\text{env}}$ with time constant $\tau = \rho c_p V / (UA)$, matching the analytical solution within numerical tolerance.
3. In `thermal_mode="state"` with flow through a `Mixer` or two-unit loop, outlet/mixed temperature follows flow-weighted enthalpy conservation and `Growth.rates_at` responds to the computed culture temperature.
4. `_result_series` emits `<unit_tag>_T_K` and `<loop_id>_T_mean_K` when `thermal_mode="state"`, and existing `thermal_mode="prescribed"` tests remain unchanged and green.

## Required tests

- `backend/tests/test_culture_temperature_state_321.py`:
  - Analytical relaxation test ($T(t) = T_{\text{env}} + (T_0 - T_{\text{env}}) e^{-t/\tau}$) for a batch unit with `UA > 0`.
  - Steady-state external heat input test ($T_{\text{ss}} = T_{\text{env}} + Q_{\text{ext}} / (UA)$).
  - Flow-weighted mixing and closed-loop temperature advection across `PhotobioreactorT1` + `HoldupTank` + `Mixer`, verifying `<unit_tag>_T_K`, `<loop_id>_T_mean_K`, and thermal coupling to growth rate.
  - Validation test rejecting negative `thermal_ua` or nonpositive initial `T`, and verifying `thermal_mode="prescribed"` default backward compatibility.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
