"""Generic process-engineering bookkeeping: parallel-branch loops, culture stoichiometry, harvest balance.

Pure functions in SI-flavoured units (stated per function), no I/O, no registries. Screening fidelity:
every function documents the assumption it relies on and refuses inputs outside it. Values used by the
tests are textbook or literature numbers, never product defaults.

Sources
- Darcy-Weisbach with Clamond's solution of Colebrook, via ``process_stack.correlations``
  (Clamond 2009, Int. J. Numer. Meth. Fluids 59; Perry's Chemical Engineers' Handbook, ch. 6).
- Elemental composition of microalgal biomass: Redfield-type N/P/C fractions are caller inputs;
  salts are converted with IUPAC standard atomic weights.
- Ideal-gas molar volume: V_m = R T / P, R = 8.314462618 J/(mol K) (CODATA 2018).
- Harvest balance: steady-state mass balance on a continuous culture with a recovery-limited
  side-stream concentrator (Doran, Bioprocess Engineering Principles, ch. 5 and 10).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from app.modules.process_stack.correlations import darcy_pipe_pressure_drop

GAS_CONSTANT: Final = 8.314462618  # J/(mol K)
STANDARD_PRESSURE_PA: Final = 101_325.0

# IUPAC standard atomic weights, g/mol
_ATOMIC_WEIGHT: Final = {"H": 1.008, "C": 12.011, "N": 14.007, "O": 15.999, "Na": 22.990, "P": 30.974}


def _molar_mass(counts: dict[str, int]) -> float:
    return sum(_ATOMIC_WEIGHT[element] * n for element, n in counts.items())


M_CO2: Final = _molar_mass({"C": 1, "O": 2})
M_O2: Final = _molar_mass({"O": 2})

# Salt -> the element it supplies: (molar mass of salt, atomic weight of element), g/mol
SALTS: Final = {
    "sodium_nitrate": ("N", _molar_mass({"Na": 1, "N": 1, "O": 3})),
    "sodium_dihydrogen_phosphate_monohydrate": ("P", _molar_mass({"Na": 1, "H": 4, "P": 1, "O": 5})),
    "sodium_bicarbonate": ("C", _molar_mass({"Na": 1, "H": 1, "C": 1, "O": 3})),
}


def _require_positive(**values: float) -> None:
    bad = sorted(name for name, value in values.items() if not (value > 0.0 and math.isfinite(value)))
    if bad:
        raise ValueError(f"must be positive and finite: {bad}")


def _require_non_negative(**values: float) -> None:
    bad = sorted(name for name, value in values.items() if not (value >= 0.0 and math.isfinite(value)))
    if bad:
        raise ValueError(f"must be non-negative and finite: {bad}")


# --- Parallel-branch loop hydraulics ------------------------------------------------------------


@dataclass(frozen=True)
class PipeSection:
    """One straight run of circular pipe. ``k_minor`` is the summed loss coefficient of its fittings,
    referenced to the velocity in this section (bends add their arc length to ``length_m`` and their
    K to ``k_minor``)."""

    length_m: float
    inner_diameter_m: float
    k_minor: float = 0.0
    roughness_m: float = 0.0


@dataclass(frozen=True)
class SectionHydraulics:
    velocity_m_s: float
    reynolds_number: float
    darcy_friction_factor: float
    major_pressure_drop_pa: float
    minor_pressure_drop_pa: float

    @property
    def pressure_drop_pa(self) -> float:
        return self.major_pressure_drop_pa + self.minor_pressure_drop_pa


def section_hydraulics(
    section: PipeSection, flow_rate_m3_s: float, density_kg_m3: float, dynamic_viscosity_pa_s: float
) -> SectionHydraulics:
    """Pressure drop of one section carrying ``flow_rate_m3_s`` (Darcy-Weisbach, Colebrook for turbulent
    flow, 64/Re for laminar; the transitional regime 2040 < Re < 4000 is refused)."""
    _require_positive(flow_rate_m3_s=flow_rate_m3_s, inner_diameter_m=section.inner_diameter_m)
    _require_non_negative(length_m=section.length_m, k_minor=section.k_minor)
    velocity = flow_rate_m3_s / (math.pi * section.inner_diameter_m**2 / 4.0)
    if section.length_m == 0.0:
        reynolds = density_kg_m3 * velocity * section.inner_diameter_m / dynamic_viscosity_pa_s
        major, friction = 0.0, 0.0
    else:
        reynolds, friction, major = darcy_pipe_pressure_drop(
            density=density_kg_m3, dynamic_viscosity=dynamic_viscosity_pa_s, velocity=velocity,
            diameter=section.inner_diameter_m, length=section.length_m, roughness=section.roughness_m,
        )
    minor = section.k_minor * density_kg_m3 * velocity**2 / 2.0
    return SectionHydraulics(velocity, reynolds, friction, major, minor)


@dataclass(frozen=True)
class ParallelLoopHydraulics:
    branch_count: int
    branch_flow_m3_s: float
    total_flow_m3_s: float
    common_sections: tuple[SectionHydraulics, ...]
    branch_sections: tuple[SectionHydraulics, ...]
    common_pressure_drop_pa: float
    branch_pressure_drop_pa: float  # ONE branch: identical branches see the same drop
    total_pressure_drop_pa: float
    hydraulic_power_w: float
    electric_power_w: float | None


def parallel_branch_loop(
    *,
    common_sections: tuple[PipeSection, ...],
    branch_sections: tuple[PipeSection, ...],
    branch_count: int,
    branch_flow_m3_s: float,
    density_kg_m3: float,
    dynamic_viscosity_pa_s: float,
    pump_efficiency: float | None = None,
) -> ParallelLoopHydraulics:
    """Loop with common sections feeding ``branch_count`` identical parallel branches.

    Flow multiplies by N, pressure drop does not: the total pressure drop is the common sections
    plus ONE branch (identical branches share the same drop), and the common sections carry
    Q_total = N * Q_branch at their own velocity, Reynolds number and friction factor. Assumes perfect
    symmetry (no maldistribution) and steady incompressible single-phase Newtonian flow. Hydraulic power
    is dP_total * Q_total; electric power divides by ``pump_efficiency`` (0 < eta <= 1) when given.
    """
    if branch_count < 1:
        raise ValueError("branch_count must be at least 1")
    if pump_efficiency is not None and not (0.0 < pump_efficiency <= 1.0):
        raise ValueError("pump_efficiency must be in (0, 1]")
    _require_positive(branch_flow_m3_s=branch_flow_m3_s)
    total_flow = branch_count * branch_flow_m3_s
    common = tuple(
        section_hydraulics(s, total_flow, density_kg_m3, dynamic_viscosity_pa_s) for s in common_sections
    )
    branch = tuple(
        section_hydraulics(s, branch_flow_m3_s, density_kg_m3, dynamic_viscosity_pa_s) for s in branch_sections
    )
    common_dp = sum(s.pressure_drop_pa for s in common)
    branch_dp = sum(s.pressure_drop_pa for s in branch)
    total_dp = common_dp + branch_dp
    hydraulic_power = total_dp * total_flow
    return ParallelLoopHydraulics(
        branch_count=branch_count,
        branch_flow_m3_s=branch_flow_m3_s,
        total_flow_m3_s=total_flow,
        common_sections=common,
        branch_sections=branch,
        common_pressure_drop_pa=common_dp,
        branch_pressure_drop_pa=branch_dp,
        total_pressure_drop_pa=total_dp,
        hydraulic_power_w=hydraulic_power,
        electric_power_w=None if pump_efficiency is None else hydraulic_power / pump_efficiency,
    )


# --- Culture stoichiometry ----------------------------------------------------------------------


def elemental_demand_g_d(production_g_d: float, mass_fractions: dict[str, float]) -> dict[str, float]:
    """Element incorporation rate = biomass production x element mass fraction (g element per g dry
    biomass, each in [0, 1]), e.g. ``{"N": 0.06, "P": 0.008, "C": 0.5}`` for a typical microalga."""
    _require_non_negative(production_g_d=production_g_d)
    for element, fraction in mass_fractions.items():
        if not (0.0 <= fraction <= 1.0):
            raise ValueError(f"mass fraction of {element} must be in [0, 1]")
    return {element: production_g_d * fraction for element, fraction in mass_fractions.items()}


def salt_dose_g_d(salt: str, element_demand_g_d: float) -> float:
    """Mass of ``salt`` (key of ``SALTS``) that supplies ``element_demand_g_d`` of its element."""
    if salt not in SALTS:
        raise ValueError(f"unknown salt {salt!r}; known: {sorted(SALTS)}")
    _require_non_negative(element_demand_g_d=element_demand_g_d)
    element, salt_molar_mass = SALTS[salt]
    return element_demand_g_d * salt_molar_mass / _ATOMIC_WEIGHT[element]


def stock_volume_l_d(dose_g_d: float, stock_concentration_g_l: float) -> float:
    """Volume of stock solution per day that delivers ``dose_g_d``."""
    _require_non_negative(dose_g_d=dose_g_d)
    _require_positive(stock_concentration_g_l=stock_concentration_g_l)
    return dose_g_d / stock_concentration_g_l


def carbon_to_gas_exchange_g_d(
    carbon_uptake_g_d: float, photosynthetic_quotient: float = 1.0
) -> tuple[float, float]:
    """(CO2 consumed, O2 evolved) in g/d for ``carbon_uptake_g_d`` of fixed carbon.

    CO2 follows from the carbon balance (44/12). O2 follows from the photosynthetic quotient
    PQ = mol O2 / mol CO2 (1.0 for pure carbohydrate; about 1.1-1.3 on nitrate), a parameter, not a constant.
    """
    _require_non_negative(carbon_uptake_g_d=carbon_uptake_g_d)
    _require_positive(photosynthetic_quotient=photosynthetic_quotient)
    co2_mol_d = carbon_uptake_g_d / _ATOMIC_WEIGHT["C"]
    return co2_mol_d * M_CO2, co2_mol_d * photosynthetic_quotient * M_O2


def molar_gas_volume_l_mol(temperature_k: float, pressure_pa: float = STANDARD_PRESSURE_PA) -> float:
    """Ideal-gas molar volume V_m = R T / P in L/mol (22.414 at 273.15 K and 1 atm; 24.0 at 20 C)."""
    _require_positive(temperature_k=temperature_k, pressure_pa=pressure_pa)
    return GAS_CONSTANT * temperature_k / pressure_pa * 1000.0


def gas_volume_l(mass_g: float, molar_mass_g_mol: float, temperature_k: float,
                 pressure_pa: float = STANDARD_PRESSURE_PA) -> float:
    """Volume of ``mass_g`` of an ideal gas at the given temperature and pressure."""
    _require_non_negative(mass_g=mass_g)
    _require_positive(molar_mass_g_mol=molar_mass_g_mol)
    return mass_g / molar_mass_g_mol * molar_gas_volume_l_mol(temperature_k, pressure_pa)


# --- Harvest mass balance -----------------------------------------------------------------------


@dataclass(frozen=True)
class HarvestBalance:
    bleed_l_d: float  # whole-culture bleed that would remove the production with no concentration step
    side_stream_l_d: float  # culture pumped through the concentrator
    side_stream_biomass_g_d: float
    recovered_biomass_g_d: float  # equals the production (steady state)
    returned_biomass_g_d: float  # uncaptured biomass returned to the culture
    concentrate_l_d: float
    filter_area_m2: float


def harvest_balance(
    *,
    production_g_d: float,
    culture_concentration_g_l: float,
    recovery: float,
    concentrate_concentration_g_l: float,
    flux_l_m2_h: float,
    filtration_hours_per_day: float,
) -> HarvestBalance:
    """Steady-state harvest of a culture by side-stream concentration.

    At steady state the recovered biomass equals the production. The concentrator captures a fraction
    ``recovery`` of the biomass it sees, so the side-stream is production / (X * recovery). Recovery is
    applied ONCE (dividing by it twice is a classic spreadsheet error). Filter area = side-stream volume
    rate / (hours x flux). Concentrate carry-over liquid is neglected.
    """
    _require_non_negative(production_g_d=production_g_d)
    _require_positive(
        culture_concentration_g_l=culture_concentration_g_l,
        concentrate_concentration_g_l=concentrate_concentration_g_l,
        flux_l_m2_h=flux_l_m2_h,
        filtration_hours_per_day=filtration_hours_per_day,
    )
    if not (0.0 < recovery <= 1.0):
        raise ValueError("recovery must be in (0, 1]")
    if concentrate_concentration_g_l <= culture_concentration_g_l:
        raise ValueError("concentrate concentration must exceed the culture concentration")
    if filtration_hours_per_day > 24.0:
        raise ValueError("filtration_hours_per_day cannot exceed 24")
    side_stream = production_g_d / (culture_concentration_g_l * recovery)
    side_stream_biomass = side_stream * culture_concentration_g_l
    return HarvestBalance(
        bleed_l_d=production_g_d / culture_concentration_g_l,
        side_stream_l_d=side_stream,
        side_stream_biomass_g_d=side_stream_biomass,
        recovered_biomass_g_d=production_g_d,
        returned_biomass_g_d=side_stream_biomass - production_g_d,
        concentrate_l_d=production_g_d / concentrate_concentration_g_l,
        filter_area_m2=side_stream / (filtration_hours_per_day * flux_l_m2_h),
    )
