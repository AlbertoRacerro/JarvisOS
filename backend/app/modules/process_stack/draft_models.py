"""Typed contracts and the supported registry for the Jarvis-owned process draft (spec 155).

The registry is the single source of what a draft may contain. Every entry names the
DWSIM type, its ports, and each parameter's SI storage key, DWSIM property and quantity
kind, so the draft schema, the inspector forms, the compiler and the read-back all derive
from one table. Anything absent here is unsupported and refused, never approximated.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

COMPILER_VERSION = "162.1"
MAX_OBJECTS = 60
ID_PATTERN = r"^[a-z][a-z0-9_-]{0,39}$"
TAG_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"

COMPOUNDS: tuple[str, ...] = (
    "Water", "Methanol", "Ethanol", "Acetone", "Benzene", "Toluene", "Nitrogen", "Oxygen",
    "Carbon dioxide", "Methane", "Ethane", "Propane", "N-butane", "N-hexane", "Hydrogen",
    "Argon", "Ammonia", "Acetic acid", "Isopropanol", "Ethylene oxide", "Ethylene glycol",
)
PROPERTY_PACKAGES: tuple[str, ...] = (
    "NRTL", "Peng-Robinson (PR)", "Soave-Redlich-Kwong (SRK)", "Raoult's Law", "UNIQUAC",
    "Steam Tables (IAPWS-IF97)",
)

QuantityKind = Literal["temperature", "temperature_difference", "pressure", "pressure_difference", "mass_flow", "percent", "power", "flow_ratio", "dimensionless", "length", "volume", "area", "heat_transfer_coefficient", "molar_flow", "specific_heat", "volume_flow", "mass_concentration", "molar_concentration", "molar_energy", "reaction_rate", "salinity", "ph", "velocity", "photon_flux_density", "time", "specific_rate", "inverse_length"]

# SI storage unit and the display units offered by inspectors, per quantity kind.
QUANTITY_UNITS: dict[str, tuple[str, tuple[str, ...]]] = {
    "temperature": ("K", ("degC", "K")),
    # A difference, not a point: 3 degC is stored as 3 K (no offset).
    "temperature_difference": ("K", ("K", "degC")),
    "velocity": ("m/s", ("m/s",)),
    "photon_flux_density": ("umol/(m2.s)", ("umol/(m2.s)",)),
    "time": ("s", ("h", "d", "s")),
    "specific_rate": ("1/s", ("1/h", "1/d", "1/s")),
    "pressure": ("Pa", ("bar", "kPa", "Pa", "psi")),
    "pressure_difference": ("Pa", ("bar", "kPa", "Pa", "psi")),
    "mass_flow": ("kg/s", ("kg/h", "kg/s", "t/h")),
    "percent": ("percent", ("percent",)),
    "power": ("kW", ("kW", "W")),
    "flow_ratio": ("dimensionless", ("dimensionless",)),
    "dimensionless": ("dimensionless", ("dimensionless",)),
    "length": ("m", ("m", "mm")),
    "volume": ("m3", ("m3",)),
    "area": ("m2", ("m2",)),
    "heat_transfer_coefficient": ("W/[m2.K]", ("W/[m2.K]",)),
    "molar_flow": ("mol/s", ("mol/s", "kmol/h")),
    "specific_heat": ("J/kg.K", ("J/kg.K",)),
    "volume_flow": ("m3/s", ("m3/s",)),
    "mass_concentration": ("kg/m3", ("kg/m3", "g/L", "mg/L")),
    "molar_concentration": ("mol/m3", ("mol/m3", "mmol/L", "kmol/m3")),
    "molar_energy": ("J/mol", ("J/mol", "kJ/mol")),
    "reaction_rate": ("mol/[m3.s]", ("kmol/[m3.h]", "mol/[m3.s]", "mol/[L.h]")),
    "salinity": ("g/kg", ("g/kg",)),
    "ph": ("pH", ("pH",)),
    # Spec 920: wall absorption coefficient; stored in 1/m.
    "inverse_length": ("1/m", ("1/m", "1/cm")),
}


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    kind: QuantityKind
    dwsim_property: str
    modes: tuple[str, ...]
    default: float | None = None
    minimum_si: float | None = None
    maximum_si: float | None = None
    # Spec 170: inspector group (additive, optional) and an open lower bound (value must exceed minimum_si).
    group: str | None = None
    exclusive_minimum: bool = False


@dataclass(frozen=True)
class UnitSpec:
    type: str
    label: str
    dwsim_type: str | None
    # Simulation class suffix in the saved native case (Mixer saves as NodeIn/Mixer).
    native_types: tuple[str, ...]
    inlets: tuple[str, ...]
    outlets: tuple[str, ...]
    required_inlets: int
    energy_inlets: tuple[str, ...] = ()
    energy_outlets: tuple[str, ...] = ()
    modes: dict[str, str] = field(default_factory=dict)
    params: tuple[ParamSpec, ...] = ()
    owner: str = "dwsim"
    culture_rule: str = "pass_through"

    @property
    def default_mode(self) -> str | None:
        return next(iter(self.modes), None)

    def params_for(self, mode: str | None) -> tuple[ParamSpec, ...]:
        return tuple(item for item in self.params if mode in item.modes)


def _heat(label: str, dwsim_type: str) -> UnitSpec:
    modes = ({"outlet_temperature": "OutletTemperature", "heat_added": "HeatAdded", "energy_stream": "EnergyStream",
              "outlet_vapor_fraction": "OutletVaporFraction", "temperature_change": "TemperatureChange",
              "heat_added_removed": "HeatAddedRemoved"} if dwsim_type == "Heater" else
             {"outlet_temperature": "OutletTemperature", "heat_removed": "HeatRemoved", "energy_stream": "EnergyStream",
              "outlet_vapor_fraction": "OutletVaporFraction", "temperature_change": "TemperatureChange"})
    heat_duty_mode: tuple[str, ...]
    if dwsim_type == "Cooler":
        heat_duty_mode = ("heat_removed",)
        all_modes = tuple(modes)
    else:
        heat_duty_mode = ("heat_added", "heat_added_removed")
        all_modes = tuple(modes)
    return UnitSpec(
        type=dwsim_type, label=label, dwsim_type=dwsim_type, native_types=(dwsim_type,),
        inlets=("inlet",), outlets=("outlet",), required_inlets=1,
        modes=modes,
        params=(
            ParamSpec("outlet_temperature", "Outlet temperature", "temperature", "OutletTemperature",
                      ("outlet_temperature",), minimum_si=1.0),
            ParamSpec("pressure_drop", "Pressure drop", "pressure_difference", "DeltaP",
                      all_modes, default=0.0, minimum_si=0.0),
            ParamSpec("heat_duty", "Heat duty", "power", "DeltaQ",
                      heat_duty_mode),
            ParamSpec("temperature_change", "Temperature change", "temperature", "DeltaT",
                      ("temperature_change",)),
            ParamSpec("vapor_fraction", "Outlet vapor fraction", "flow_ratio", "OutletVaporFraction",
                      ("outlet_vapor_fraction",), minimum_si=0.0, maximum_si=1.0),
        ),
        energy_inlets=("energy feed",),
    )


_PBR_MODE = ("periodic_steady",)
PBR_T2_PARAMETERS = frozenset({
    "tubes_across", "layers", "pitch_h", "pitch_v", "stagger_offset", "axis_azimuth",
    "centre_height_above_sea", "n_wall", "n_water", "sea_albedo",
})
PBR_MAX_PHOTOPERIOD_S = 24.0 * 3600.0

# Dynamic-only optics inputs stay outside the periodic-steady T1 registry contract.
PBR_T2_SPECS: tuple[ParamSpec, ...] = (
    ParamSpec("tubes_across", "Tubes across module", "dimensionless", "", _PBR_MODE,
              minimum_si=1.0, maximum_si=64.0, group="Light & environment"),
    ParamSpec("layers", "Tube layers", "dimensionless", "", _PBR_MODE,
              minimum_si=1.0, maximum_si=64.0, group="Light & environment"),
    ParamSpec("pitch_h", "Horizontal tube pitch", "length", "", _PBR_MODE,
              minimum_si=0.0, exclusive_minimum=True, group="Light & environment"),
    ParamSpec("pitch_v", "Vertical tube pitch", "length", "", _PBR_MODE,
              minimum_si=0.0, exclusive_minimum=True, group="Light & environment"),
    ParamSpec("stagger_offset", "Alternate layer stagger", "length", "", _PBR_MODE,
              group="Light & environment"),
    ParamSpec("axis_azimuth", "Tube axis azimuth (degrees)", "dimensionless", "", _PBR_MODE,
              minimum_si=0.0, maximum_si=360.0, group="Light & environment"),
    ParamSpec("centre_height_above_sea", "Lowest tube centre above sea", "length", "", _PBR_MODE,
              minimum_si=0.0, group="Light & environment"),
    ParamSpec("n_wall", "Wall refractive index", "dimensionless", "", _PBR_MODE,
              minimum_si=1.0, maximum_si=5.0, group="Light & environment"),
    ParamSpec("n_water", "Water refractive index", "dimensionless", "", _PBR_MODE,
              minimum_si=1.0, maximum_si=5.0, group="Light & environment"),
    ParamSpec("sea_albedo", "Sea albedo", "dimensionless", "", _PBR_MODE,
              minimum_si=0.0, maximum_si=1.0, group="Light & environment"),
    # #920 optics v2: optional pair. Both set selects optics_t2.v2; neither keeps v1; one alone is refused at prepare.
    ParamSpec("tube_outer_diameter", "Tube outer diameter (optional, optics v2)", "length", "", _PBR_MODE,
              minimum_si=0.0, exclusive_minimum=True, group="Light & environment"),
    ParamSpec("wall_absorption", "Wall absorption coefficient (optional, optics v2)", "inverse_length", "", _PBR_MODE,
              minimum_si=0.0, group="Light & environment"),
)
PBR_T2_OPTIONAL_KEYS = frozenset({"tube_outer_diameter", "wall_absorption"})

UNIT_REGISTRY: dict[str, UnitSpec] = {
    "Heater": _heat("Heater", "Heater"),
    "Cooler": _heat("Cooler", "Cooler"),
    "Pump": UnitSpec(
        type="Pump", label="Pump", dwsim_type="Pump", native_types=("Pump",),
        inlets=("inlet",), outlets=("outlet",), required_inlets=1,
        modes={"outlet_pressure": "OutletPressure", "pressure_increase": "Delta_P"},
        params=(
            ParamSpec("outlet_pressure", "Outlet pressure", "pressure", "Pout", ("outlet_pressure",),
                      minimum_si=1.0),
            ParamSpec("pressure_increase", "Pressure increase", "pressure_difference", "DeltaP",
                      ("pressure_increase",), minimum_si=0.0),
            ParamSpec("efficiency", "Efficiency", "percent", "Efficiency",
                      ("outlet_pressure", "pressure_increase"), default=75.0, minimum_si=1.0, maximum_si=100.0),
        ),
    ),
    "Valve": UnitSpec(
        type="Valve", label="Valve", dwsim_type="Valve", native_types=("Valve",),
        inlets=("inlet",), outlets=("outlet",), required_inlets=1,
        modes={"outlet_pressure": "OutletPressure", "pressure_drop": "DeltaP"},
        params=(
            ParamSpec("outlet_pressure", "Outlet pressure", "pressure", "OutletPressure", ("outlet_pressure",),
                      minimum_si=1.0),
            ParamSpec("pressure_drop", "Pressure drop", "pressure_difference", "DeltaP", ("pressure_drop",),
                      minimum_si=0.0),
        ),
    ),
    "Mixer": UnitSpec(
        type="Mixer", label="Mixer", dwsim_type="Mixer", native_types=("Mixer", "NodeIn"),
        inlets=("inlet 1", "inlet 2", "inlet 3"), outlets=("outlet",), required_inlets=2, culture_rule="mixer",
    ),
    "Flash": UnitSpec(
        type="Flash", label="Flash vessel", dwsim_type="Vessel", native_types=("Vessel",),
        inlets=("feed",), outlets=("vapor", "liquid"), required_inlets=1, culture_rule="refuse",
    ),
    "Splitter": UnitSpec(
        type="Splitter", label="Splitter", dwsim_type="Splitter", native_types=("Splitter", "NodeOut"),
        inlets=("inlet",), outlets=("outlet 1", "outlet 2", "outlet 3"), required_inlets=1, culture_rule="splitter",
        modes={"split_ratios": "SplitRatios", "mass_flow_spec": "StreamMassFlowSpec",
               "mole_flow_spec": "StreamMoleFlowSpec", "volumetric_flow_spec": "StreamVolumetricFlowSpec"},
        params=(ParamSpec("split_ratio_1", "Outlet 1 split ratio", "flow_ratio", "SR1", ("split_ratios",), default=0.5, minimum_si=0.0, maximum_si=1.0),
                ParamSpec("split_ratio_2", "Outlet 2 split ratio", "flow_ratio", "SR2", ("split_ratios",), default=0.5, minimum_si=0.0, maximum_si=1.0),
                ParamSpec("flow_spec_1", "Outlet 1 flow specification", "mass_flow", "StreamFlowSpec", ("mass_flow_spec",), minimum_si=0.0),
                ParamSpec("flow_spec_2", "Outlet 2 flow specification", "mass_flow", "Stream2FlowSpec", ("mass_flow_spec",), minimum_si=0.0),
                ParamSpec("mole_flow_spec_1", "Outlet 1 molar flow specification", "molar_flow", "StreamFlowSpec", ("mole_flow_spec",), minimum_si=0.0),
                ParamSpec("mole_flow_spec_2", "Outlet 2 molar flow specification", "molar_flow", "Stream2FlowSpec", ("mole_flow_spec",), minimum_si=0.0),
                ParamSpec("volume_flow_spec_1", "Outlet 1 volumetric flow specification", "volume_flow", "StreamFlowSpec", ("volumetric_flow_spec",), minimum_si=0.0),
                ParamSpec("volume_flow_spec_2", "Outlet 2 volumetric flow specification", "volume_flow", "Stream2FlowSpec", ("volumetric_flow_spec",), minimum_si=0.0)),
    ),
    "HeatExchanger": UnitSpec(
        type="HeatExchanger", label="Heat exchanger", dwsim_type="HeatExchanger", native_types=("HeatExchanger",),
        inlets=("hot inlet", "cold inlet"), outlets=("hot outlet", "cold outlet"), required_inlets=2,
        modes={"calc_both_temp_ua": "CalcBothTemp_UA"},
        params=(ParamSpec("overall_coefficient", "Overall heat transfer coefficient", "heat_transfer_coefficient", "OverallCoefficient", ("calc_both_temp_ua",), default=1000.0, minimum_si=0.0),
                ParamSpec("area", "Heat transfer area", "area", "Area", ("calc_both_temp_ua",), default=1.0, minimum_si=0.0),
                ParamSpec("hot_pressure_drop", "Hot side pressure drop", "pressure_difference", "HotSidePressureDrop", ("calc_both_temp_ua",), default=0.0, minimum_si=0.0),
                ParamSpec("cold_pressure_drop", "Cold side pressure drop", "pressure_difference", "ColdSidePressureDrop", ("calc_both_temp_ua",), default=0.0, minimum_si=0.0),
                ParamSpec("heat_loss", "Heat loss", "power", "HeatLoss", ("calc_both_temp_ua",), default=0.0)),
    ),
    "Recycle": UnitSpec(
        type="Recycle", label="Recycle", dwsim_type="Recycle", native_types=("Recycle",),
        inlets=("inlet",), outlets=("outlet",), required_inlets=1, culture_rule="tear",
    ),
    "CSTR": UnitSpec(
        type="CSTR", label="Continuous stirred tank reactor", dwsim_type="CSTR",
        native_types=("CSTR", "Reactor_CSTR"), inlets=("inlet",), outlets=("outlet",),
        required_inlets=1, energy_inlets=("energy feed",), culture_rule="refuse",
        modes={"isothermic": "Isothermic", "outlet_temperature": "OutletTemperature"},
        params=(ParamSpec("volume", "Volume", "volume", "Volume", ("isothermic", "outlet_temperature"),
                          minimum_si=0.0, exclusive_minimum=True),
                ParamSpec("outlet_temperature", "Outlet temperature", "temperature", "OutletTemperature",
                          ("outlet_temperature",), minimum_si=1.0)),
    ),
    "PFR": UnitSpec(
        type="PFR", label="Plug flow reactor", dwsim_type="PFR", native_types=("PFR", "Reactor_PFR"),
        inlets=("inlet",), outlets=("outlet",), required_inlets=1, energy_inlets=("energy feed",), culture_rule="refuse",
        modes={"adiabatic": "Adiabatic", "isothermic": "Isothermic", "outlet_temperature": "OutletTemperature",
               "nonisothermal_nonadiabatic": "NonIsothermalNonAdiabatic", "heat_exchange": "HeatExchange"},
        params=(ParamSpec("volume", "Volume", "volume", "Volume", tuple(("adiabatic", "isothermic", "outlet_temperature", "nonisothermal_nonadiabatic", "heat_exchange")), minimum_si=0.0),
                ParamSpec("length", "Length", "length", "Length", tuple(("adiabatic", "isothermic", "outlet_temperature", "nonisothermal_nonadiabatic", "heat_exchange")), minimum_si=0.0),
                ParamSpec("pressure_drop", "Pressure drop", "pressure_difference", "DeltaP", tuple(("adiabatic", "isothermic", "outlet_temperature", "nonisothermal_nonadiabatic", "heat_exchange")), default=0.0, minimum_si=0.0),
                ParamSpec("overall_coefficient", "Overall heat transfer coefficient", "heat_transfer_coefficient", "OverallHeatTransferCoefficient", ("heat_exchange",), minimum_si=0.0),
                ParamSpec("heat_exchange_area", "Heat exchange area", "area", "HeatExchangeArea", ("heat_exchange",), minimum_si=0.0),
                ParamSpec("coolant_inlet_temperature", "Coolant inlet temperature", "temperature", "CoolantInletTemperature", ("heat_exchange",)),
                ParamSpec("coolant_mass_flow", "Coolant mass flow rate", "mass_flow", "CoolantMassFlowRate", ("heat_exchange",), minimum_si=0.0),
                ParamSpec("coolant_specific_heat", "Coolant specific heat", "specific_heat", "CoolantSpecificHeat", ("heat_exchange",), minimum_si=0.0)),
    ),
    "DistillationColumn": UnitSpec(
        type="DistillationColumn", label="Distillation column", dwsim_type="DistillationColumn",
        native_types=("DistillationColumn",), inlets=("feed",), outlets=("distillate", "bottoms"),
        required_inlets=1, energy_inlets=("reboiler duty",), energy_outlets=("condenser duty",), culture_rule="refuse",
        params=(ParamSpec("number_of_stages", "Number of stages", "dimensionless", "NumberOfStages", ("column",), default=15, minimum_si=3, maximum_si=200),
                ParamSpec("feed_stage", "Feed stage", "dimensionless", "__FeedStage", ("column",), default=7, minimum_si=0, maximum_si=199),
                ParamSpec("top_pressure", "Top pressure", "pressure", "__TopPressure", ("column",), default=101325.0, minimum_si=1),
                ParamSpec("bottom_pressure", "Bottom pressure", "pressure", "__BottomPressure", ("column",), default=101325.0, minimum_si=1),
                ParamSpec("condenser_spec", "Condenser reflux ratio", "dimensionless", "Condenser_Specification_Value", ("column",), default=2.0, minimum_si=0),
                ParamSpec("reboiler_spec", "Bottoms molar flow", "molar_flow", "Reboiler_Specification_Value", ("column",), default=27.5, minimum_si=0)),
        modes={"column": "Wang-Henke (Bubble Point)"},
    ),
    "SpecifiedSeparator": UnitSpec(
        type="SpecifiedSeparator", label="Specified separator", dwsim_type=None, native_types=(),
        inlets=("inlet",), outlets=("concentrate", "clarified"), required_inlets=1,
        modes={"specified": "specified"}, owner="jarvis_bio", culture_rule="separator",
        params=(
            ParamSpec("biomass_recovery", "Biomass recovery", "percent", "", ("specified",),
                      default=90.0, minimum_si=0.0, maximum_si=100.0),
            ParamSpec("concentration_factor", "Concentration factor", "dimensionless", "", ("specified",),
                      default=10.0, minimum_si=1.0, maximum_si=1000.0),
        ),
    ),
    "PhotobioreactorT1": UnitSpec(
        type="PhotobioreactorT1", label="Photobioreactor (T1)", dwsim_type=None, native_types=(),
        inlets=("inlet",), outlets=("outlet",), required_inlets=1,
        modes={"periodic_steady": "periodic_steady"}, owner="jarvis_bio", culture_rule="pbr",
        # Spec 170 capability 1: every parameter is required and none has a default.
        params=(
            ParamSpec("tube_inner_diameter", "Tube inner diameter", "length", "", _PBR_MODE,
                      minimum_si=0.005, maximum_si=0.5, group="Geometry"),
            ParamSpec("tube_length", "Tube length", "length", "", _PBR_MODE,
                      minimum_si=0.0, exclusive_minimum=True, group="Geometry"),
            ParamSpec("tube_count", "Tube count", "dimensionless", "", _PBR_MODE,
                      minimum_si=1.0, group="Geometry"),
            ParamSpec("liquid_velocity", "Liquid velocity", "velocity", "", _PBR_MODE,
                      minimum_si=0.0, exclusive_minimum=True, group="Operation"),
            ParamSpec("pump_efficiency", "Pump efficiency", "percent", "", _PBR_MODE,
                      minimum_si=0.0, maximum_si=100.0, exclusive_minimum=True, group="Operation"),
            ParamSpec("baffle_friction_multiplier", "Baffle friction multiplier", "dimensionless", "", _PBR_MODE,
                      minimum_si=1.0, group="Operation"),
            ParamSpec("oxygen_kla", "Oxygen transfer coefficient (kLa)", "specific_rate", "", _PBR_MODE,
                      minimum_si=0.0, group="Operation"),
            ParamSpec("oxygen_saturation", "Oxygen saturation concentration", "mass_concentration", "", _PBR_MODE,
                      minimum_si=0.0, exclusive_minimum=True, group="Operation"),
            ParamSpec("peak_par", "Peak PAR", "photon_flux_density", "", _PBR_MODE,
                      minimum_si=0.0, group="Light & environment"),
            ParamSpec("photoperiod", "Photoperiod", "time", "", _PBR_MODE,
                      minimum_si=0.0, maximum_si=PBR_MAX_PHOTOPERIOD_S, exclusive_minimum=True,
                      group="Light & environment"),
            ParamSpec("diffuse_fraction", "Diffuse fraction", "dimensionless", "", _PBR_MODE,
                      minimum_si=0.0, maximum_si=1.0, group="Light & environment"),
            ParamSpec("temperature_mean", "Mean culture temperature", "temperature", "", _PBR_MODE,
                      minimum_si=0.0, exclusive_minimum=True, group="Light & environment"),
            ParamSpec("temperature_amplitude", "Diel temperature amplitude", "temperature_difference", "", _PBR_MODE,
                      minimum_si=0.0, group="Light & environment"),
        ),
    ),
    "HoldupTank": UnitSpec(
        type="HoldupTank", label="Holdup tank", dwsim_type=None, native_types=(),
        inlets=("inlet",), outlets=("outlet",), required_inlets=1,
        modes={"dynamic": "dynamic"}, owner="jarvis_bio", culture_rule="pbr",
        params=(
            ParamSpec("liquid_volume", "Liquid volume", "volume", "", ("dynamic",),
                      minimum_si=0.0, exclusive_minimum=True),
            ParamSpec("min_volume", "Minimum working volume", "volume", "", ("dynamic",), minimum_si=0.0),
            ParamSpec("max_volume", "Maximum working volume", "volume", "", ("dynamic",), minimum_si=0.0),
            ParamSpec("temperature", "Culture temperature", "temperature", "", ("dynamic",),
                      minimum_si=0.0, exclusive_minimum=True),
            ParamSpec("oxygen_kla", "Oxygen transfer coefficient (kLa)", "specific_rate", "", ("dynamic",),
                      minimum_si=0.0),
            ParamSpec("oxygen_saturation", "Oxygen saturation concentration", "mass_concentration", "",
                      ("dynamic",), minimum_si=0.0, exclusive_minimum=True),
        ),
    ),
}

def pbr_temperature_invalid(params: dict[str, dict]) -> bool:
    """Spec 170: the diel temperature minimum T_mean - amplitude must stay above 0 K."""
    mean = (params.get("temperature_mean") or {}).get("si")
    amplitude = (params.get("temperature_amplitude") or {}).get("si")
    return mean is not None and amplitude is not None and not mean - amplitude > 0.0


if any((spec.owner == "dwsim") != (spec.dwsim_type is not None and bool(spec.native_types))
       for spec in UNIT_REGISTRY.values()):
    raise RuntimeError("Process unit registry owner/native-type contract is inconsistent")
if any(spec.owner not in {"dwsim", "jarvis_bio"} for spec in UNIT_REGISTRY.values()):
    raise RuntimeError("Process unit registry contains an unsupported owner")

STUDIO_UNIT = "StudioUnit"


class StudioPinError(ValueError):
    """A placed StudioUnit's embedded contract does not match its pinned hash."""


def studio_contract_digest(contract: dict[str, Any]) -> str:
    raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@lru_cache(maxsize=256)
def _studio_spec(raw_contract: str, name: str) -> UnitSpec:
    contract = json.loads(raw_contract)
    return UnitSpec(
        type=STUDIO_UNIT, label=name, dwsim_type=None, native_types=(),
        inlets=tuple(item["name"] for item in contract["inlets"]),
        outlets=tuple(item["name"] for item in contract["outlets"]),
        required_inlets=len(contract["inlets"]), modes={"steady": "steady"}, owner="jarvis_bio",
        culture_rule="studio",
        params=tuple(ParamSpec(item["name"], item["label"], "dimensionless", "", ("steady",),
                               minimum_si=item.get("min"), maximum_si=item.get("max"))
                     for item in contract["parameters"]))


def spec_for(unit: dict[str, Any]) -> UnitSpec:
    """The UnitSpec of a placed unit; a StudioUnit's ports and parameters come from its verified pinned contract."""
    if unit["type"] != STUDIO_UNIT:
        return UNIT_REGISTRY[unit["type"]]
    contract = (unit.get("studio") or {}).get("contract")
    pin = unit.get("model") or {}
    if not isinstance(contract, dict) or studio_contract_digest(contract) != pin.get("contract_sha256"):
        raise StudioPinError(f"{unit.get('tag')}: the pinned Studio contract does not match its hash")
    return _studio_spec(json.dumps(contract, sort_keys=True, separators=(",", ":")),
                        str((unit.get("studio") or {}).get("name") or "Studio unit"))


UNSUPPORTED_TYPES: dict[str, str] = {
    "Reactor": ("Generic reactors are not offered: use a PFR or CSTR with typed kinetics; Gibbs, conversion and "
                "equilibrium reactors are not supported."),
}

# Feed-stream specification keys: SI storage and DWSIM MCP argument / read-back names.
STREAM_SPECS: dict[str, tuple[QuantityKind, str, str]] = {
    "temperature": ("temperature", "temperature_K", "Temperature"),
    "pressure": ("pressure", "pressure_Pa", "Pressure"),
    "mass_flow": ("mass_flow", "mass_flow_kg_s", "Mass flow"),
    "molar_flow": ("molar_flow", "molar_flow_mol_s", "Molar flow"),
    "vapor_fraction": ("flow_ratio", "vapor_fraction", "Vapor fraction"),
}
# Mutually exclusive feed alternatives proven on the pinned DWSIM 10.2.9 MCP (spec 162 probe,
# evidence/162/feed_specs.json): a feed needs pressure plus exactly one key of each group.
FEED_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "flow": ("mass_flow", "molar_flow"),
    "state": ("temperature", "vapor_fraction"),
}
COMPOSITION_BASES: tuple[str, ...] = ("mass", "mole")


class DraftQuantity(BaseModel):
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=16)


class Endpoint(BaseModel):
    unit: str = Field(pattern=ID_PATTERN)
    port: int = Field(ge=0, le=7)


class _Op(BaseModel):
    model_config = {"extra": "forbid"}


class AddUnit(_Op):
    op: Literal["add_unit"]
    id: str | None = Field(default=None, pattern=ID_PATTERN)
    type: str
    tag: str = Field(pattern=TAG_PATTERN)
    x: int = Field(ge=-5000, le=5000)
    y: int = Field(ge=-5000, le=5000)
    # Spec 190: the published Studio version a "StudioUnit" is placed from.
    version_id: str | None = Field(default=None, max_length=64)


class UpdateStudioUnit(_Op):
    """Spec 190: explicitly re-pin a placed StudioUnit to a newer published version of the same model."""

    op: Literal["update_studio_unit"]
    unit: str = Field(pattern=ID_PATTERN)
    version_id: str = Field(max_length=64)


class AddStream(_Op):
    op: Literal["add_stream"]
    id: str | None = Field(default=None, pattern=ID_PATTERN)
    tag: str = Field(pattern=TAG_PATTERN)
    stream_type: Literal["material", "energy"] = "material"
    x: int = Field(ge=-5000, le=5000)
    y: int = Field(ge=-5000, le=5000)


class Delete(_Op):
    op: Literal["delete"]
    id: str = Field(pattern=ID_PATTERN)


class Move(_Op):
    op: Literal["move"]
    id: str = Field(pattern=ID_PATTERN)
    x: int = Field(ge=-5000, le=5000)
    y: int = Field(ge=-5000, le=5000)


class Rename(_Op):
    op: Literal["rename"]
    id: str = Field(pattern=ID_PATTERN)
    tag: str = Field(pattern=TAG_PATTERN)


class Connect(_Op):
    op: Literal["connect"]
    stream: str = Field(pattern=ID_PATTERN)
    end: Literal["source", "target"]
    unit: str = Field(pattern=ID_PATTERN)
    port: int = Field(ge=0, le=7)


class Disconnect(_Op):
    op: Literal["disconnect"]
    stream: str = Field(pattern=ID_PATTERN)
    end: Literal["source", "target"]


class RoutePoint(BaseModel):
    x: int = Field(ge=-5000, le=5000)
    y: int = Field(ge=-5000, le=5000)


class SetOrientation(_Op):
    """Layout-only mirroring of a unit's port sides; never changes process meaning."""

    op: Literal["set_orientation"]
    id: str = Field(pattern=ID_PATTERN)
    flip_x: bool | None = None
    flip_y: bool | None = None


class SetRoute(_Op):
    op: Literal["set_route"]
    stream: str = Field(pattern=ID_PATTERN)
    points: list[RoutePoint] = Field(max_length=32)


class SetStreamSpec(_Op):
    op: Literal["set_stream_spec"]
    stream: str = Field(pattern=ID_PATTERN)
    temperature: DraftQuantity | None = None
    pressure: DraftQuantity | None = None
    mass_flow: DraftQuantity | None = None
    molar_flow: DraftQuantity | None = None
    vapor_fraction: DraftQuantity | None = None
    duty: DraftQuantity | None = None
    composition: dict[str, float] | None = None
    composition_basis: Literal["mass", "mole"] | None = None
    clear: list[Literal["temperature", "pressure", "mass_flow", "molar_flow", "vapor_fraction", "composition",
                        "duty"]] = Field(default_factory=list)

    @field_validator("composition")
    @classmethod
    def _fractions(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is not None and any(not 0.0 <= item <= 1.0 for item in value.values()):
            raise ValueError("composition fractions must be between 0 and 1")
        return value


class SetUnitParams(_Op):
    op: Literal["set_unit_params"]
    unit: str = Field(pattern=ID_PATTERN)
    mode: str | None = None
    values: dict[str, DraftQuantity] = Field(default_factory=dict)
    # Bounded removal of optional params (PBR_T2_OPTIONAL_KEYS on PhotobioreactorT1 only); absent keys are a no-op.
    unset: list[str] = Field(default_factory=list, max_length=8)
    options: dict[str, str | float | bool] = Field(default_factory=dict)
    reactions: list[str] | None = None


class UnitModelPin(BaseModel):
    model_config = {"extra": "forbid"}
    card_id: str = Field(min_length=1, max_length=128)
    card_revision: str = Field(pattern=r"^r-[a-f0-9]{16}$")
    card_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SetUnitModel(_Op):
    op: Literal["set_unit_model"]
    unit: str = Field(pattern=ID_PATTERN)
    model: UnitModelPin | None


class UnitHydraulicsPin(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["cfd_reduced_model"]
    studio_version_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reduced_model_version_id: str = Field(min_length=1, max_length=128)
    geometry_digest: str = Field(min_length=1, max_length=128)
    out_of_range: Literal["refuse", "warn"] = "refuse"


class SetUnitHydraulics(_Op):
    op: Literal["set_unit_hydraulics"]
    unit: str = Field(pattern=ID_PATTERN)
    hydraulics: UnitHydraulicsPin | None


class SetStreamCulture(_Op):
    op: Literal["set_stream_culture"]
    stream: str = Field(pattern=ID_PATTERN)
    culture: dict[str, DraftQuantity] | None


class Inhibition(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["noncompetitive", "competitive"]
    inhibitor: str
    k_i: DraftQuantity


class TemperatureFactor(BaseModel):
    model_config = {"extra": "forbid"}
    activation_energy: DraftQuantity
    reference_temperature: DraftQuantity


class _RateLaw(BaseModel):
    model_config = {"extra": "forbid"}
    substrate: str
    # Absent parameters are a pre-Run finding (KINETICS_PARAMETER_MISSING), not an apply refusal.
    v_max: DraftQuantity | None = None
    k_s: DraftQuantity | None = None
    inhibitions: list[Inhibition] = Field(default_factory=list, max_length=3)
    temperature: TemperatureFactor | None = None


class MonodRateLaw(_RateLaw):
    form: Literal["monod"]


class HaldaneRateLaw(_RateLaw):
    form: Literal["haldane"]
    k_i: DraftQuantity | None = None


class ReactionProvenance(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["literature", "measurement", "operator_estimate", "synthetic"]
    citation: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=500)


class ReactionValidity(BaseModel):
    model_config = {"extra": "forbid"}
    temperature_min: DraftQuantity | None = None
    temperature_max: DraftQuantity | None = None
    substrate_max: DraftQuantity | None = None


class KineticReaction(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=80)
    stoichiometry: dict[str, float] = Field(min_length=2)
    orders: dict[str, float] = Field(default_factory=dict)
    base_reactant: str
    phase: Literal["Mixture", "Vapor", "Liquid"] = "Mixture"
    basis: Literal["MolarConc"] = "MolarConc"
    A_forward: DraftQuantity | None = None
    E_forward: DraftQuantity | None = None
    rate_law: Annotated[MonodRateLaw | HaldaneRateLaw, Field(discriminator="form")] | None = None
    provenance: ReactionProvenance | None = None
    validity: ReactionValidity | None = None

    @model_validator(mode="after")
    def _form_contract(self) -> KineticReaction:
        if self.rate_law is None:
            if self.A_forward is None or self.E_forward is None:
                raise ValueError("Arrhenius constants are required")
        elif self.A_forward is not None or self.E_forward is not None or self.orders:
            raise ValueError("script rate laws cannot carry Arrhenius constants or orders")
        elif self.phase != "Liquid":
            raise ValueError("typed rate laws require the Liquid phase")
        return self

    @field_validator("stoichiometry")
    @classmethod
    def _nonzero_stoichiometry(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not float(item) or not math.isfinite(float(item)) for item in value.values()):
            raise ValueError("stoichiometric coefficients must be finite and nonzero")
        return value

    @field_validator("orders")
    @classmethod
    def _nonnegative_orders(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(float(item)) or float(item) < 0 for item in value.values()):
            raise ValueError("reaction orders must be finite and nonnegative")
        return value

    @field_validator("A_forward", "E_forward")
    @classmethod
    def _nonnegative_constants(cls, value: DraftQuantity) -> DraftQuantity:
        if value is not None and value.value < 0:
            raise ValueError("kinetic constants must be nonnegative")
        return value


class SetReactions(_Op):
    op: Literal["set_reactions"]
    reactions: dict[str, KineticReaction] = Field(max_length=12)


class SetReactorReaction(_Op):
    op: Literal["set_reactor_reaction"]
    unit: str = Field(pattern=ID_PATTERN)
    reaction_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    reaction: KineticReaction | None


class SetThermo(_Op):
    op: Literal["set_thermo"]
    compounds: list[str] | None = Field(default=None, max_length=12)
    property_package: str | None = None


class _DynamicSet(_Op):
    id: str = Field(pattern=ID_PATTERN)
    value: dict[str, Any]


class SetSchedule(_DynamicSet):
    op: Literal["set_schedule"]


class DeleteSchedule(_Op):
    op: Literal["delete_schedule"]
    id: str = Field(pattern=ID_PATTERN)


class SetController(_DynamicSet):
    op: Literal["set_controller"]


class DeleteController(_Op):
    op: Literal["delete_controller"]
    id: str = Field(pattern=ID_PATTERN)


class SetScenario(_DynamicSet):
    op: Literal["set_scenario"]


class DeleteScenario(_Op):
    op: Literal["delete_scenario"]
    id: str = Field(pattern=ID_PATTERN)


class SetSolver(_Op):
    """Recycle/tear solver settings for Jarvis-converged tears (spec 183); None restores the defaults."""

    op: Literal["set_solver"]
    solver: dict[str, Any] | None


DraftOp = Annotated[
    AddUnit | UpdateStudioUnit | AddStream | Delete | Move | Rename | Connect | Disconnect | SetRoute | SetOrientation | SetStreamSpec | SetStreamCulture | SetUnitParams | SetUnitModel | SetUnitHydraulics | SetReactions | SetReactorReaction | SetThermo | SetSchedule | DeleteSchedule | SetController | DeleteController | SetScenario | DeleteScenario | SetSolver,
    Field(discriminator="op"),
]


class PatchRequest(BaseModel):
    expected_revision: str
    ops: list[DraftOp] = Field(min_length=1, max_length=64)


class CreateDraft(BaseModel):
    name: str = Field(default="Process draft", min_length=1, max_length=80)


class RestoreRequest(BaseModel):
    expected_revision: str
    source_revision: str


class SolveRequest(BaseModel):
    """Start a background solve of an exact revision (spec 210); ``retry_of`` links it to a parent run."""

    revision: str = Field(min_length=1, max_length=128)
    retry_of: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ProposedChange(BaseModel):
    target: str = Field(min_length=1, max_length=32)
    property: str = Field(min_length=1, max_length=40)
    proposed: DraftQuantity | str | dict[str, float]


class ProposalRequest(BaseModel):
    base_revision: str
    changes: list[ProposedChange] = Field(min_length=1, max_length=12)
    rationale: str = Field(default="", max_length=600)
    source: str = Field(default="operator", max_length=80)


class ProposalDecision(BaseModel):
    accepted_changes: list[int] | None = None


class RunRequest(BaseModel):
    pass
