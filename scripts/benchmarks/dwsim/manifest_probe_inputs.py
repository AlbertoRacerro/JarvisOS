"""Capture pinned-runtime acceptance of every property the 158 compiler writes, per unit and mode.

Replays ``draft_compiler.plan`` for a synthetic water flowsheet per registry unit/mode against the
pinned DWSIM MCP and records each call's raw response, the unit's read-back and a solve attempt in
``evidence/158/inputs.json``. The manifest generator reads the ``applied``/``connections`` rows.

    JARVISOS_DWSIM_MCP_PATH=... JARVISOS_DWSIM_MCP_SHA256=... \\
        backend/.venv/bin/python scripts/benchmarks/dwsim/manifest_probe_inputs.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.modules.process_stack import draft_compiler
from app.modules.process_stack.draft_models import UNIT_REGISTRY
from app.modules.process_stack.dwsim_mcp import DwsimMcpClient

OUT = ROOT / "backend/app/modules/process_stack/evidence/158/inputs.json"
TYPES = ("Heater", "Cooler", "Pump", "Valve", "Mixer", "Flash", "Recycle", "PFR")
# Plausible SI magnitudes for a 1 kg/s, 300 K, 1 bar water feed; acceptance is what is probed.
VALUES = {
    "temperature": 330.0,
    "pressure": 3e5,
    "pressure_difference": 5e4,
    "mass_flow": 1.0,
    "percent": 75.0,
    "power": 2e4,
    "flow_ratio": 0.5,
    "dimensionless": 0.5,
    "length": 4.0,
    "volume": 2.0,
    "area": 12.0,
    "heat_transfer_coefficient": 400.0,
    "molar_flow": 10.0,
    "specific_heat": 4180.0,
    "volume_flow": 0.001,
}
OVERRIDES = {
    ("Heater", "temperature_change"): 10.0,
    ("Cooler", "temperature_change"): -10.0,
    ("Cooler", "outlet_temperature"): 290.0,
    ("Valve", "outlet_pressure"): 5e4,
}


def _document(unit_type: str, mode: str | None, tag: str) -> dict[str, Any]:
    spec = UNIT_REGISTRY[unit_type]
    params = {}
    for item in spec.params_for(mode):
        value = (
            OVERRIDES.get((unit_type, mode))
            if item.kind in {"temperature", "pressure"} or mode == "temperature_change"
            else None
        )
        params[item.key] = {
            "si": value
            if value is not None
            else item.default
            if item.default is not None
            else VALUES[item.kind]
        }
    objects: dict[str, Any] = {
        "u": {
            "id": "u",
            "kind": "unit",
            "type": unit_type,
            "tag": tag,
            "x": 0,
            "y": 0,
            "mode": mode,
            "params": params,
            "options": {},
            "reactions": [],
        }
    }

    def stream(suffix: str, kind: str, end: str, port: int) -> None:
        feed = {
            "temperature": {"si": 300.0},
            "pressure": {"si": 1e5},
            "mass_flow": {"si": 1.0},
            "composition": {"Water": 1.0},
        }
        objects[suffix] = {
            "id": suffix,
            "kind": "stream",
            "type": kind,
            "tag": f"{tag}_{suffix}",
            "x": 0,
            "y": 0,
            "source": {"unit": "u", "port": port} if end == "source" else None,
            "target": {"unit": "u", "port": port} if end == "target" else None,
            "spec": {"duty": {"si": 20.0}}
            if kind == "EnergyStream"
            else feed
            if end == "target"
            else {},
        }

    for port in range(len(spec.inlets)):
        stream(f"F{port}", "MaterialStream", "target", port)
    for port in range(len(spec.outlets)):
        stream(f"P{port}", "MaterialStream", "source", port)
    for port in range(len(spec.energy_inlets)):
        stream(f"QF{port}", "EnergyStream", "target", port)
    for port in range(len(spec.energy_outlets)):
        stream(f"QP{port}", "EnergyStream", "source", port)
    return {
        "compounds": ["Water"],
        "property_package": "NRTL",
        "objects": objects,
        "reactions": {},
    }


def _raw(
    client: DwsimMcpClient, name: str, args: dict[str, Any], timeout: float = 60
) -> Any:
    response = client._request("tools/call", {"name": name, "arguments": args}, timeout)
    if response.get("isError"):
        text = " ".join(
            block.get("text", "")
            for block in response.get("content", [])
            if isinstance(block, dict)
        )
        return {"__error__": text, "structured": response.get("structuredContent")}
    if response.get("structuredContent") is not None:
        return response["structuredContent"]
    text = response["content"][0]["text"]
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


def main() -> None:
    client = DwsimMcpClient(
        Path(os.environ["JARVISOS_DWSIM_MCP_PATH"]),
        os.environ["JARVISOS_DWSIM_MCP_SHA256"],
    )
    cases: dict[str, Any] = {}
    with client:
        for unit_type in TYPES:
            for mode in tuple(UNIT_REGISTRY[unit_type].modes) or (None,):
                tag = f"{unit_type}_{mode or 'default'}"
                flow = _raw(client, "dwsim_flowsheet_create", {"name": tag})[
                    "flowsheet_id"
                ]
                calls = []
                for name, args in draft_compiler.plan(_document(unit_type, mode, tag)):
                    calls.append(
                        {
                            "tool": name,
                            "args": args,
                            "result": _raw(
                                client, name, {"flowsheet_id": flow, **args}
                            ),
                        }
                    )
                cases[tag] = {
                    "type": unit_type,
                    "mode": mode,
                    "calls": calls,
                    "solve": _raw(
                        client,
                        "dwsim_solve_run",
                        {"flowsheet_id": flow, "timeout_s": 60},
                        90,
                    ),
                    "read_back": _raw(
                        client,
                        "dwsim_unitop_get_results",
                        {"flowsheet_id": flow, "name": tag},
                    ),
                }
                print(
                    tag,
                    [
                        call["result"].get("__error__", "ok")[:160]
                        for call in calls
                        if call["tool"] in {"dwsim_unitop_set", "dwsim_unitop_connect"}
                    ],
                    str(cases[tag]["solve"])[:160],
                    flush=True,
                )
    OUT.write_text(
        json.dumps(
            {"runtime_sha256": os.environ["JARVISOS_DWSIM_MCP_SHA256"], "cases": cases},
            indent=1,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
