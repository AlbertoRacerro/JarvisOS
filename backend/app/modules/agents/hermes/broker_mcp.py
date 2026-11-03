"""One read-only MCP stdio bridge to the Jarvis worker relay (stdlib only)."""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_TOOL = {
    "name": "jarvis_context_preview",
    "description": "Preview exact Jarvis context refs using a live Jarvis capability grant.",
    "inputSchema": {
        "type": "object", "properties": {
            "grant_id": {"type": "string"},
            "request": {"type": "object"},
        }, "required": ["grant_id", "request"], "additionalProperties": False,
    },
}
_RETRIEVAL_TOOL = {
    "name": "jarvis_retrieval_query",
    "description": "Find bounded, current Second Brain evidence within the live grant scope.",
    "inputSchema": {
        "type": "object", "properties": {
            "grant_id": {"type": "string"},
            "query": {"type": "string", "maxLength": 2000},
            "source_scope": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            "token_budget": {"type": "integer", "minimum": 1, "maximum": 1024},
        }, "required": ["grant_id", "query", "source_scope"], "additionalProperties": False,
    },
}
_DECISION_TOOL = {
    "name": "jarvis_decide",
    "description": "Request bounded, non-authoritative Jarvis decision advice.",
    "inputSchema": {
        "type": "object", "properties": {
            "grant_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "kind": {"type": "string", "enum": ["route_class", "retry_or_stop", "escalate", "model_select"]},
            "request": {"type": "object"},
        }, "required": ["grant_id", "kind", "request"], "additionalProperties": False,
    },
}


_QUANTITY = {"type": "object", "properties": {"value": {"type": "number"}, "unit": {"type": "string"}},
             "required": ["value", "unit"], "additionalProperties": False}
# Spec 180: the agent proposes only typed Monod/Haldane reactions; KineticReaction stays the apply-time authority.
def _rate_law(form: str, **extra: dict[str, Any]) -> dict[str, Any]:
    # Only Haldane carries the substrate-inhibition constant k_i; Monod refuses it at apply time.
    return {"type": "object", "properties": {
        "form": {"const": form}, "substrate": {"type": "string"}, "v_max": _QUANTITY, "k_s": _QUANTITY, **extra,
        "inhibitions": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
            "kind": {"enum": ["noncompetitive", "competitive"]}, "inhibitor": {"type": "string"}, "k_i": _QUANTITY},
            "required": ["kind", "inhibitor", "k_i"], "additionalProperties": False}},
        "temperature": {"type": "object", "properties": {"activation_energy": _QUANTITY,
                        "reference_temperature": _QUANTITY},
                        "required": ["activation_energy", "reference_temperature"], "additionalProperties": False}},
        "required": ["form", "substrate", "v_max", "k_s"], "additionalProperties": False}


_REACTION = {"type": "object", "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 80},
    "stoichiometry": {"type": "object", "additionalProperties": {"type": "number"}, "minProperties": 2},
    "base_reactant": {"type": "string"},
    "phase": {"const": "Liquid"}, "basis": {"const": "MolarConc"},
    "rate_law": {"oneOf": [_rate_law("monod"), _rate_law("haldane", k_i=_QUANTITY)]},
    "provenance": {"type": "object", "properties": {
        "kind": {"enum": ["literature", "measurement", "operator_estimate", "synthetic"]},
        "citation": {"type": "string", "maxLength": 300}, "note": {"type": "string", "maxLength": 500}},
        "required": ["kind"], "additionalProperties": False}},
    "required": ["name", "stoichiometry", "base_reactant", "phase", "rate_law", "provenance"],
    "additionalProperties": False}
_PROCESS_READ_TOOL = {
    "name": "jarvis_process_read",
    "description": "Read the current Jarvis process flowsheet draft: tags, typed parameters with units, "
                   "findings and whether DWSIM results are current or stale.",
    "inputSchema": {
        "type": "object", "properties": {
            "grant_id": {"type": "string", "minLength": 1, "maxLength": 128},
        }, "required": ["grant_id"], "additionalProperties": False,
    },
}
_PROCESS_ACT_TOOL = {
    "name": "jarvis_process_act",
    "description": "Request supported typed Process changes. Use exact tags and base revision from jarvis_process_read; reactor reactions are confirm-tier proposals.",
    "inputSchema": {"type": "object", "properties": {
        "grant_id": {"type": "string", "maxLength": 128},
        "base_revision": {"type": "string", "maxLength": 64},
        "actions": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": [
            {"type": "object", "properties": {"op": {"const": "set_value"}, "target": {"type": "string"},
             "property": {"type": "string"}, "value": {"oneOf": [_QUANTITY, {"type": "string"},
             {"type": "object", "additionalProperties": {"type": "number"}}]}},
             "required": ["op", "target", "property", "value"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "set_unit_model"},
             "unit": {"type": "string"}, "card": {"type": "string", "maxLength": 200}},
             "required": ["op", "unit", "card"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "set_unit_hydraulics"},
             "unit": {"type": "string"}, "reduced_model": {"type": ["string", "null"], "maxLength": 200},
             "out_of_range": {"enum": ["refuse", "warn"]}},
             "required": ["op", "unit"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "set_cfd_study_template"},
             "unit": {"type": "string"}, "regime": {"enum": ["laminar", "turbulent_rans"]},
             "fluid": {"type": "object"}, "inlet": {"type": "object"}, "outlet_pressure_pa": {"type": "number"},
             "turbulence_model": {"enum": ["kOmegaSST", "kEpsilon", None]},
             "wall_treatment": {"enum": ["resolved", "low_re_resolved", "wall_function"]},
             "mesh_level": {"type": "integer", "minimum": 0, "maximum": 4}, "solver": {"type": "object"}},
             "required": ["op", "unit", "regime", "inlet", "wall_treatment"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "set_reaction"},
             "unit": {"type": "string"}, "reaction_id": {"type": "string"},
             "reaction": _REACTION},
             "required": ["op", "unit", "reaction_id", "reaction"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "add_unit"}, "type": {"type": "string"},
             "tag": {"type": "string"}, "near": {"type": "string"}}, "required": ["op", "type"],
             "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "insert_unit_after"}, "type": {"type": "string"},
             "after": {"type": "string"}, "tag": {"type": "string"}}, "required": ["op", "type", "after"],
             "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "connect"}, "from": {"type": "string"},
             "from_port": {"type": "string"}, "to": {"type": "string"}, "to_port": {"type": "string"}},
             "required": ["op", "from", "to"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "disconnect"}, "stream": {"type": "string"}},
             "required": ["op", "stream"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "mirror"}, "target": {"type": "string"},
             "axis": {"enum": ["horizontal", "vertical"]}}, "required": ["op", "target", "axis"],
             "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "move"}, "target": {"type": "string"},
             "dx": {"type": "number"}, "dy": {"type": "number"}}, "required": ["op", "target", "dx", "dy"],
             "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "rename"}, "target": {"type": "string"},
             "new_tag": {"type": "string"}}, "required": ["op", "target", "new_tag"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "delete"}, "target": {"type": "string"}},
             "required": ["op", "target"], "additionalProperties": False}
        ]}},
        "rationale": {"type": "string", "maxLength": 600}},
        "required": ["grant_id", "base_revision", "actions"], "additionalProperties": False},
}
_BLUECAD_READ_TOOL = {
    "name": "jarvis_bluecad_read", "description": "Read the current BLUECAD candidate and selected parts.",
    "inputSchema": {"type": "object", "properties": {"grant_id": {"type": "string", "maxLength": 128},
                     },
                     "required": ["grant_id"], "additionalProperties": False},
}
_BLUECAD_ACT_TOOL = {
    "name": "jarvis_bluecad_act",
    "description": "Request a typed BLUECAD candidate change such as duplicate_part, set_part_param, move_part or delete_part.",
    "inputSchema": {"type": "object", "properties": {
        "grant_id": {"type": "string", "maxLength": 128},
        "base_revision": {"type": "string", "maxLength": 64},
        "actions": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": [
            {"type": "object", "properties": {"op": {"const": "duplicate_part"}, "part": {"type": "string"},
             "placement": {"enum": ["beside", "above", "along"]}, "gap_mm": {"type": "number"}},
             "required": ["op", "part"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "set_part_param"}, "part": {"type": "string"},
             "param": {"type": "string"}, "value": {"type": "number"},
             "unit": {"enum": ["mm", "m", "cm", "deg"]}},
             "required": ["op", "part", "param", "value", "unit"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "move_part"}, "part": {"type": "string"},
             "dx": {"type": "number"}, "dy": {"type": "number"}, "dz": {"type": "number"},
             "unit": {"enum": ["mm", "m", "cm"]}}, "required": ["op", "part"], "additionalProperties": False},
            {"type": "object", "properties": {"op": {"const": "delete_part"}, "part": {"type": "string"}},
             "required": ["op", "part"], "additionalProperties": False}
        ]}},
        "rationale": {"type": "string", "maxLength": 600}},
        "required": ["grant_id", "base_revision", "actions"], "additionalProperties": False},
}
_STUDIO_READ_TOOL = {
    "name": "jarvis_studio_read", "description": "Read the selected Studio draft or new-model context.",
    "inputSchema": {"type": "object", "properties": {"grant_id": {"type": "string", "maxLength": 128}},
                    "required": ["grant_id"], "additionalProperties": False},
}
# An untyped object becomes an empty-object grammar under constrained local decoding, so the
# contract shape is spelled out. The Studio owner still validates it per category.
_STUDIO_VARIABLE = {"type": "object", "properties": {
    "name": {"type": "string", "maxLength": 64}, "label": {"type": "string", "maxLength": 120},
    "unit": {"type": "string", "maxLength": 64}, "description": {"type": "string", "maxLength": 500},
    "min": {"type": "number"}, "max": {"type": "number"}},
    "required": ["name", "label", "unit", "description"], "additionalProperties": False}
_STUDIO_PORT = {"type": "object", "properties": {
    "name": {"type": "string", "maxLength": 64}, "label": {"type": "string", "maxLength": 120}},
    "required": ["name", "label"], "additionalProperties": False}
_STUDIO_CONTRACT = {
    "type": "object",
    "description": ("calculation: inputs and outputs; geometry: inputs (lengths in mm); "
                    "process_unit: inlets, outlets, parameters, culture_generation."),
    "properties": {
        "inputs": {"type": "array", "maxItems": 64, "items": _STUDIO_VARIABLE},
        "outputs": {"type": "array", "maxItems": 64, "items": _STUDIO_VARIABLE},
        "inlets": {"type": "array", "maxItems": 4, "items": _STUDIO_PORT},
        "outlets": {"type": "array", "maxItems": 4, "items": _STUDIO_PORT},
        "parameters": {"type": "array", "maxItems": 32, "items": _STUDIO_VARIABLE},
        "culture_generation": {"type": "array", "maxItems": 6, "items": {"enum": [
            "biomass", "nitrogen", "phosphorus", "oxygen", "dic", "salinity"]}}},
    "additionalProperties": False,
}
_STUDIO_ACT_TOOL = {
    "name": "jarvis_studio_act",
    "description": "Propose one inert calculation, geometry, or steady process-unit Studio draft. Operator Apply is required.",
    "inputSchema": {"type": "object", "properties": {
        "grant_id": {"type": "string", "maxLength": 128},
        "base_revision": {"type": "string", "maxLength": 128},
        "actions": {"type": "array", "minItems": 1, "maxItems": 1, "items": {
            "type": "object", "properties": {
                "op": {"const": "studio_draft"}, "mode": {"enum": ["create", "update"]},
                "category": {"enum": ["calculation", "geometry", "process_unit"]},
                "name": {"type": "string", "maxLength": 160},
                "engineering_question": {"type": "string", "maxLength": 2000},
                "contract": _STUDIO_CONTRACT, "source": {"type": "string", "maxLength": 65536},
                "assumptions": {"type": "string", "maxLength": 20000}},
            "required": ["op", "mode", "category", "name", "engineering_question", "contract", "source"],
            "additionalProperties": False}},
        "rationale": {"type": "string", "maxLength": 600}},
        "required": ["grant_id", "base_revision", "actions"], "additionalProperties": False},
}
_TOOLS = [_TOOL, _RETRIEVAL_TOOL, _DECISION_TOOL, _PROCESS_READ_TOOL, _PROCESS_ACT_TOOL,
          _BLUECAD_READ_TOOL, _BLUECAD_ACT_TOOL, _STUDIO_READ_TOOL, _STUDIO_ACT_TOOL]
_NAMED = {tool["name"] for tool in _TOOLS if tool is not _TOOL}


class _DenyRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _reply(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "jarvis", "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") not in {tool["name"] for tool in _TOOLS}:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown tool"}}
        body = params.get("arguments") or {}
        if params.get("name") in _NAMED and isinstance(body, dict):
            body = {**body, "tool_name": params["name"]}
        encoded = json.dumps(body).encode()
        broker_url = os.environ["JARVIS_HERMES_BROKER_URL"]
        parsed = urlsplit(broker_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/v1/jarvis/tool":
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": "invalid Jarvis broker endpoint"}}
        request = Request(broker_url, data=encoded,
                          headers={"Authorization": f"Bearer {os.environ['JARVIS_HERMES_BROKER_TOKEN']}",
                                   "Content-Type": "application/json"})
        try:
            opener = build_opener(ProxyHandler({}), _DenyRedirects())
            with opener.open(request, timeout=120) as response:
                result = json.load(response)
            content = json.dumps(result, separators=(",", ":"))
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": content}],
                "isError": result.get("status") != "succeeded"}}
        except (HTTPError, URLError, ValueError):
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "Jarvis capability unavailable"}], "isError": True}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown method"}}


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = _reply(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except (ValueError, KeyError):
            pass


if __name__ == "__main__":
    main()
