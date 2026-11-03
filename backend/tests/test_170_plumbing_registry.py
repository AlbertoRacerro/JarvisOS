"""Spec 170 capability 1: registry unit, quantity kinds and parameter domains."""

from __future__ import annotations

import pytest

from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import (
    QUANTITY_UNITS,
    UNIT_REGISTRY,
    AddUnit,
    DraftQuantity,
    SetUnitParams,
)
from tests.plumbing_170_support import PBR_VALUES, codes, pbr_document, pbr_quantities

PBR_TABLE = {
    # key: (kind, group)
    "tube_inner_diameter": ("length", "Geometry"), "tube_length": ("length", "Geometry"),
    "tube_count": ("dimensionless", "Geometry"), "liquid_velocity": ("velocity", "Operation"),
    "pump_efficiency": ("percent", "Operation"), "baffle_friction_multiplier": ("dimensionless", "Operation"),
    "oxygen_kla": ("specific_rate", "Operation"), "oxygen_saturation": ("mass_concentration", "Operation"),
    "peak_par": ("photon_flux_density", "Light & environment"), "photoperiod": ("time", "Light & environment"),
    "diffuse_fraction": ("dimensionless", "Light & environment"),
    "temperature_mean": ("temperature", "Light & environment"),
    "temperature_amplitude": ("temperature_difference", "Light & environment"),
}


def test_pbr_registry_entry_matches_contract_table() -> None:
    spec = UNIT_REGISTRY["PhotobioreactorT1"]
    assert (spec.label, spec.owner, spec.culture_rule) == ("Photobioreactor (T1)", "jarvis_bio", "pbr")
    assert spec.inlets == ("inlet",) and spec.outlets == ("outlet",) and not spec.energy_inlets
    assert list(spec.modes) == ["periodic_steady"] and spec.dwsim_type is None and spec.native_types == ()
    assert {item.key: (item.kind, item.group) for item in spec.params} == PBR_TABLE
    assert all(item.default is None for item in spec.params), "every PBR parameter is required, none defaulted"
    assert all(item.modes == ("periodic_steady",) for item in spec.params)


def test_projection_exposes_pbr_group_additively_and_palette_source() -> None:
    projection = draft.registry_projection()
    units = {item["type"]: item for item in projection["units"]}
    pbr = units["PhotobioreactorT1"]
    assert pbr["owner"] == "jarvis_bio" and pbr["label"] == "Photobioreactor (T1)"
    assert {item["key"]: item["group"] for item in pbr["params"]} == {k: v[1] for k, v in PBR_TABLE.items()}
    # The Jarvis units palette group is derived from owner == jarvis_bio, exactly like SpecifiedSeparator.
    assert {item["type"] for item in projection["units"] if item["owner"] == "jarvis_bio"} == {
        "SpecifiedSeparator", "PhotobioreactorT1", "HoldupTank"}
    assert all(item["group"] is None for name, unit in units.items() if name != "PhotobioreactorT1"
               for item in unit["params"])
    assert list(units)[-3:] == ["SpecifiedSeparator", "PhotobioreactorT1", "HoldupTank"]
    assert list(units).index("CSTR") < list(units).index("PFR"), "180 adds CSTR alongside DWSIM reactors"
    for kind in ("velocity", "photon_flux_density", "temperature_difference", "time", "specific_rate"):
        assert kind in projection["quantity_units"]


@pytest.mark.parametrize("kind,unit,value,si", [
    ("time", "h", 12.0, 43200.0), ("time", "d", 1.5, 129600.0), ("time", "s", 7.0, 7.0),
    ("specific_rate", "1/h", 36.0, 0.01), ("specific_rate", "1/d", 86400.0, 1.0), ("specific_rate", "1/s", 0.5, 0.5),
    ("velocity", "m/s", 0.8, 0.8), ("photon_flux_density", "umol/(m2.s)", 1800.0, 1800.0),
    ("temperature_difference", "K", 5.0, 5.0), ("temperature_difference", "degC", 5.0, 5.0),
])
def test_new_kinds_store_si_and_round_trip(kind: str, unit: str, value: float, si: float) -> None:
    stored = draft._si(DraftQuantity(value=value, unit=unit), kind, "field")
    assert stored == {"si": pytest.approx(si), "value": value, "unit": unit}
    assert draft.convert_si(stored["si"], kind, unit) == pytest.approx(value, rel=1e-14)
    assert QUANTITY_UNITS[kind][0] != ""


def test_temperature_difference_has_no_offset_and_heater_storage_is_unchanged() -> None:
    assert draft._si(DraftQuantity(value=3, unit="degC"), "temperature_difference", "dT")["si"] == 3.0
    assert draft.convert_si(3.0, "temperature_difference", "degC") == 3.0
    # A temperature point still converts with the offset, and the Heater keeps that storage.
    assert draft._si(DraftQuantity(value=3, unit="degC"), "temperature", "T")["si"] == pytest.approx(276.15)
    heater = {item.key: item.kind for item in UNIT_REGISTRY["Heater"].params}
    assert heater["temperature_change"] == "temperature"
    document = draft.apply_ops(draft.empty_document("h"), [
        AddUnit(op="add_unit", id="h", type="Heater", tag="H", x=0, y=0),
        SetUnitParams(op="set_unit_params", unit="h", mode="temperature_change",
                      values={"temperature_change": DraftQuantity(value=3, unit="degC")})])
    assert document["objects"]["h"]["params"]["temperature_change"]["si"] == pytest.approx(276.15)


def test_unsupported_units_for_new_kinds_are_refused() -> None:
    for kind, unit in (("velocity", "km/h"), ("time", "min"), ("specific_rate", "1/min"), ("photon_flux_density", "W/m2")):
        with pytest.raises(draft.DraftError) as error:
            draft._si(DraftQuantity(value=1, unit=unit), kind, "field")
        assert error.value.code == "unit_unsupported"


def _set(document: dict, key: str, value: float, unit: str) -> dict:
    return draft.apply_ops(document, [SetUnitParams(
        op="set_unit_params", unit="pbr", values={key: DraftQuantity(value=value, unit=unit)})])


@pytest.mark.parametrize("key,value,unit", [
    ("tube_inner_diameter", 0.005, "m"), ("tube_inner_diameter", 0.5, "m"), ("tube_inner_diameter", 5, "mm"),
    ("tube_length", 0.001, "m"), ("tube_count", 1, "dimensionless"), ("liquid_velocity", 1e-6, "m/s"),
    ("pump_efficiency", 100, "percent"), ("pump_efficiency", 0.1, "percent"),
    ("baffle_friction_multiplier", 1, "dimensionless"), ("oxygen_kla", 0, "1/h"), ("oxygen_saturation", 1e-9, "kg/m3"),
    ("peak_par", 0, "umol/(m2.s)"), ("photoperiod", 24, "h"), ("photoperiod", 1, "d"), ("photoperiod", 1e-3, "h"),
    ("diffuse_fraction", 0, "dimensionless"), ("diffuse_fraction", 1, "dimensionless"),
    ("temperature_amplitude", 0, "K"),
])
def test_pbr_domain_boundaries_accept(key: str, value: float, unit: str) -> None:
    document = _set(pbr_document(), key, value, unit)
    assert document["objects"]["pbr"]["params"][key]["unit"] == unit


@pytest.mark.parametrize("key,value,unit", [
    ("tube_inner_diameter", 0.0049, "m"), ("tube_inner_diameter", 0.51, "m"), ("tube_length", 0, "m"),
    ("tube_length", -1, "m"), ("tube_count", 0, "dimensionless"), ("tube_count", 2.5, "dimensionless"),
    ("liquid_velocity", 0, "m/s"), ("pump_efficiency", 0, "percent"), ("pump_efficiency", 100.1, "percent"),
    ("baffle_friction_multiplier", 0.99, "dimensionless"), ("oxygen_kla", -0.1, "1/h"), ("oxygen_saturation", 0, "kg/m3"),
    ("peak_par", -1, "umol/(m2.s)"), ("photoperiod", 0, "h"), ("photoperiod", 24.01, "h"), ("photoperiod", 2, "d"),
    ("diffuse_fraction", -0.01, "dimensionless"), ("diffuse_fraction", 1.01, "dimensionless"),
    ("temperature_mean", 0, "K"), ("temperature_amplitude", -1, "K"),
])
def test_pbr_domain_violations_are_refused(key: str, value: float, unit: str) -> None:
    with pytest.raises(draft.DraftError) as error:
        _set(pbr_document(), key, value, unit)
    assert error.value.code == "quantity_out_of_range" and error.value.detail["field"] == key


def test_temperature_minimum_must_stay_above_zero_kelvin() -> None:
    with pytest.raises(draft.DraftError) as error:
        _set(pbr_document(), "temperature_amplitude", 300, "K")  # mean is 25 degC = 298.15 K
    assert error.value.code == "quantity_out_of_range"
    # In one op both values are checked together, so a coherent pair is accepted.
    document = draft.apply_ops(pbr_document(), [SetUnitParams(op="set_unit_params", unit="pbr", values={
        "temperature_mean": DraftQuantity(value=400, unit="K"), "temperature_amplitude": DraftQuantity(value=300, unit="K")})])
    assert document["objects"]["pbr"]["params"]["temperature_amplitude"]["si"] == 300.0
    # A document that nevertheless violates it gets a blocker from instant validation.
    document["objects"]["pbr"]["params"]["temperature_amplitude"]["si"] = 400.0
    assert "PBR_TEMPERATURE_INVALID" in codes(draft.validate_document(document))


def test_new_pbr_has_no_defaults_so_every_parameter_is_a_blocker_until_set() -> None:
    document = draft.apply_ops(draft.empty_document("n"), [AddUnit(op="add_unit", id="pbr", type="PhotobioreactorT1",
                                                                  tag="PBR", x=0, y=0)])
    unit = document["objects"]["pbr"]
    assert unit["params"] == {} and unit["mode"] == "periodic_steady" and "model" not in unit
    missing = {item["field"] for item in draft.validate_document(document) if item["code"] == "UNIT_PARAM_MISSING"}
    assert missing == set(PBR_VALUES)


def test_complete_pbr_stores_si_values() -> None:
    params = pbr_document()["objects"]["pbr"]["params"]
    assert params["photoperiod"]["si"] == 12 * 3600
    assert params["oxygen_kla"]["si"] == pytest.approx(20 / 3600)
    assert params["temperature_mean"]["si"] == pytest.approx(298.15)
    assert params["temperature_amplitude"]["si"] == 5.0
    assert params["oxygen_saturation"]["si"] == pytest.approx(0.008)
    assert set(pbr_quantities()) == set(PBR_VALUES)
