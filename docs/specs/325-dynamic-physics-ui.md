# 325 — DYNAMIC-PHYSICS-UI-1: Expose thermal state, carbonate/pH, and flat-wall optics fields in ScenarioEditor

Status: ready
Depends on: 321, 322, 323

## Goal

Surface the dynamic physics `Scenario` fields added by specs 321 (thermal state mode), 322 (carbonate/pH state mode), and 323 (flat-slab optics and wall fouling) in the Process dynamic studio (`ScenarioEditor.tsx` and `dynamicLogic.ts`), so operators can configure thermal energy balance, dissolved inorganic carbon / pH dynamics, and PBR wall optics from the UI without editing JSON by hand.

## Why

Maintainer directives `L20261116201429-63ee` and `L20261117071737-1216` require capabilities built in the backend to be reachable and usable from the front end. Specs 321, 322, and 323 extended `dynamic_models.Scenario` with opt-in physics modes and per-unit optical/thermal/carbonate parameters, while `ScenarioEditor.tsx` still only edits the original baseline fields (`temperature_source`, `par_scale`, `X`/`N`/`O2` initial state, `feed_flows`, `circulation`, `downstream_*`).

## Code facts checked at `72b74128`

- `backend/app/modules/process_stack/dynamic_models.py::Scenario` defines `thermal_mode`, `thermal_ua`, `thermal_heat_w`, `water_density_kg_m3`, `water_cp_j_kg_k`, `optics_geometry`, `back_wall_albedo`, `wall_transmittance`, `wall_fouling_rate_1_s`, and `wall_fouling_floor` (and spec 322 adds `carbon_mode`, `carbon_kla_h`, `co2_partial_pressure_atm`, `biomass_carbon_fraction`, `co2_half_saturation_mol_m3`, `salinity_g_kg`).
- `backend/app/modules/process_stack/dynamic_engine.py::prepare` accepts optional `"T"` (Kelvin, `> 0`) in `scenario.initial[tag]` alongside `"X"`, `"N"`, and `"O2"` (and spec 322 adds `"dic"` and `"alkalinity"` / `"ph"`).
- `frontend/src/components/dynamic/dynamicLogic.ts::DynScenario` types the scenario form state.
- `frontend/src/components/dynamic/dynamicLogic.ts::contentOf` hydrates omitted backend defaults via `scenarioDefaults`.
- `frontend/src/components/dynamic/dynamicLogic.ts::scenarioHints` computes non-authoritative client-side validation hints.
- `frontend/src/components/dynamic/dynamicLogic.ts::blankScenario` constructs a new scenario with default field values.

## Scope

In scope:
- Extend `DynScenario`, `scenarioDefaults()`, and `blankScenario()` in `frontend/src/components/dynamic/dynamicLogic.ts` with backward-compatible defaults matching `dynamic_models.Scenario`:
  - Thermal (spec 321): `thermal_mode` (`"prescribed"` | `"state"`, default `"prescribed"`), `thermal_ua` (`{}`), `thermal_heat_w` (`{}`), `water_density_kg_m3` (`1000`), `water_cp_j_kg_k` (`4184`).
  - Carbonate/pH (spec 322): `carbon_mode` (`"off"` | `"state"`, default `"off"`), `carbon_kla_h` (`{}`), `co2_partial_pressure_atm` (`{}`), `biomass_carbon_fraction` (`0.5`), `co2_half_saturation_mol_m3` (`0`), `salinity_g_kg` (`0`).
  - Optics & fouling (spec 323): `optics_geometry` (`{}`), `back_wall_albedo` (`{}`), `wall_transmittance` (`{}`), `wall_fouling_rate_1_s` (`{}`), `wall_fouling_floor` (`{}`).
- Extend `scenarioHints()` in `dynamicLogic.ts` with non-authoritative checks matching backend constraints (`water_density_kg_m3 > 0`, `water_cp_j_kg_k > 0`, `biomass_carbon_fraction` in `(0, 1]`, `co2_half_saturation_mol_m3 >= 0`, `salinity_g_kg >= 0`, `back_wall_albedo` in `[0, 1]`, `wall_transmittance` in `(0, 1]`, `wall_fouling_rate_1_s >= 0`, `wall_fouling_floor` in `[0, 1]`, `thermal_ua >= 0`, `carbon_kla_h >= 0`, `co2_partial_pressure_atm >= 0`).
- Update `frontend/src/components/dynamic/ScenarioEditor.tsx`:
  - Add a **Thermal** fieldset with `Thermal mode` (`prescribed` / `state`); when `thermal_mode === "state"`, show `Water density [kg/m3]`, `Water Cp [J/(kg*K)]`, per-unit `Thermal UA of <tag> [W/K]`, `External heat of <tag> [W]`, and optional `Initial T of <tag> [K]` inside `Initial state`.
  - Add a **Carbonate & pH** fieldset with `Carbon mode` (`off` / `state`); when `carbon_mode === "state"`, show `Biomass carbon fraction`, `CO2 half-saturation [mol/m3]`, `Salinity [g/kg]`, per-unit `CO2 kLa of <tag> [1/h]`, `CO2 partial pressure of <tag> [atm]`, and optional per-unit `Initial dic [mol/m3]`, `Initial alkalinity [eq/m3]`, and `Initial ph` inputs inside `Initial state`.
  - Add an **Optics & wall fouling (optional)** fieldset with per-unit `Optics geometry of <tag>` (`cylinder` / `flat_slab`), `Back-wall albedo of <tag>`, `Wall transmittance of <tag>`, `Wall fouling rate of <tag> [1/s]`, and `Wall fouling floor of <tag>`.
  - Omit `undefined` optional per-unit entries cleanly when saving (`setOp("scenario", value)`), preserving existing `"X"`, `"N"`, `"O2"` behavior.

Out of scope (binding non-goals):
- Backend changes to `dynamic_models.py`, `dynamic_engine.py`, or `carbonate.py`.
- Database migrations or API route changes.
- Changes to `RunResultsView` / `SyncCharts` (already plot any series emitted by `dynamic_engine._result_series`).

## Files likely touched

- `frontend/src/components/dynamic/dynamicLogic.ts`
- `frontend/src/components/dynamic/ScenarioEditor.tsx`
- `frontend/tests/173-process-dynamic.mjs`

## Design constraints

- Default values on a newly created or hydrated scenario (`thermal_mode: "prescribed"`, `carbon_mode: "off"`, empty per-unit maps) must produce the exact same save payload semantics for baseline scenarios so existing tests and scenarios are unaffected.
- Follow the existing `ScenarioEditor.tsx` field helpers (`SelectField`, `NumField`), `dyn-fieldset` / `dyn-grid` layout, and `setInitial` / `setMap` delete-on-undefined pattern.
- Never clip or silently alter user values in `dynamicLogic.ts`; `scenarioHints` remains advisory and the backend remains authoritative.
- UI-changing PR: requires a passing `e2e/` browser walk covering the Process dynamic studio before maintainer review.

## Acceptance criteria

1. `ScenarioEditor` renders controls for `thermal_mode`, `carbon_mode`, and per-unit `optics_geometry` + wall optics/fouling fields, and conditionally shows the thermal-state and carbon-state parameters when their respective modes are `"state"`.
2. `contentOf()` and `blankScenario()` populate all 321, 322, and 323 defaults cleanly without altering existing baseline scenario fields.
3. `scenarioHints()` reports clear hints for out-of-range thermal, carbonate, and optics inputs while returning `[]` for valid defaults.
4. `npm run build` (including `.mjs` static tests, `tsc`, and `vite`) and the Process dynamic browser walk pass with zero console/page errors.

## Required tests

- `frontend/tests/173-process-dynamic.mjs`:
  - Assert `contentOf` restores 321/322/323 defaults and `blankScenario` includes them.
  - Assert `scenarioHints` returns `[]` for valid thermal/carbon/optics values and flags invalid bounds (e.g. `water_density_kg_m3 <= 0`, `biomass_carbon_fraction > 1`, `wall_transmittance <= 0`, `back_wall_albedo > 1`).
  - Assert `ScenarioEditor.tsx` renders the new Thermal, Carbonate & pH, and Optics & wall fouling controls.

## Definition of done

Test gate green (see `AGENTS.md`), browser walk passing, acceptance criteria met, spec status updated, summary written.
