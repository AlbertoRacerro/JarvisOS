# 322 — CARBON-PH-STATE-1: Dissolved inorganic carbon, carbonate pH, and CO2 mass transfer in culture units

Status: merged
Depends on: 321

## Goal

In Wave 2 of the capability map (directives `L20261116160140-7608` and `L20261116205733-a728` B), add a deterministic carbonate speciation and pH solver (`carbonate.py`) and an opt-in `carbon_mode = "state"` mode on `Scenario` that tracks dissolved inorganic carbon (DIC, mol/m³) and total alkalinity (eq/m³) as dynamic states in `PhotobioreactorT1` and `HoldupTank`, with gas-liquid $\text{CO}_2$ transfer, biological carbon uptake, optional $\text{CO}_2$ growth limitation, stream advection/mixing, and pH/DIC output series.

## Why

The CO₂ column, pH-controlled cultivation, and outdoor photobioreactor cases require dissolved inorganic carbon and pH to evolve with gas exchange, biological uptake, and dilution. `culture.py` already carries `dic` and `ph` on steady-state stream metadata with a placeholder note for carbonate speciation; this spec adds the dynamic carbonate chemistry without new dependencies or breaking existing runs.

## Code facts checked at `1058fe4d`

- `backend/app/modules/process_stack/culture.py::mix_mass_specific` and `backend/app/modules/process_stack/culture.py::propagate` already carry `dic` (`mol/m3`), `salinity` (`g/kg`), and `ph` on steady-state culture streams.
- `backend/app/modules/process_stack/dynamic_models.py::Scenario` defines `StrictModel` scenario inputs with `ConfigDict(extra="forbid", strict=True)`.
- `backend/app/modules/process_stack/dynamic_engine.py::_network_concentrations` propagates unit outlet concentrations through `Mixer`, `Splitter`, `Pump`, `Recycle`, and `SpecifiedSeparator`.
- `backend/app/modules/process_stack/dynamic_engine.py::_result_series` emits unit and loop time-series arrays.
- `backend/app/modules/process_stack/dynamic_engine.py::prepare` and `backend/app/modules/process_stack/dynamic_engine.py::run` build the pinned snapshot and integrate the ODE system.
- `backend/app/modules/process_stack/pbr_unit.py::Growth.rates_at` evaluates hourly biological growth and loss rates.

## Scope

In scope:
- New pure-Python module `backend/app/modules/process_stack/carbonate.py` (standard library `math` + `scipy.optimize.brentq` already in `backend/requirements.txt`):
  - Temperature- and salinity-dependent equilibrium constants $K_1(T, S)$, $K_2(T, S)$, $K_w(T, S)$ and Henry's law $\text{CO}_2$ solubility $K_0(T, S)$.
  - `solve_ph(dic_mol_m3, alkalinity_eq_m3, temperature_k, salinity_g_kg=0.0)` returning a dict/dataclass with `ph`, `h_mol_m3`, `co2_aq_mol_m3`, `hco3_mol_m3`, `co3_mol_m3`.
  - `alkalinity_from_ph(dic_mol_m3, ph, temperature_k, salinity_g_kg=0.0)` to initialize alkalinity from a stream/unit's `(dic, ph)`.
  - `co2_saturation_mol_m3(p_co2_atm, temperature_k, salinity_g_kg=0.0)` using $K_0(T, S) p_{\text{CO}_2}$.
- Extend `Scenario` in `dynamic_models.py` with backward-compatible defaults:
  - `carbon_mode: Literal["off", "state"] = "off"`
  - `carbon_kla_h: dict[str, float] = Field(default_factory=dict)` ($\text{CO}_2$ $k_L a$ in $\text{h}^{-1}$ per unit tag, $\ge 0$)
  - `co2_partial_pressure_atm: dict[str, float] = Field(default_factory=dict)` (gas-phase $p_{\text{CO}_2}$ in atm per unit tag, $\ge 0$, default `0.00042`)
  - `biomass_carbon_fraction: float = Field(default=0.50, gt=0, le=1)` (kg C / kg dry biomass)
  - `co2_half_saturation_mol_m3: float = Field(default=0.0, ge=0)` (optional Monod $\text{CO}_2$ limitation factor $[\text{CO}_2^*]/(K_{\text{CO}_2} + [\text{CO}_2^*])$ when $> 0$)
  - `salinity_g_kg: float = Field(default=0.0, ge=0)`
- In `dynamic_engine.py`, when `carbon_mode == "state"`:
  - Append 2 state slots per culture unit (`dic` in mol/m³, `alkalinity` in eq/m³) at the end of the base state vector (leaving `STATE_STRIDE = 7` untouched when `carbon_mode == "off"`).
  - Initialize `(dic, alkalinity)` per unit from `scenario.initial[tag]` (`dic`, and `alkalinity` or `ph` via `alkalinity_from_ph`).
  - Advect and mix `dic` and `alkalinity` across streams and algebraic units (`Mixer`, `Splitter`, `Pump`, `Recycle`, `SpecifiedSeparator`).
  - In `rhs`: compute $[\text{CO}_2^*]$ and pH from `(dic, alkalinity, T, salinity)`; apply $\text{CO}_2$ gas-liquid transfer `kla_co2_s * (co2_sat - co2_aq)` to `dic`, biological carbon uptake `-(biomass_carbon_fraction / 0.012011) * rx` to `dic`, and nitrate-assimilation alkalinity shift `+(growth.nitrogen_quota / 0.0140067) * rx` to `alkalinity`; when `co2_half_saturation_mol_m3 > 0`, scale `mu` by `co2_aq / (K_co2 + co2_aq)`.
  - Validate `dic >= 0` in `_valid_state`.
  - Emit `<unit_tag>_dic_mol_m3`, `<unit_tag>_pH`, `<unit_tag>_co2_aq_mol_m3`, `<loop_id>_dic_mean_mol_m3`, and `<loop_id>_pH_mean` in `_result_series`.

Out of scope (binding non-goals):
- Updating `culture.py::mix_mass_specific` steady-state DWSIM pass-through (separate slice if needed).
- Dynamic acid/base dosing controllers or pH-stat actuators (follow-up slice).
- Frontend UI changes or database migrations.

## Files likely touched

- `backend/app/modules/process_stack/carbonate.py` (new)
- `backend/app/modules/process_stack/dynamic_models.py`
- `backend/app/modules/process_stack/dynamic_engine.py`
- `backend/tests/test_carbon_ph_state_322.py` (new)

## Design constraints

- Default `carbon_mode = "off"` must preserve bit-for-bit behaviour for all existing dynamic scenarios and tests.
- No new Python package dependencies (`math` and `scipy.optimize.brentq` only).
- `carbonate.py` must be pure, deterministic, and validate finite nonnegative inputs (`dic >= 0`, `temperature_k > 0`, `salinity_g_kg >= 0`, `0 <= ph <= 14`).

## Acceptance criteria

1. `carbonate.py` solves freshwater and seawater carbonate speciation and pH (`solve_ph`, `alkalinity_from_ph`, `co2_saturation_mol_m3`) consistently: `solve_ph(dic, alkalinity_from_ph(dic, ph, T, S), T, S)["ph"]` round-trips `ph` within `1e-6`, and speciation fractions satisfy $\alpha_0 + \alpha_1 + \alpha_2 = 1$.
2. In `carbon_mode="state"` with `carbon_kla_h > 0` and zero growth/flow, `dic` relaxes toward `co2_saturation` + bicarbonate/carbonate equilibrium at constant alkalinity, and pH shifts accordingly.
3. In `carbon_mode="state"` with biological growth, `dic` decreases by `(biomass_carbon_fraction / 0.012011) * dX` in a closed batch unit, and `co2_half_saturation_mol_m3 > 0` limits growth when $[\text{CO}_2^*]$ is low.
4. `_result_series` emits `<unit_tag>_dic_mol_m3`, `<unit_tag>_pH`, `<unit_tag>_co2_aq_mol_m3`, `<loop_id>_dic_mean_mol_m3`, and `<loop_id>_pH_mean` when `carbon_mode="state"`, and existing `carbon_mode="off"` tests remain unchanged and green.

## Required tests

- `backend/tests/test_carbon_ph_state_322.py`:
  - `carbonate.py` unit tests: round-trip `alkalinity_from_ph` ↔ `solve_ph` across freshwater (`S=0`) and seawater (`S=35`), pH 5–10, mass-balance $\alpha_0 + \alpha_1 + \alpha_2 = 1$, and Henry's law saturation monotonicity with temperature.
  - Dynamic batch gas-liquid $\text{CO}_2$ equilibration test (`carbon_mode="state"`, `carbon_kla_h > 0`, `mu_max=0`) verifying DIC equilibration and constant alkalinity.
  - Dynamic batch growth test verifying carbon mass conservation (`Delta DIC = -(f_C / M_C) * Delta X`) and $\text{CO}_2$ limitation (`co2_half_saturation_mol_m3 > 0`).
  - Validation test rejecting negative `carbon_kla_h`, negative `co2_partial_pressure_atm`, and verifying `carbon_mode="off"` default backward compatibility.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
