"""Evidence-derived DWSIM capability contract for process drafts (spec 158)."""

import copy
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from app.core.database import initialize_database
from app.main import app
from app.modules.process_stack import draft, draft_compiler, editor
from app.modules.process_stack.draft import (
    DraftError,
    _capability_manifest,
    apply_ops,
    empty_document,
    registry_projection,
    validate_document,
)
from app.modules.process_stack.draft_compiler import _native_energy_port, _stream_result, _unit_properties
from app.modules.process_stack.draft_models import UNIT_REGISTRY, UNSUPPORTED_TYPES, DraftOp

_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR = _ROOT / "scripts" / "benchmarks" / "dwsim" / "manifest_generator.py"
_MANIFEST = _ROOT / "backend" / "app" / "modules" / "process_stack" / "dwsim_10_2_9_manifest.json"
_CONNECT_ROLES = {"feed_port": "feed", "product_port": "product",
                  "energy_feed_port": "energy_feed", "energy_product_port": "energy_product"}


def _modes(unit_type: str) -> tuple[str | None, ...]:
    return tuple(UNIT_REGISTRY[unit_type].modes) or (None,)


def _synthetic_document(unit_type: str, mode: str | None) -> dict[str, Any]:
    """A compile-ready draft with every parameter of ``mode`` set and every port occupied."""
    spec = UNIT_REGISTRY[unit_type]
    objects: dict[str, Any] = {"u": {
        "id": "u", "kind": "unit", "type": unit_type, "tag": "U", "x": 0, "y": 0, "mode": mode,
        "params": {item.key: {"si": 1.0} for item in spec.params_for(mode)}, "options": {},
        "reactions": ["R1"] if unit_type == "PFR" else []}}

    def stream(tag: str, kind: str, end: str, port: int) -> None:
        feed = {"temperature": {"si": 300.0}, "pressure": {"si": 1e5}, "mass_flow": {"si": 1.0}}
        objects[tag.lower()] = {"id": tag.lower(), "kind": "stream", "type": kind, "tag": tag, "x": 0, "y": 0,
                                "source": {"unit": "u", "port": port} if end == "source" else None,
                                "target": {"unit": "u", "port": port} if end == "target" else None,
                                "spec": {"duty": {"si": 1.0}} if kind == "EnergyStream" else
                                feed if end == "target" else {}}

    for port in range(len(spec.inlets)):
        stream(f"F{port}", "MaterialStream", "target", port)
    for port in range(len(spec.outlets)):
        stream(f"P{port}", "MaterialStream", "source", port)
    for port in range(len(spec.energy_inlets)):
        stream(f"QF{port}", "EnergyStream", "target", port)
    for port in range(len(spec.energy_outlets)):
        stream(f"QP{port}", "EnergyStream", "source", port)
    return {"compounds": [], "property_package": "NRTL", "objects": objects, "reactions": {}}


def _unit_and_document(unit_type: str, mode: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    document = _synthetic_document(unit_type, mode)
    return document["objects"]["u"], document


def _compiled_writes() -> tuple[set[tuple[str, str]], set[tuple[str, str, int]]]:
    """Every (type, property) the compiler sets and every (type, role, port) it connects over MCP."""
    written: set[tuple[str, str]] = set()
    connected: set[tuple[str, str, int]] = set()
    for unit_type, unit_spec in UNIT_REGISTRY.items():
        if unit_spec.owner != "dwsim":
            continue
        for mode in _modes(unit_type):
            for name, args in draft_compiler.plan(_synthetic_document(unit_type, mode)):
                owner = unit_type if args.get("name", args.get("unitop")) == "U" else "EnergyStream"
                if name == "dwsim_unitop_set":
                    written |= {(owner, key) for key in args["properties"]}
                elif name == "dwsim_unitop_connect":
                    connected |= {(unit_type, role, args[key]) for key, role in _CONNECT_ROLES.items() if key in args}
    return written, connected


def _manifest_object(unit_type: str) -> dict[str, Any]:
    manifest = _capability_manifest()
    native = UNIT_REGISTRY[unit_type].dwsim_type if unit_type in UNIT_REGISTRY else unit_type
    return manifest["objects"][native]


def test_checked_in_manifest_equals_generator_output() -> None:
    spec = importlib.util.spec_from_file_location("generate_manifest_158", _GENERATOR)
    assert spec and spec.loader
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert _MANIFEST.read_text(encoding="utf-8") == generator.render()


def test_every_compiled_input_property_is_settable_in_the_manifest() -> None:
    written, _connected = _compiled_writes()
    assert ("EnergyStream", "EnergyFlow") in written
    missing = {(unit_type, prop) for unit_type, prop in written
               if prop not in _manifest_object(unit_type)["settable_properties"]}
    assert missing == set()
    for unit_type, prop in written:
        assert _manifest_object(unit_type)["settable_property_evidence"][prop]
    for unit_type, spec in UNIT_REGISTRY.items():
        if spec.owner != "dwsim":
            continue
        native_xml = _manifest_object(unit_type)["native_xml_inputs"]
        registry_native = {item.dwsim_property for item in spec.params if item.dwsim_property.startswith("__")}
        compiled_native = {key for mode in _modes(unit_type)
                           for key in _unit_properties(*_unit_and_document(unit_type, mode))
                           if key.startswith("__") and not key.startswith("__SplitRatio")}
        assert registry_native | compiled_native <= set(native_xml), unit_type


def test_every_registry_mode_and_enum_value_is_in_the_manifest() -> None:
    for unit_type, spec in UNIT_REGISTRY.items():
        if spec.owner != "dwsim":
            continue
        capability = _manifest_object(unit_type)
        assert set(spec.modes.values()) <= set(capability["modes"]), unit_type
        assert all(capability["mode_evidence"][mode] for mode in spec.modes.values())
        for mode in _modes(unit_type):
            properties = _unit_properties(*_unit_and_document(unit_type, mode))
            for key in {"CalcMode", "CalculationMode", "OperationMode", "ReactorOperationMode"} & set(properties):
                assert properties[key] in capability["modes"], (unit_type, key)
            if "CondenserType" in properties:
                assert properties["CondenserType"] in capability["enum_values"]["CondenserType"]


def test_every_registry_and_compiled_port_is_a_manifest_native_port() -> None:
    _written, connected = _compiled_writes()
    used = set(connected)
    for unit_type, spec in UNIT_REGISTRY.items():
        if spec.owner != "dwsim":
            continue
        unit = {"type": unit_type}
        used |= {(unit_type, "feed", port) for port in range(len(spec.inlets))}
        used |= {(unit_type, "product", port) for port in range(len(spec.outlets))}
        used |= {(unit_type, "energy_feed", _native_energy_port(unit, port)) for port in range(len(spec.energy_inlets))}
        used |= {(unit_type, "energy_product", _native_energy_port(unit, port))
                 for port in range(len(spec.energy_outlets))}
    for unit_type, role, port in used:
        entries = {item["port"]: item for item in _manifest_object(unit_type)["ports"].get(role, [])}
        assert port in entries, (unit_type, role, port)
        assert entries[port]["evidence"], (unit_type, role, port)
    evidence_dir = _MANIFEST.parent / "evidence" / "158"
    for native, capability in _capability_manifest()["objects"].items():
        for role, entries in capability["ports"].items():
            for entry in entries:
                if entry["captured"]:
                    assert all((evidence_dir / source.split(":", 1)[0]).is_file() for source in entry["evidence"])
                else:  # the MCP refuses column energy connections; native XML + reload, verified by read-back
                    assert (native, role) in {("DistillationColumn", "energy_feed"),
                                              ("DistillationColumn", "energy_product")}


def test_registry_modes_and_ports_are_present_in_pinned_manifest() -> None:
    projection = registry_projection()
    manifest = projection["dwsim_capabilities"]
    assert manifest["runtime"] == "DWSIM 10.2.9"
    for unit in UNIT_REGISTRY.values():
        if unit.owner != "dwsim":
            continue
        capability = manifest["objects"][unit.dwsim_type]
        assert set(unit.modes.values()) <= set(capability["modes"])
        assert capability["native_type"] in unit.native_types


def test_manifest_exposes_reported_property_catalogue_and_column_limit() -> None:
    projection = registry_projection()
    entries = {item["type"]: item for item in projection["units"]}
    assert entries["Heater"]["result_properties"]
    assert {item["classification"] for item in entries["Heater"]["result_properties"]} <= {"input", "result"}
    assert "DistillationColumn" in UNIT_REGISTRY
    assert "DistillationColumn" not in UNSUPPORTED_TYPES
    assert draft_compiler._NATIVE_TO_TYPE["Reactor_PFR"] == "PFR"


def test_stream_result_keeps_dwsim_report_without_recalculation() -> None:
    reported = {
        "temperature_K": 300.0,
        "properties": {"Cp": {"value": "4.2", "units": "kJ/kg.K", "specification": False}},
        "phases": [{"name": "Mixture", "compounds": {"Water": {"mass_fraction": 1.0}}}],
    }
    result = _stream_result(reported)
    assert result["reported"] == reported
    assert result["temperature_K"] == 300.0


def test_energy_stream_and_layout_routes_compile_orthogonally() -> None:
    document = empty_document("Energy train")
    ops = [
        {"op": "set_thermo", "compounds": ["Water"], "property_package": "NRTL"},
        {"op": "add_unit", "id": "heater", "type": "Heater", "tag": "H1", "x": 0, "y": 0},
        {"op": "add_stream", "id": "feed", "tag": "F", "x": 0, "y": 0},
        {"op": "add_stream", "id": "product", "tag": "P", "x": 0, "y": 0},
        {"op": "add_stream", "id": "duty", "tag": "Q", "stream_type": "energy", "x": 0, "y": 0},
        {"op": "set_stream_spec", "stream": "feed", "temperature": {"value": 25, "unit": "degC"},
         "pressure": {"value": 1, "unit": "bar"}, "mass_flow": {"value": 1, "unit": "kg/s"},
         "composition": {"Water": 1.0}},
        {"op": "connect", "stream": "feed", "end": "target", "unit": "heater", "port": 0},
        {"op": "connect", "stream": "product", "end": "source", "unit": "heater", "port": 0},
        {"op": "connect", "stream": "duty", "end": "target", "unit": "heater", "port": 0},
        {"op": "set_unit_params", "unit": "heater", "mode": "energy_stream"},
        {"op": "set_stream_spec", "stream": "duty", "duty": {"value": 50, "unit": "kW"}},
    ]
    adapter = TypeAdapter(DraftOp)
    canonical = apply_ops(document, [adapter.validate_python(op) for op in ops])
    calls = draft_compiler.plan(canonical)
    assert ("dwsim_stream_add_energy", {"name": "Q"}) in calls
    assert ("dwsim_unitop_connect", {"unitop": "H1", "energy_feed": "Q", "energy_feed_port": 1}) in calls
    assert ("dwsim_unitop_set", {"name": "Q", "properties": {"EnergyFlow": 50.0}}) in calls
    expected = draft_compiler.expected(canonical)
    routed = apply_ops(canonical, [adapter.validate_python({
        "op": "set_route", "stream": "duty", "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}],
    })])
    assert draft_compiler.expected(routed) == expected
    assert draft_compiler._native_energy_port({"type": "PFR"}, 0) == 1


def test_heat_exchanger_and_two_outlet_splitter_use_native_mode_fields() -> None:
    adapter = TypeAdapter(DraftOp)
    document = apply_ops(empty_document("Verified operation modes"), [
        adapter.validate_python({"op": "set_thermo", "compounds": ["Water"], "property_package": "NRTL"}),
        adapter.validate_python({"op": "add_unit", "id": "hx", "type": "HeatExchanger", "tag": "HX", "x": 0, "y": 0}),
        adapter.validate_python({"op": "set_unit_params", "unit": "hx", "mode": "calc_both_temp_ua",
                                "values": {"overall_coefficient": {"value": 1000, "unit": "W/[m2.K]"},
                                           "area": {"value": 1, "unit": "m2"}}}),
    ])
    assert draft_compiler.expected(document)["units"]["HX"]["CalculationMode"] == "CalcBothTemp_UA"
    unitop_set = next(args for name, args in draft_compiler.plan(document) if name == "dwsim_unitop_set")
    assert unitop_set["properties"]["CalculationMode"] == "CalcBothTemp_UA"
    assert "CalcMode" not in unitop_set["properties"]


def test_readback_comparison_covers_energy_duty_and_reaction_configuration() -> None:
    expected = {
        "compounds": [], "property_package": None, "objects": {}, "connections": [], "feeds": {},
        "energy_streams": {"Q": {"EnergyFlow": 12.0}},
        "reactions": {"R1": {"name": "hydration", "stoichiometry": {"A": -1.0, "B": 1.0},
                              "orders": {"A": 1.0, "B": 0.0}, "base_reactant": "A", "phase": "Mixture",
                              "basis": "MolarConc", "A_forward": 0.005, "A_forward_unit": "kmol/[m3.h]",
                              "E_forward": 1000.0, "E_forward_unit": "J/mol"}},
        "reaction_sets": {"JARVIS_R": ["R1"]}, "units": {},
    }
    actual = {**expected, "energy_streams": {"Q": {"EnergyFlow": 11.0}},
              "reactions": {"R1": {**expected["reactions"]["R1"], "A_forward": 0.006}}}
    paths = {item["path"] for item in draft_compiler.compare(expected, actual)}
    assert "energy_streams.Q.EnergyFlow" in paths
    assert "reactions.R1.A_forward" in paths


def test_kinetic_reaction_is_typed_attached_and_materialized_by_native_set() -> None:
    adapter = TypeAdapter(DraftOp)
    document = apply_ops(empty_document("Reactive train"), [
        adapter.validate_python({"op": "set_thermo", "compounds": ["Water", "Ethylene oxide", "Ethylene glycol"],
                                 "property_package": "NRTL"}),
        adapter.validate_python({"op": "set_reactions", "reactions": {
            "R1": {"name": "Hydration", "stoichiometry": {"Ethylene oxide": -1, "Water": -1,
                                                                      "Ethylene glycol": 1},
                   "orders": {"Ethylene oxide": 1}, "base_reactant": "Ethylene oxide", "phase": "Mixture",
                   "basis": "MolarConc", "A_forward": {"value": 0.005, "unit": "kmol/[m3.h]"},
                   "E_forward": {"value": 0, "unit": "J/mol"}},
        }}),
        adapter.validate_python({"op": "add_unit", "id": "pfr", "type": "PFR", "tag": "R", "x": 0, "y": 0}),
        adapter.validate_python({"op": "set_unit_params", "unit": "pfr", "mode": "heat_exchange",
                                 "reactions": ["R1"], "values": {
                                     "volume": {"value": 2, "unit": "m3"}, "length": {"value": 4, "unit": "m"},
                                     "overall_coefficient": {"value": 400, "unit": "W/[m2.K]"},
                                     "heat_exchange_area": {"value": 12, "unit": "m2"},
                                     "coolant_inlet_temperature": {"value": 26.85, "unit": "degC"},
                                     "coolant_mass_flow": {"value": 2, "unit": "kg/s"},
                                     "coolant_specific_heat": {"value": 4180, "unit": "J/kg.K"},
                                 }}),
    ])
    normalized = draft_compiler.expected(document)
    assert normalized["units"]["R"]["ReactorOperationMode"] == "HeatExchange"
    assert normalized["units"]["R"]["__ReactionSetID"] == "JARVIS_R"
    assert draft_compiler.plan(document)[-1] == ("dwsim_unitop_set", {"name": "R", "properties": {
        "ReactorOperationMode": "HeatExchange", "Volume": 2.0, "Length": 4.0, "DeltaP": 0.0,
        "OverallHeatTransferCoefficient": 400.0, "HeatExchangeArea": 12.0,
        "CoolantInletTemperature": 300.0, "CoolantMassFlowRate": 2.0,
        "CoolantSpecificHeat": 4180.0,
    }})


# ---------------------------------------------------------------- 158 required deterministic evidence

Q = lambda value, unit: {"value": value, "unit": unit}  # noqa: E731
_OPS: TypeAdapter[Any] = TypeAdapter(DraftOp)
_THERMO = {"op": "set_thermo", "compounds": ["Water", "Ethylene oxide", "Ethylene glycol"],
           "property_package": "NRTL"}
_FEED_SPEC = {"op": "set_stream_spec", "stream": "feed", "temperature": Q(25, "degC"), "pressure": Q(1, "bar"),
              "mass_flow": Q(1, "kg/s"), "composition": {"Water": 1.0}}
_HYDRATION: dict[str, Any] = {
    "name": "Hydration", "stoichiometry": {"Ethylene oxide": -1, "Water": -1, "Ethylene glycol": 1},
    "orders": {"Ethylene oxide": 1}, "base_reactant": "Ethylene oxide", "phase": "Mixture",
    "basis": "MolarConc", "A_forward": Q(0.005, "kmol/[m3.h]"), "E_forward": Q(0, "J/mol")}


def _doc(*ops: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    return apply_ops(base or empty_document("158"), [_OPS.validate_python(op) for op in ops])


def _codes(document: dict[str, Any]) -> set[str]:
    return {item["code"] for item in validate_document(document)}


def _refused(document: dict[str, Any], *ops: dict[str, Any]) -> str:
    with pytest.raises(DraftError) as caught:
        _doc(*ops, base=document)
    return caught.value.code


def _unit(uid: str, kind: str) -> dict[str, Any]:
    return {"op": "add_unit", "id": uid, "type": kind, "tag": uid.upper(), "x": 0, "y": 0}


def _link(stream: str, source: str | None, target: str | None, *, out: int = 0, into: int = 0,
          energy: bool = False) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [{"op": "add_stream", "id": stream, "tag": stream.upper(), "x": 0, "y": 0,
                                  "stream_type": "energy" if energy else "material"}]
    if source:
        ops.append({"op": "connect", "stream": stream, "end": "source", "unit": source, "port": out})
    if target:
        ops.append({"op": "connect", "stream": stream, "end": "target", "unit": target, "port": into})
    return ops


def _loop(with_recycle: bool) -> dict[str, Any]:
    """Mixer -> Heater -> Splitter with the splitter's second outlet returned to the mixer."""
    back = (_link("s3", "sp1", "rc1", out=1) + _link("s4", "rc1", "m1", into=1) if with_recycle
            else _link("s3", "sp1", "m1", out=1, into=1))
    return _doc({"op": "set_thermo", "compounds": ["Water"], "property_package": "NRTL"},
                _unit("m1", "Mixer"), _unit("h1", "Heater"), _unit("sp1", "Splitter"),
                *([_unit("rc1", "Recycle")] if with_recycle else []),
                *_link("s1", "m1", "h1"), *_link("s2", "h1", "sp1"), *back, *_link("out", "sp1", None))


def _reactive_train_ops() -> list[dict[str, Any]]:
    """Feed -> PFR (kinetic R1) -> heater (energy-stream mode) -> heat exchanger hot side."""
    return [_THERMO, {"op": "set_reactions", "reactions": {"R1": _HYDRATION}},
            _unit("pfr", "PFR"), _unit("h1", "Heater"), _unit("hx", "HeatExchanger"),
            {"op": "set_unit_params", "unit": "pfr", "mode": "adiabatic", "reactions": ["R1"],
             "values": {"volume": Q(2, "m3"), "length": Q(4, "m")}},
            {"op": "set_unit_params", "unit": "h1", "mode": "energy_stream"},
            *_link("feed", None, "pfr"), _FEED_SPEC, *_link("s1", "pfr", "h1"), *_link("s2", "h1", "hx"),
            *_link("q", None, "h1", energy=True),
            {"op": "set_stream_spec", "stream": "q", "duty": Q(50, "kW")}]


def _reactive_train() -> dict[str, Any]:
    return _doc(*_reactive_train_ops())


def test_process_loop_requires_a_recycle_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editor, "_client", lambda: pytest.fail("validation must not contact DWSIM"))
    monkeypatch.setattr(draft_compiler, "materialize", lambda *_a, **_k: pytest.fail("validation must not run DWSIM"))
    open_loop = [item for item in validate_document(_loop(False)) if item["code"] == "RECYCLE_REQUIRED"]
    assert len(open_loop) == 1 and open_loop[0]["field"] == "connections"
    assert {"M1", "H1", "SP1"} >= {open_loop[0]["object"]}
    assert "RECYCLE_REQUIRED" not in _codes(_loop(True))
    # The op layer refuses only the degenerate one-unit loop; multi-unit loops are a validation finding.
    assert _refused(_loop(False), _unit("h2", "Heater"), *_link("self", "h2", "h2")) == "port_invalid"


def test_reactor_and_energy_stream_units_need_reaction_set_and_energy_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editor, "_client", lambda: pytest.fail("validation must not contact DWSIM"))
    bare = _doc(_THERMO, _unit("pfr", "PFR"), _unit("h1", "Heater"), _unit("col", "DistillationColumn"),
                {"op": "set_unit_params", "unit": "pfr", "mode": "heat_exchange"},
                {"op": "set_unit_params", "unit": "h1", "mode": "energy_stream"})
    findings = {(item["object"], item["code"]) for item in validate_document(bare)}
    assert {("PFR", "REACTION_SET_MISSING"), ("PFR", "REACTOR_ENERGY_STREAM_MISSING"),
            ("H1", "UNIT_ENERGY_INLET_MISSING"), ("COL", "UNIT_ENERGY_INLET_MISSING"),
            ("COL", "UNIT_ENERGY_OUTLET_MISSING")} <= findings
    wired = _doc({"op": "set_reactions", "reactions": {"R1": _HYDRATION}},
                 {"op": "set_unit_params", "unit": "pfr", "reactions": ["R1"]},
                 *_link("qr", None, "pfr", energy=True), *_link("qh", None, "h1", energy=True),
                 *_link("qb", None, "col", energy=True), *_link("qc", "col", None, energy=True), base=bare)
    remaining = {(item["object"], item["code"]) for item in validate_document(wired)}
    assert not {code for _tag, code in remaining} & {"REACTION_SET_MISSING", "UNIT_ENERGY_INLET_MISSING", "REACTOR_ENERGY_STREAM_MISSING",
                                                      "UNIT_ENERGY_OUTLET_MISSING"}
    # A heater that is not in energy-stream mode does not need one, and a duty on its stream is flagged.
    fixed = _doc({"op": "set_unit_params", "unit": "h1", "mode": "outlet_temperature"},
                 {"op": "set_stream_spec", "stream": "qh", "duty": Q(10, "kW")}, base=wired)
    assert ("QH", "ENERGY_DUTY_MODE_MISMATCH") in {(item["object"], item["code"]) for item in validate_document(fixed)}
    assert ("H1", "UNIT_ENERGY_INLET_MISSING") not in {(item["object"], item["code"])
                                                       for item in validate_document(fixed)}
    assert _refused(bare, {"op": "set_unit_params", "unit": "pfr", "reactions": ["NOPE"]}) == "reaction_not_found"


def test_energy_streams_connect_only_to_registered_energy_ports() -> None:
    document = _doc(_THERMO, _unit("h1", "Heater"), _unit("m1", "Mixer"), _unit("col", "DistillationColumn"),
                    {"op": "add_stream", "id": "q", "tag": "Q", "stream_type": "energy", "x": 0, "y": 0},
                    {"op": "add_stream", "id": "q2", "tag": "Q2", "stream_type": "energy", "x": 0, "y": 0})
    for op in ({"op": "connect", "stream": "q", "end": "target", "unit": "h1", "port": 1},  # one energy inlet
               {"op": "connect", "stream": "q", "end": "source", "unit": "h1", "port": 0},  # no energy outlet
               {"op": "connect", "stream": "q", "end": "target", "unit": "m1", "port": 0},  # no energy ports
               {"op": "connect", "stream": "q", "end": "source", "unit": "col", "port": 1}):
        assert _refused(document, op) == "port_invalid", op
    occupied = _doc({"op": "connect", "stream": "q", "end": "target", "unit": "h1", "port": 0}, base=document)
    assert _refused(occupied, {"op": "connect", "stream": "q2", "end": "target", "unit": "h1",
                               "port": 0}) == "port_occupied"
    # Material and energy ports are distinct namespaces: material inlet 0 stays free.
    shared = _doc(*_link("feed", None, "h1"), _FEED_SPEC, {"op": "set_unit_params", "unit": "h1", "mode": "energy_stream"},
                  base=occupied)
    assert shared["objects"]["feed"]["target"] == {"unit": "h1", "port": 0}
    assert _refused(occupied, {"op": "set_stream_spec", "stream": "q",
                               "temperature": Q(300, "K")}) == "spec_on_energy_stream"
    assert draft_compiler.expected(shared)["connections"] == ["FEED>H1:in0", "Q>H1:in1"]


def test_reaction_stoichiometry_compounds_and_base_reactant_are_checked() -> None:
    document = _doc(_THERMO)
    cases = [
        ({"stoichiometry": {"Ethylene oxide": -1, "Methanol": 1}}, "compound_undeclared"),
        ({"orders": {"Methanol": 1}}, "compound_undeclared"),
        ({"base_reactant": "Ethylene glycol"}, "reaction_base_invalid"),  # a product, not consumed
        ({"base_reactant": "Methanol"}, "reaction_base_invalid"),  # not in the stoichiometry at all
        ({"A_forward": Q(0.005, "mol/[m3.s]")}, "reaction_unit_unsupported"),
    ]
    for override, code in cases:
        op = {"op": "set_reactions", "reactions": {"R1": {**_HYDRATION, **override}}}
        assert _refused(document, op) == code, override
    assert _refused(document, {"op": "set_reactions", "reactions": {"1bad": _HYDRATION}}) == "reaction_id_invalid"
    for override in ({"stoichiometry": {"Ethylene oxide": 0, "Water": 1}}, {"orders": {"Water": -1}},
                     {"A_forward": Q(-1, "kmol/[m3.h]")}):
        with pytest.raises(ValueError):
            _OPS.validate_python({"op": "set_reactions", "reactions": {"R1": {**_HYDRATION, **override}}})
    accepted = _doc({"op": "set_reactions", "reactions": {"R1": _HYDRATION}}, base=document)
    assert accepted["reactions"]["R1"]["base_reactant"] == "Ethylene oxide"


def test_split_ratios_must_close_for_two_outlets() -> None:
    document = _doc({"op": "set_thermo", "compounds": ["Water"], "property_package": "NRTL"},
                    _unit("sp", "Splitter"), *_link("a", "sp", None), *_link("b", "sp", None, out=1),
                    {"op": "set_unit_params", "unit": "sp", "values": {"split_ratio_1": Q(0.5, "dimensionless"),
                                                                        "split_ratio_2": Q(0.3, "dimensionless")}})
    assert "SPLIT_RATIOS_INVALID" in _codes(document)
    closed = _doc({"op": "set_unit_params", "unit": "sp", "values": {"split_ratio_2": Q(0.5, "dimensionless")}}, base=document)
    assert "SPLIT_RATIOS_INVALID" not in _codes(closed)


def test_readback_mismatch_is_reported_on_new_unit_reaction_and_energy_connection() -> None:
    document = _reactive_train()
    expected = draft_compiler.expected(document)
    assert draft_compiler.compare(expected, copy.deepcopy(expected)) == []
    energy_link = next(item for item in expected["connections"] if item.startswith("Q>"))
    assert energy_link == "Q>H1:in1"
    actual = copy.deepcopy(expected)
    actual["units"]["HX"]["OverallCoefficient"] = 900.0
    actual["reactions"]["R1"]["stoichiometry"]["Water"] = -2.0
    actual["reactions"]["R1"]["base_reactant"] = "Water"
    actual["reaction_sets"]["JARVIS_PFR"] = []
    actual["connections"].remove(energy_link)
    diffs = {item["path"]: item for item in draft_compiler.compare(expected, actual)}
    assert diffs["units.HX.OverallCoefficient"]["expected"] == 1000.0
    assert diffs["units.HX.OverallCoefficient"]["actual"] == 900.0
    assert "reactions.R1.stoichiometry.Water" in diffs and "reactions.R1.base_reactant" in diffs
    assert "reaction_sets.JARVIS_PFR[R1]" in diffs
    assert diffs[f"connections[{energy_link}]"]["actual"] == "<missing>"
    # A connection to the wrong native energy port is also a mismatch, not a silent pass.
    wrong_port = copy.deepcopy(expected)
    wrong_port["connections"] = sorted([*(item for item in expected["connections"] if item != energy_link), "Q>H1:in0"])
    assert {item["path"] for item in draft_compiler.compare(expected, wrong_port)} == {
        f"connections[{energy_link}]", "connections[Q>H1:in0]"}


def test_route_ops_leave_materialization_fingerprint_and_results_state_unchanged() -> None:
    document = _reactive_train()
    routed = _doc({"op": "set_route", "stream": "s1", "points": [{"x": 0, "y": 0}, {"x": 40, "y": 0},
                                                                 {"x": 40, "y": 30}]},
                  {"op": "set_route", "stream": "q", "points": []}, base=document)
    assert routed["objects"]["s1"]["route"] and routed != document
    assert draft_compiler.expected(routed) == draft_compiler.expected(document)
    assert draft_compiler.plan(routed) == draft_compiler.plan(document)

    def fp(value: dict[str, Any]) -> str:
        return draft_compiler.fingerprint(draft_compiler.expected(value), dwsim_version="10.2.9", mcp_sha256="a" * 64)

    assert fp(routed) == fp(document)
    solved = {"run_id": "run-1", "action": "run", "status": "completed", "draft_revision": "4:solved",
              "started_at": "2026-01-01T00:00:00+00:00", "dwsim_version": "10.2.9", "mcp_sha256": "a" * 64,
              "materialization_fingerprint": fp(document)}
    head = {"revision": "6:routed", "seq": 6}
    state = draft.results_state(head, [solved], routed, edits_since=0)
    assert state["state"] == "current" and state["edits_since"] == 0
    moved = _doc({"op": "move", "id": "h1", "x": 10, "y": 0}, base=routed)
    stale = draft.results_state({"revision": "7:moved", "seq": 7}, [solved], moved, edits_since=1)
    assert stale["state"] == "stale" and stale["edits_since"] == 1
    assert draft.results_state(head, [], routed)["state"] == "none"


@pytest.fixture()
def workspace_draft() -> Any:
    initialize_database()
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "158 draft", "slug": f"draft-158-{id(client)}"})
        workspace.raise_for_status()
        workspace_id = workspace.json()["id"]
        yield client, workspace_id, draft.create_draft(workspace_id, "158 train")


def _patched(workspace_id: str, state: dict[str, Any], *ops: dict[str, Any]) -> dict[str, Any]:
    return draft.patch(workspace_id, state["draft_id"], state["revision"], [_OPS.validate_python(op) for op in ops])


def test_route_only_revisions_do_not_count_as_edits_since_a_run(workspace_draft: Any) -> None:
    _client, workspace_id, state = workspace_draft
    build = [_THERMO, {"op": "set_reactions", "reactions": {"R1": _HYDRATION}}, _unit("pfr", "PFR"),
             {"op": "set_unit_params", "unit": "pfr", "reactions": ["R1"],
              "values": {"volume": Q(2, "m3"), "length": Q(4, "m")}},
             *_link("feed", None, "pfr"), _FEED_SPEC, *_link("out", "pfr", None)]
    state = _patched(workspace_id, state, *build)
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    document = draft.load_revision(directory, state["revision"])["document"]
    draft.record_run(directory, {
        "run_id": "run-158", "action": "run", "status": "completed", "draft_revision": state["revision"],
        "started_at": "2026-01-01T00:00:00+00:00", "dwsim_version": "10.2.9", "mcp_sha256": "a" * 64,
        "materialization_fingerprint": draft_compiler.fingerprint(
            draft_compiler.expected(document), dwsim_version="10.2.9", mcp_sha256="a" * 64),
        "process_fingerprint": draft_compiler.fingerprint(
            draft_compiler.process_view(draft_compiler.expected(document)), dwsim_version="10.2.9",
            mcp_sha256="a" * 64)})
    assert draft.projection(workspace_id, state["draft_id"])["results"]["state"] == "current"
    route = {"op": "set_route", "stream": "out", "points": [{"x": 0, "y": 0}, {"x": 0, "y": 50}]}
    state = _patched(workspace_id, state, route)
    state = _patched(workspace_id, state, {**route, "points": [{"x": 0, "y": 0}, {"x": 60, "y": 0}]})
    assert state["results"]["state"] == "current" and state["results"]["edits_since"] == 0
    # Spec 162: moving a unit is layout too, so it keeps results current.
    state = _patched(workspace_id, state, {"op": "move", "id": "pfr", "x": 5, "y": 5})
    assert state["results"]["state"] == "current"
    state = _patched(workspace_id, state, {"op": "set_unit_params", "unit": "pfr", "values": {"volume": Q(3, "m3")}})
    assert state["results"]["state"] == "stale"
    assert state["results"]["edits_since"] == 1  # the route and move revisions are not counted


def test_orientation_round_trips_as_revisioned_layout_without_staling_results(workspace_draft: Any) -> None:
    _client, workspace_id, state = workspace_draft
    state = _patched(workspace_id, state, *_reactive_train_ops())
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    document = draft.load_revision(directory, state["revision"])["document"]
    expected = draft_compiler.expected(document)
    process_fp = draft_compiler.fingerprint(draft_compiler.process_view(expected), dwsim_version="10.2.9",
                                             mcp_sha256="a" * 64)
    draft.record_run(directory, {"run_id": "orientation-run", "action": "run", "status": "completed",
        "draft_revision": state["revision"], "started_at": "2026-01-01T00:00:00+00:00",
        "dwsim_version": "10.2.9", "mcp_sha256": "a" * 64,
        "materialization_fingerprint": draft_compiler.fingerprint(expected, dwsim_version="10.2.9",
                                                                    mcp_sha256="a" * 64),
        "process_fingerprint": process_fp})
    next_state = _patched(workspace_id, state, {"op": "set_orientation", "id": "pfr", "flip_x": True,
                                                "flip_y": True})
    assert next_state["seq"] == state["seq"] + 1
    unit = next(item for item in next_state["objects"] if item["id"] == "pfr")
    assert unit["flip_x"] is True and unit["flip_y"] is True
    rotated = draft.load_revision(directory, next_state["revision"])["document"]
    assert draft_compiler.expected(rotated) == expected
    assert draft_compiler.plan(rotated) == draft_compiler.plan(document)
    assert next_state["results"]["state"] == "current"
    assert next_state["results"]["edits_since"] == 0


def test_incomplete_equipment_after_run_projects_stale_results_instead_of_failing(workspace_draft: Any) -> None:
    _client, workspace_id, state = workspace_draft
    state = _patched(workspace_id, state, *_reactive_train_ops())
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    document = draft.load_revision(directory, state["revision"])["document"]
    expected = draft_compiler.expected(document)
    draft.record_run(directory, {"run_id": "incomplete-edit-run", "action": "run", "status": "completed",
        "draft_revision": state["revision"], "started_at": "2026-01-01T00:00:00+00:00",
        "dwsim_version": "10.2.9", "mcp_sha256": "a" * 64,
        "materialization_fingerprint": draft_compiler.fingerprint(expected, dwsim_version="10.2.9",
                                                                    mcp_sha256="a" * 64),
        "process_fingerprint": draft_compiler.fingerprint(draft_compiler.process_view(expected),
            dwsim_version="10.2.9", mcp_sha256="a" * 64)})
    edited = _patched(workspace_id, state, {"op": "add_unit", "id": "newpfr", "type": "PFR", "tag": "PFR2",
                                            "x": 300, "y": 300})
    assert edited["results"]["state"] == "stale"
    assert "UNIT_PARAM_MISSING" in {item["code"] for item in edited["findings"]}


def test_feed_alternatives_compile_only_probe_proven_mcp_inputs() -> None:
    document = _doc(_THERMO, _unit("h1", "Heater"),
                    {"op": "set_unit_params", "unit": "h1", "values": {"outlet_temperature": Q(80, "degC")}},
                    *_link("feed", None, "h1"),
                    {"op": "set_stream_spec", "stream": "feed", "pressure": Q(1.01325, "bar"),
                     "vapor_fraction": Q(0.3, "dimensionless"), "molar_flow": Q(40, "mol/s"),
                     "composition_basis": "mole", "composition": {"Water": 0.5, "Ethylene oxide": 0.5}},
                    *_link("out", "h1", None))
    calls = draft_compiler.plan(document)
    assert ("dwsim_stream_add_material", {"name": "FEED", "pressure_Pa": 101325.0}) in calls
    assert ("dwsim_stream_set_conditions", {"name": "FEED", "molar_flow_mol_s": 40.0}) in calls
    mole_call = next(args for name, args in calls if name == "dwsim_unitop_set" and args.get("name") == "FEED"
                     and "PROP_MS_102/Water" in args["properties"])
    assert mole_call["properties"] == {"PROP_MS_102/Water": 0.5, "PROP_MS_102/Ethylene oxide": 0.5,
                                       "PROP_MS_102/Ethylene glycol": 0.0}
    assert ("dwsim_unitop_set", {"name": "FEED", "properties": {
        "SpecType": "Pressure_and_VaporFraction", "PROP_MS_27": 0.3}}) in calls
    assert not any(item["severity"] == "blocker" for item in draft.validate_document(document))
    with pytest.raises(DraftError, match="alternatives"):
        _doc(_THERMO, _unit("h1", "Heater"), *_link("feed", None, "h1"), _FEED_SPEC,
             {"op": "set_stream_spec", "stream": "feed", "mass_flow": Q(1, "kg/s"),
              "molar_flow": Q(1, "mol/s")})
    zero = _doc(_THERMO, _unit("h1", "Heater"), *_link("feed", None, "h1"),
                {"op": "set_stream_spec", "stream": "feed", "pressure": Q(1.01325, "bar"),
                 "temperature": Q(25, "degC"), "mass_flow": Q(0, "kg/s"),
                 "composition": {"Water": 1.0}}, *_link("out", "h1", None))
    findings = validate_document(zero)
    assert any(item["code"] == "FEED_FLOW_ZERO" and item["severity"] == "warning" for item in findings)
    missing = _doc(_THERMO, _unit("h1", "Heater"), *_link("feed", None, "h1"), *_link("out", "h1", None))
    assert any(item["code"] == "FEED_SPEC_MISSING" and item["severity"] == "blocker" for item in validate_document(missing))


def test_process_read_view_contains_guidance_results_and_dwsim_errors(workspace_draft: Any) -> None:
    _client, workspace_id, state = workspace_draft
    state = _patched(workspace_id, state, *_reactive_train_ops())
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    run = {"run_id": "failed-run", "action": "run", "status": "runtime_failed", "draft_revision": state["revision"],
           "started_at": "2026-01-01T00:00:00+00:00", "error": "RuntimeError",
           "error_detail": {"step": "dwsim_solve_run", "dwsim_message": "DWSIM could not solve fixture"},
           "dwsim_check": {"findings": [{"severity": "warning", "code": "W", "object": "H1",
                                          "message": "DWSIM warning", "fix": "Inspect inputs"}]},
           "solve": {"errors": ["solver convergence failed"], "failed_objects": [{"tag": "H1", "error": "not calculated"}]}}
    feedback = draft.dwsim_feedback(run)
    assert feedback["solve_errors"] == ["solver convergence failed"]
    assert feedback["dwsim_message"] == "DWSIM could not solve fixture"
    assert feedback["check_findings"][0]["message"] == "DWSIM warning"
    document = draft.load_revision(directory, state["revision"])["document"]
    expected = draft_compiler.expected(document)
    draft.record_run(directory, {"run_id": "c" * 32, "action": "run", "status": "completed",
        "draft_revision": state["revision"], "started_at": "2026-01-02T00:00:00+00:00",
        "dwsim_version": "10.2.9", "mcp_sha256": "a" * 64,
        "materialization_fingerprint": draft_compiler.fingerprint(expected, dwsim_version="10.2.9",
                                                                    mcp_sha256="a" * 64),
        "process_fingerprint": draft_compiler.fingerprint(draft_compiler.process_view(expected),
            dwsim_version="10.2.9", mcp_sha256="a" * 64),
        "streams": {"Feed": {"display": {"molar_flow": {"value": 40, "unit": "kmol/h"}},
                              "vapor_fraction": 0.25},
                    "S1": {"display": {"mass_flow": {"value": 3600, "unit": "kg/h"}}},
                    "S2": {"display": {}}, "Q": {"display": {}}, "Product": {"display": {}}},
        "units": {"PFR": {"calculated": True, "reported": {}}}})
    run["started_at"] = "2026-01-03T00:00:00+00:00"
    draft.record_run(directory, run)
    view = draft.agent_view(workspace_id, state["draft_id"])
    assert view["guidance"] and isinstance(view["blockers"], list) and isinstance(view["warnings"], list)
    assert view["dwsim"]["solve_errors"] and view["dwsim"]["error"] == "RuntimeError"
    # Spec 181: the later failed Run of the same revision demotes the older converged answer to stale.
    assert view["current_results"]["state"] == "stale"
    assert view["current_results"]["streams"]["Feed"]["molar_flow"] == "40 kmol/h"
    assert view["dwsim"]["outcome"]["state"] == "failed"


def test_real_dwsim_solver_failure_text_reaches_hermes_view(workspace_draft: Any) -> None:
    _client, workspace_id, state = workspace_draft
    state = _patched(workspace_id, state, *_reactive_train_ops())
    fixture_path = _ROOT / "backend" / "app" / "modules" / "process_stack" / "evidence" / "162" / "real_dwsim_failure_fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    message = "Could not converge to a valid solution. Please check the column specs"
    raw_solve = fixture["raw_dwsim"]["dwsim_solve_run"]
    assert fixture["dwsim_version"] == "10.2.9"
    assert raw_solve["ok"] is False
    assert message in raw_solve["errors"][0]

    run = copy.deepcopy(fixture["process_run"])
    run.update(action="run", run_id="real-dwsim-failure", draft_revision=state["revision"],
               started_at="2026-01-04T00:00:00+00:00")
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    draft.record_run(directory, run)

    view = draft.agent_view(workspace_id, state["draft_id"])
    assert message in view["dwsim"]["solve_errors"][0]
    assert view["dwsim"]["failed_objects"][0] == {"tag": "COL", "error": message}


def test_hermes_view_deduplicates_solver_feedback_and_groups_uncalculated_objects(workspace_draft: Any) -> None:
    _client, workspace_id, state = workspace_draft
    state = _patched(workspace_id, state, *_reactive_train_ops())
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    run = {"run_id": "deduplicated-failure", "action": "run", "status": "failed",
           "draft_revision": state["revision"], "started_at": "2026-01-05T00:00:00+00:00",
           "solve": {"errors": ["COL: convergence failed", "COL: convergence failed"],
                     "failed_objects": [
                         {"tag": "COL", "error": "convergence failed"},
                         {"tag": "COL", "error": "convergence failed"},
                         {"tag": "Distillate", "error": ""},
                         {"tag": "Condenser", "error": ""},
                         {"tag": "Bottoms", "error": ""},
                         {"tag": "Bottoms", "error": ""},
                     ]}}
    draft.record_run(directory, run)

    view = draft.agent_view(workspace_id, state["draft_id"])

    assert view["dwsim"]["solve_errors"] == ["COL: convergence failed"]
    assert view["dwsim"]["failed_objects"] == [
        {"tag": "COL", "error": "convergence failed"},
        {"tag": "Distillate", "error": "not calculated"},
        {"tag": "Condenser", "error": "not calculated"},
        {"tag": "Bottoms", "error": "not calculated"},
    ]
    assert "DWSIM did not calculate: Distillate, Condenser, Bottoms" in view["warnings"]
    assert view["findings"].count("Bottoms: DWSIM did not calculate it: no error text") == 1
    assert view["warning_count"] == len(view["warnings"])
    assert view["blocker_count"] == len(view["blockers"])


def test_only_input_properties_are_proposable(workspace_draft: Any) -> None:
    client, workspace_id, state = workspace_draft
    state = _patched(workspace_id, state, *_reactive_train_ops())
    view = draft.agent_view(workspace_id, state["draft_id"])
    registry = {item["type"]: item for item in registry_projection()["units"]}
    for unit_type, allowed in view["proposable"].items():
        if unit_type == "stream":
            assert set(allowed) == {"temperature", "pressure", "mass_flow", "molar_flow", "vapor_fraction",
                                    "composition"}
            continue
        inputs = {param["key"] for param in registry[unit_type]["params"] if param["classification"] == "input"}
        assert set(allowed) == {"mode"} | inputs, unit_type
        results = {row["name"] for row in registry[unit_type]["result_properties"]}
        assert not results & set(allowed), unit_type
    url = f"/workspaces/{workspace_id}/process/drafts/{state['draft_id']}/proposals"
    heater_results = [row["name"] for row in registry["Heater"]["result_properties"]]
    assert heater_results, "the manifest reports Heater result properties"
    for target, prop in [("H1", heater_results[0]), ("H1", "DeltaQ"), ("HX", "OverallCoefficient"),
                         ("PFR", "reactions"), ("S1", "enthalpy"), ("Q", "EnergyFlow")]:
        response = client.post(url, json={"base_revision": state["revision"], "changes": [
            {"target": target, "property": prop, "proposed": Q(1, "kW")}]})
        assert response.status_code == 422, (target, prop, response.text)
        assert response.json()["detail"]["code"] == "proposal_property_unsupported", (target, prop)
    accepted = client.post(url, json={"base_revision": state["revision"], "changes": [
        {"target": "HX", "property": "overall_coefficient", "proposed": Q(800, "W/[m2.K]")}]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["changes"][0]["current"] == {"value": 1000.0, "unit": "W/[m2.K]"}


def test_negative_canvas_coordinates_are_shifted_consistently_into_dwsim_space() -> None:
    document = _synthetic_document("Heater", "outlet_temperature")
    positive_plan = draft_compiler.plan(document)
    assert draft_compiler.expected(document)["objects"]["U"] == {"type": "Heater", "x": 0, "y": 0}
    document["objects"]["u"].update(x=-40, y=-120)
    document["objects"]["p0"]["y"] = 30
    objects = draft_compiler.expected(document)["objects"]
    assert objects["U"]["x"] == 0 and objects["U"]["y"] == 0 and objects["P0"]["y"] == 150
    edits = {args["name"]: (args["x"], args["y"]) for name, args in draft_compiler.plan(document)
             if name == "dwsim_graphic_edit"}
    assert all(edits[tag] == (item["x"], item["y"]) for tag, item in objects.items())
    assert min(min(xy) for xy in edits.values()) >= 0
    assert len(positive_plan) == len(draft_compiler.plan(document))


@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_run_with_objects_above_and_left_of_origin_completes() -> None:
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        DraftQuantity,
        SetStreamSpec,
        SetThermo,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state = draft.create_draft(workspace_id, "negative layout")
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddStream(op="add_stream", id="f", tag="Feed", x=-80, y=0),
        AddUnit(op="add_unit", id="h", type="Heater", tag="Heater", x=100, y=0),
        AddStream(op="add_stream", id="p", tag="Product", x=200, y=-50),
        Connect(op="connect", stream="f", end="target", unit="h", port=0),
        Connect(op="connect", stream="p", end="source", unit="h", port=0),
        SetStreamSpec(op="set_stream_spec", stream="f", pressure=DraftQuantity(value=1.0, unit="bar"),
                      temperature=DraftQuantity(value=300, unit="K"),
                      mass_flow=DraftQuantity(value=0.01, unit="kg/s"), composition={"Water": 1.0},
                      composition_basis="mass"),
        SetUnitParams(op="set_unit_params", unit="h", mode="outlet_temperature",
                      values={"outlet_temperature": DraftQuantity(value=320, unit="K")}),
    ])
    run = draft.execute(workspace_id, state["draft_id"], state["revision"], "run")["run"]
    assert run["status"] == "completed", run.get("materialization_diffs") or run.get("error")
    assert abs(run["streams"]["Product"]["temperature_K"] - 320) < 1e-6


def test_native_recycle_mass_flow_tolerance_scales_with_feed_and_is_read_back() -> None:
    document = _synthetic_document("Recycle", None)
    feed = next(item for item in document["objects"].values() if item["kind"] == "stream" and item["source"] is None)
    feed["spec"]["mass_flow"] = {"si": 0.01}
    tolerance = draft_compiler.recycle_mass_flow_tolerance_kg_s(document)
    assert tolerance == pytest.approx(1e-7)
    assert draft_compiler.expected(document)["units"]["U"] == {"__MassFlowTolerance": tolerance}
    sets = [args["properties"] for name, args in draft_compiler.plan(document)
            if name == "dwsim_unitop_set" and args["name"] == "U"]
    assert sets == [{"PROP_RY_1": pytest.approx(tolerance * 3600.0)}]
    feed["spec"] = {**{k: v for k, v in feed["spec"].items() if k != "mass_flow"}, "molar_flow": {"si": 1.0}}
    assert draft_compiler.recycle_mass_flow_tolerance_kg_s(document) == pytest.approx(1e-5 * 0.002016)


@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_small_flow_native_recycle_closes_its_mass_balance() -> None:
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        DraftQuantity,
        SetStreamSpec,
        SetThermo,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state = draft.create_draft(workspace_id, "small recycle")
    streams = (("f", "Feed", 0), ("hot", "Hot", 200), ("mixed", "Mixed", 400), ("purge", "Purge", 500),
               ("back", "Back", 450), ("torn", "Torn", 300))
    wiring = (("f", "target", "heater", 0), ("hot", "source", "heater", 0), ("hot", "target", "mixer", 0),
              ("mixed", "source", "mixer", 0), ("mixed", "target", "split", 0), ("purge", "source", "split", 0),
              ("back", "source", "split", 1), ("back", "target", "rec", 0), ("torn", "source", "rec", 0),
              ("torn", "target", "mixer", 1))
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddUnit(op="add_unit", id="heater", type="Heater", tag="Heater", x=150, y=0),
        AddUnit(op="add_unit", id="mixer", type="Mixer", tag="Mixer", x=250, y=0),
        AddUnit(op="add_unit", id="split", type="Splitter", tag="Split", x=450, y=0),
        AddUnit(op="add_unit", id="rec", type="Recycle", tag="Rec", x=350, y=80),
        *[AddStream(op="add_stream", id=sid, tag=tag, x=x, y=0) for sid, tag, x in streams],
        *[Connect(op="connect", stream=sid, end=end, unit=unit, port=port) for sid, end, unit, port in wiring],
        SetStreamSpec(op="set_stream_spec", stream="f", pressure=DraftQuantity(value=1.0, unit="bar"),
                      temperature=DraftQuantity(value=300, unit="K"),
                      mass_flow=DraftQuantity(value=0.01, unit="kg/s"), composition={"Water": 1.0},
                      composition_basis="mass"),
        SetUnitParams(op="set_unit_params", unit="heater", mode="outlet_temperature",
                      values={"outlet_temperature": DraftQuantity(value=298.15, unit="K")}),
        SetUnitParams(op="set_unit_params", unit="split", mode="split_ratios", values={
            "split_ratio_1": DraftQuantity(value=0.9, unit="dimensionless"),
            "split_ratio_2": DraftQuantity(value=0.1, unit="dimensionless")}),
    ])
    run = draft.execute(workspace_id, state["draft_id"], state["revision"], "run")["run"]
    assert run["status"] == "completed", run.get("materialization_diffs") or run.get("error")
    # DWSIM's default 36 kg/h tolerance reported this loop as solved with Purge 0.019 kg/s and boiling water.
    assert run["streams"]["Purge"]["mass_flow_kg_s"] == pytest.approx(0.01, rel=2e-5)
    assert run["streams"]["Mixed"]["mass_flow_kg_s"] == pytest.approx(0.01 / 0.9, rel=2e-5)
    assert run["streams"]["Mixed"]["vapor_fraction"] == 0.0
