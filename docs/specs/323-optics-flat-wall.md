# 323 — OPTICS-FLAT-WALL-1: Flat-slab geometry, wall transmittance, time-dependent fouling loss, and reflective back wall

Status: merged
Depends on: none

## Goal

In Wave 3 of the capability map (directives `L20261116160140-7608` and `L20261116205733-a728` B), add light-transport support beyond the cylindrical tube: a flat-slab optical kernel (`bio_models/slab.py`) with optional reflective back wall ($R_{\text{back}} \in [0, 1]$), wall material transmittance ($T_{\text{wall}} \in (0, 1]$), and time-dependent wall transmission loss ($f_{\text{loss}}(t)$) wired into `Growth` and `Scenario` with cylinder/unattenuated defaults so existing cases are unchanged.

## Why

Flat-panel and open-channel literature cases require planar Beer–Lambert attenuation rather than cylindrical cross-section averaging, and wall-material / fouling-loss cases require wall transmittance and time-varying transmission decay as well as reflective back-wall bounce. This is a parallel lane that does not depend on Wave 2.

## Code facts checked at `f03ad19c`

- `backend/app/modules/bio_models/cylinder.py::optical_depth` and `backend/app/modules/bio_models/cylinder.py::response_average_tau` compute cylindrical optical depth and cross-section-averaged light response.
- `backend/app/modules/process_stack/optics_t2.py::wall_transmittance` computes thin-slab air/wall/water transmittance.
- `backend/app/modules/process_stack/pbr_unit.py::Growth` and `backend/app/modules/process_stack/pbr_unit.py::Growth.light` dispatch cylindrical light averaging for `Growth.rates_at`.
- `backend/app/modules/process_stack/dynamic_models.py::Scenario` defines scenario configuration with `ConfigDict(extra="forbid", strict=True)`.
- `backend/app/modules/process_stack/dynamic_engine.py::run` calls `growth.rates_at(par_value, temperature, x, nitrogen)` inside `rhs`.

## Scope

In scope:
- New pure-NumPy optical kernel `backend/app/modules/bio_models/slab.py`:
  - `optical_depth(extinction, biomass, depth_m)` computing $\tau = k_X X L$.
  - `slab_irradiance_profile(i0, tau, z_norm, back_albedo=0.0, front_albedo=0.0)` computing $I(z)/I_0 = (e^{-\tau z} + R_{\text{back}} e^{-\tau (2 - z)}) / (1 - R_{\text{front}} R_{\text{back}} e^{-2\tau})$ for $z \in [0, 1]$ (with $0 \le R_{\text{back}} \le 1$, $0 \le R_{\text{front}} < 1$).
  - `response_average_tau(response_fn, i0, tau, back_albedo=0.0, front_albedo=0.0)` integrating $\langle f(I(z)) \rangle$ over $z \in [0, 1]$ via fixed Gauss–Legendre quadrature.
  - `wall_transmission_factor(elapsed_s, base_transmittance=1.0, fouling_rate_1_s=0.0, fouling_floor=0.0)` returning $T_{\text{wall}} \cdot (\text{floor} + (1 - \text{floor}) e^{-k_{\text{foul}} t})$.
- Extend `Growth` in `pbr_unit.py` with backward-compatible defaults:
  - `optics_geometry: Literal["cylinder", "flat_slab"] = "cylinder"`
  - `slab_depth_m: float | None = None` (falls back to `diameter` if `None`)
  - `back_wall_albedo: float = 0.0`
  - `front_wall_albedo: float = 0.0`
  - `wall_transmittance: float = 1.0`
  - Update `Growth.light(par, biomass, diffuse_fraction=None)` to multiply incident `par` by `wall_transmittance` and dispatch to `slab.response_average_tau` when `optics_geometry == "flat_slab"`.
- Extend `Scenario` in `dynamic_models.py` with backward-compatible defaults:
  - `optics_geometry: dict[str, Literal["cylinder", "flat_slab"]] = Field(default_factory=dict)` (per unit tag)
  - `back_wall_albedo: dict[str, float] = Field(default_factory=dict)` (per unit tag, in `[0, 1]`)
  - `wall_transmittance: dict[str, float] = Field(default_factory=dict)` (per unit tag, in `(0, 1]`)
  - `wall_fouling_rate_1_s: dict[str, float] = Field(default_factory=dict)` (per unit tag, `>= 0`)
  - `wall_fouling_floor: dict[str, float] = Field(default_factory=dict)` (per unit tag, in `[0, 1]`)
- In `dynamic_engine.py`, apply `slab.wall_transmission_factor(elapsed, ...)` to `par_value` when `wall_fouling_rate_1_s` or `wall_transmittance` is configured for a unit, and configure `Growth` with `optics_geometry` / `back_wall_albedo`.

Out of scope (binding non-goals):
- 2D/3D ray-traced `optics_t2.py` multi-tube flat-panel geometry.
- New `draft_models.py` unit types or database migrations.
- Frontend UI changes.

## Files likely touched

- `backend/app/modules/bio_models/slab.py` (new)
- `backend/app/modules/process_stack/pbr_unit.py`
- `backend/app/modules/process_stack/dynamic_models.py`
- `backend/app/modules/process_stack/dynamic_engine.py`
- `backend/tests/test_optics_flat_wall_323.py` (new)

## Design constraints

- Defaults (`optics_geometry="cylinder"`, `back_wall_albedo=0.0`, `wall_transmittance=1.0`, `wall_fouling_rate_1_s=0.0`) must leave all existing cylindrical and T2 optics tests bit-for-bit unchanged.
- `slab.py` uses NumPy only (no new dependencies) and validates finite bounds on albedos, transmittance, and fouling parameters.

## Acceptance criteria

1. `slab.response_average_tau` matches the exact analytical integral $\frac{1}{\tau} \int_0^\tau \frac{I_0 e^{-u}}{K_I + I_0 e^{-u}} du = \frac{1}{\tau} \ln\left(\frac{K_I + I_0}{K_I + I_0 e^{-\tau}}\right)$ for Monod response with `back_albedo = 0` within `1e-10`.
2. Increasing `back_wall_albedo` from `0.0` to `0.9` strictly increases $\langle f(I) \rangle$ at positive optical depth $\tau > 0$.
3. `wall_transmission_factor` decays monotonically from `base_transmittance` at $t = 0$ toward `base_transmittance * fouling_floor` as $t \to \infty$, and reduces effective PAR and biomass growth in `dynamic_engine.run`.
4. `Growth.light` and `dynamic_engine.run` support `optics_geometry="flat_slab"` while keeping `optics_geometry="cylinder"` default behaviour unchanged.

## Required tests

- `backend/tests/test_optics_flat_wall_323.py`:
  - Analytical Monod flat-slab integral test ($\tau \in \{0.1, 1.0, 5.0\}$) comparing `slab.response_average_tau` to $\frac{1}{\tau}\ln\frac{K_I + I_0}{K_I + I_0 e^{-\tau}}$.
  - Reflective back-wall monotonicity test (`back_albedo = 0.0` vs `0.5` vs `0.9`).
  - Wall transmittance and fouling decay test in `slab.wall_transmission_factor` and end-to-end `dynamic_engine.run` comparing clean wall vs fouled wall biomass yield.
  - Validation tests for out-of-range albedo/transmittance/fouling rate.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
