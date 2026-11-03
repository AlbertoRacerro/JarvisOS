"""Normalize checked-in DWSIM 10.2.9 probe captures into the 158 capability manifest.

Every settable property, mode and native port carries the capture path that proves it
(``<file>:<json path>``). A few facts have no checked-in capture; they are declared below
with their explicit non-capture source and never mixed with captured evidence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "backend/app/modules/process_stack/evidence/158"
OUT = ROOT / "backend/app/modules/process_stack/dwsim_10_2_9_manifest.json"
TYPES = (
    "Heater",
    "Cooler",
    "Pump",
    "Valve",
    "Mixer",
    "Vessel",
    "Splitter",
    "HeatExchanger",
    "Recycle",
    "PFR",
    "CSTR",
    "DistillationColumn",
)
CAPTURES = ("units.json", "ports.json", "pfr_ok.json", "column2.json", "inputs.json", "cstr_180.json",
            "recycle_tolerance.json")
# Native object type names reported by solve/DOF/result captures -> manifest type.
NATIVE_ALIASES = {"NodeOut": "Splitter", "NodeIn": "Mixer", "RCT_PFR": "PFR", "PFR reactor": "PFR",
                  "RCT_CSTR": "CSTR", "CSTR reactor": "CSTR"}
# Properties whose written value selects a calculation mode or a native enum.
MODE_PROPERTIES = ("CalcMode", "CalculationMode", "OperationMode", "ReactorOperationMode")
ENUM_PROPERTIES = ("CondenserType",)

SPEC_FINDINGS = "docs/specs/158-process-dwsim-parity-beta.md#probe-findings"
COMPILER = "backend/app/modules/process_stack/draft_compiler.py"
SMOKE_155 = "scripts/qualification/155/draft_smoke.py (155 exact-head host acceptance; capture not checked in)"
COLUMN_XML = (
    f"{COMPILER}:_patch_native_xml native connector 10 plus reload, verified by read-back; "
    "the MCP refuses this connection (spec 158 probe findings)"
)
# ReactorOperationMode is an enum on the native object rather than a calculation-mode list
# returned by dwsim_unitop_get_results; only Adiabatic and HeatExchange are in a checked-in
# capture, the other alternatives are recorded by the spec's pinned-runtime probe findings.
PFR_MODES = ("Isothermic", "Adiabatic", "OutletTemperature", "NonIsothermalNonAdiabatic", "HeatExchange")
DECLARED_MODES: dict[str, dict[str, str]] = {"PFR": {mode: SPEC_FINDINGS for mode in PFR_MODES},
                                            "CSTR": {mode: "evidence/pbr/pDWSIM4/q1b_raw.json"
                                                     for mode in ("Isothermic", "OutletTemperature")}}
# Native ports without a checked-in connection capture: (type, role, port) -> source.
DECLARED_PORTS: dict[tuple[str, str, int], str] = {
    ("Cooler", "feed", 0): f"{COMPILER}:plan (draft material port is the native port; Heater-family wiring)",
    ("Cooler", "product", 0): f"{COMPILER}:plan (draft material port is the native port; Heater-family wiring)",
    ("Cooler", "energy_feed", 1): f"{SPEC_FINDINGS} (Heater and Cooler energy feed on native port 1)",
    ("Pump", "feed", 0): SMOKE_155,
    ("Pump", "product", 0): SMOKE_155,
    ("Valve", "feed", 0): SMOKE_155,
    ("Valve", "product", 0): SMOKE_155,
    ("Vessel", "feed", 0): SMOKE_155,
    ("Vessel", "product", 0): f"{SMOKE_155} (vapor)",
    ("Vessel", "product", 1): f"{SMOKE_155} (liquid)",
    ("Mixer", "feed", 0): f"{COMPILER}:plan (155 registry: 2-3 feeds)",
    ("Mixer", "feed", 1): f"{COMPILER}:plan (155 registry: 2-3 feeds)",
    ("Mixer", "feed", 2): f"{COMPILER}:plan (155 registry: 2-3 feeds)",
    ("Mixer", "product", 0): f"{COMPILER}:plan (155 registry)",
    ("Recycle", "feed", 0): f"{SPEC_FINDINGS} (Recycle: 1 inlet and 1 outlet)",
    ("Recycle", "product", 0): f"{SPEC_FINDINGS} (Recycle: 1 inlet and 1 outlet)",
    ("CSTR", "feed", 0): "evidence/pbr/pDWSIM4/q1b_raw.json",
    ("CSTR", "product", 0): "evidence/pbr/pDWSIM4/q1b_raw.json",
    ("CSTR", "energy_feed", 1): "evidence/pbr/pDWSIM4/q1b_raw.json",
    ("DistillationColumn", "energy_feed", 10): f"{COLUMN_XML} (reboiler duty)",
    ("DistillationColumn", "energy_product", 10): f"{COLUMN_XML} (condenser duty)",
}
# Inputs the compiler writes into the saved native case rather than through dwsim_unitop_set.
NATIVE_XML_INPUTS: dict[str, dict[str, dict[str, str]]] = {
    "DistillationColumn": {
        "__FeedStage": {
            "xml": "MaterialStreams/StreamInformation[StreamBehavior=Feed]/AssociatedStage",
            "source": f"{COMPILER}:_patch_native_xml; no MCP feed-stage property (column2.json:try_FeedStage)",
        },
        "__TopPressure": {
            "xml": "Stages/Stage[0]/P",
            "source": f"{COMPILER}:_patch_native_xml; MCP refuses Stages[0].P (column2.json:try_Stages[0].P)",
        },
        "__BottomPressure": {
            "xml": "Stages/Stage[N-1]/P",
            "source": f"{COMPILER}:_patch_native_xml; no MCP stage-pressure property",
        },
        "__CondenserSpec": {
            "xml": "Specs/Spec[@ID='C']/SpecValue",
            "source": f"{COMPILER}:_patch_native_xml; also set as Condenser_Specification_Value",
        },
        "__ReboilerSpec": {
            "xml": "Specs/Spec[@ID='R']/SpecValue",
            "source": f"{COMPILER}:_patch_native_xml; also set as Reboiler_Specification_Value",
        },
    },
    "Recycle": {
        "__MassFlowTolerance": {
            "xml": "ConvergenceParameters/VazaoMassica (kg/s)",
            "source": "recycle_tolerance.json:Recycle.set (written as PROP_RY_1 in kg/h, read back in kg/s)",
        },
    },
    "PFR": {
        "__ReactionSetID": {
            "xml": "SimulationObject/ReactionSetID plus Reactions/ReactionSets",
            "source": "pfr_ok.json:load and pfr_ok.json:setrs (native reaction set reloaded)",
        },
    },
}

_CONNECTION = re.compile(r"^(feed|product|energy_feed|energy_product):[^-]+->port(\d+)$")
_REFUSED = re.compile(r"'([^']+)' has no settable property '([^']+)'\. Available: (.*)$", re.DOTALL)


def _walk(node: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _native(name: str | None) -> str | None:
    return NATIVE_ALIASES.get(name or "", name)


def _type_of(capture: str, path: str, names: dict[str, str], name: str | None) -> str | None:
    if capture == "units.json":
        head = path.split(".", 1)[0]
        return head if head in TYPES else None
    return names.get(name or "")


class _Evidence:
    def __init__(self) -> None:
        self.settable: dict[str, dict[str, set[str]]] = {}
        self.refused: dict[str, set[str]] = {}
        self.modes: dict[str, dict[str, set[str]]] = {}
        self.mode_property: dict[str, dict[str, set[str]]] = {}
        self.enums: dict[str, dict[str, dict[str, set[str]]]] = {}
        self.ports: dict[str, dict[tuple[str, int], set[str]]] = {}

    def add(self, table: dict[str, dict[Any, set[str]]], kind: str, key: Any, source: str) -> None:
        table.setdefault(kind, {}).setdefault(key, set()).add(source)


def _collect() -> _Evidence:
    found = _Evidence()
    for capture in CAPTURES:
        data = json.loads((EVIDENCE / capture).read_text(encoding="utf-8"))
        names: dict[str, str] = {}
        for _path, node in _walk(data, ""):
            label = node.get("name", node.get("object"))
            if isinstance(label, str) and isinstance(node.get("type"), str) and node["type"] != "MaterialStream":
                names.setdefault(label, _native(node["type"]) or "")
        for path, node in _walk(data, ""):
            where = f"{capture}:{path}"
            refusal = _REFUSED.search(str(node.get("__error__", "")))
            refused_type = names.get(refusal[1]) if refusal else None
            if refusal and refused_type:
                found.refused.setdefault(refused_type, set()).add(refusal[2])
                for prop in re.findall(r"'([^']+)'", refusal[3]):
                    found.add(found.settable, refused_type, prop, f"{where} (available)")
            unit = _type_of(capture, path, names, node.get("unitop") or node.get("object") or node.get("name"))
            if not unit:
                continue
            for row in node.get("connections", []):
                match = _CONNECTION.match(row)
                if match:
                    found.add(found.ports, unit, (match[1], int(match[2])), where)
            for row in node.get("applied", []):
                prop, _, value = row.partition(" = ")
                found.add(found.settable, unit, prop, f"{where} (applied)")
                if prop in MODE_PROPERTIES:
                    found.add(found.modes, unit, value, f"{where} (applied)")
                    found.add(found.mode_property, unit, prop, f"{where} (applied)")
                if prop in ENUM_PROPERTIES:
                    found.enums.setdefault(unit, {}).setdefault(prop, {}).setdefault(value, set()).add(where)
            if isinstance(node.get("slots"), list):
                for slot in node["slots"]:
                    found.add(found.settable, unit, slot["property"], f"{where} (dof_slot)")
                if node.get("mode"):
                    found.add(found.modes, unit, node["mode"], f"{where} (dof)")
            for mode in node.get("calculation_modes", []):
                found.add(found.modes, unit, mode, f"{where} (calculation_modes)")
    return found


def _sources(values: set[str]) -> list[str]:
    return sorted(values)


def build_manifest() -> dict[str, Any]:
    evidence = json.loads((EVIDENCE / "units.json").read_text(encoding="utf-8"))
    found = _collect()
    objects: dict[str, Any] = {}
    for name in (*TYPES, "EnergyStream"):
        probe = evidence.get(name, {})
        results = probe.get("results", {})
        dof = probe.get("dof", {})
        slots = dof.get("slots", [])
        refused = found.refused.get(name, set())
        settable = {key: value for key, value in found.settable.get(name, {}).items() if key not in refused}
        captured_modes = found.modes.get(name, {})
        if name == "PFR":
            modes = list(PFR_MODES)
        elif name == "CSTR":
            modes = ["Isothermic", "OutletTemperature"]
        elif name == "DistillationColumn" and dof.get("mode"):
            modes = [dof["mode"]]
        else:
            modes = list(results.get("calculation_modes", []))
        ports: dict[str, list[dict[str, Any]]] = {}
        captured_ports = found.ports.get(name, {})
        declared_ports = {
            (role, port): {source}
            for (kind, role, port), source in DECLARED_PORTS.items()
            if kind == name and (role, port) not in captured_ports
        }
        for (role, port), sources in sorted({**captured_ports, **declared_ports}.items()):
            ports.setdefault(role, []).append(
                {"port": port, "captured": (role, port) in captured_ports, "evidence": _sources(sources)}
            )
        objects[name] = {
            "native_type": probe.get("add", {}).get("type", name),
            "modes": modes,
            "mode_evidence": {
                mode: _sources(captured_modes[mode]) if mode in captured_modes else [DECLARED_MODES[name][mode]]
                for mode in modes
            },
            "mode_property": {prop: _sources(sources) for prop, sources in found.mode_property.get(name, {}).items()},
            "enum_values": {
                prop: {value: _sources(sources) for value, sources in sorted(values.items())}
                for prop, values in found.enums.get(name, {}).items()
            },
            "observed_mode": results.get("calculation_mode"),
            "settable_properties": sorted(settable),
            "settable_property_evidence": {key: _sources(value) for key, value in sorted(settable.items())},
            "refused_properties": sorted(refused),
            "native_xml_inputs": NATIVE_XML_INPUTS.get(name, {}),
            "ports": ports,
            "dof": {
                "mode": dof.get("mode"),
                "supported": bool(dof.get("supported", name == "EnergyStream")),
                "slots": [
                    {"property": row.get("property"), "units": row.get("units"), "required": bool(row.get("required"))}
                    for row in slots
                ],
            },
            "result_properties": [
                {
                    "name": key,
                    "unit": value.get("units", ""),
                    "classification": "input" if key in {s["property"] for s in slots} else "result",
                }
                for key, value in sorted(results.get("properties", {}).items())
            ],
        }
    energy_result = json.loads((EVIDENCE / "ports.json").read_text(encoding="utf-8")).get("h_Q", {})
    objects["EnergyStream"]["result_properties"] = [
        {"name": key, "unit": value.get("units", ""), "classification": "result"}
        for key, value in sorted(energy_result.get("properties", {}).items())
    ]
    return {
        "runtime": "DWSIM 10.2.9",
        "probe_sha256": "d20f9742",
        "source": "evidence/158/units.json",
        "captures": [f"evidence/158/{name}" for name in CAPTURES],
        "objects": objects,
        "verified": {
            "energy_stream": "ports.json: energy feed port 1 (h_q1) and EnergyFlow setter (q_set)",
            "pfr_heat_exchange_reaction": "pfr_ok.json: native reaction-set reload and solve",
            "distillation_column": (
                "column2.json: the MCP connects feed port 0, distillate product 0 and bottoms product 1 but "
                "rejects the condenser/reboiler energy connections, exposes no feed-stage or stage-pressure "
                "property, and the solve fails on missing streams; draft_compiler materializes the column "
                "through native XML (stage count and pressures, both specification values, feed stage, "
                "energy connectors 10) plus reload, verified by read-back"
            ),
        },
    }


def render() -> str:
    return json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUT.write_text(render(), encoding="utf-8")


if __name__ == "__main__":
    main()
