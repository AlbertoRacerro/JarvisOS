"""Jarvis-owned process draft: revisions, typed edits, validation and proposals (spec 155).

The draft is the authoritative editable flowsheet. Edits never contact DWSIM; they apply
typed operations to a canonical JSON document and write an immutable revision under
compare-and-swap. DWSIM is reached only through ``draft_compiler`` on Validate/Run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.core.paths import build_paths
from app.modules.engineering.refs import Quantity
from app.modules.process_stack import culture as culture_engine
from app.modules.process_stack import kinetics, pbr_validation, studio_unit
from app.modules.process_stack._common import EvaluationRefusal, magnitude
from app.modules.process_stack.draft_models import (
    COMPILER_VERSION,
    COMPOUNDS,
    FEED_ALTERNATIVES,
    MAX_OBJECTS,
    PBR_T2_OPTIONAL_KEYS,
    PBR_T2_SPECS,
    PROPERTY_PACKAGES,
    QUANTITY_UNITS,
    STREAM_SPECS,
    STUDIO_UNIT,
    UNIT_REGISTRY,
    UNSUPPORTED_TYPES,
    AddStream,
    AddUnit,
    Connect,
    Delete,
    DeleteController,
    DeleteScenario,
    DeleteSchedule,
    Disconnect,
    DraftQuantity,
    Move,
    ParamSpec,
    ProposalRequest,
    Rename,
    SetController,
    SetOrientation,
    SetReactions,
    SetReactorReaction,
    SetRoute,
    SetScenario,
    SetSchedule,
    SetSolver,
    SetStreamCulture,
    SetStreamSpec,
    SetThermo,
    SetUnitHydraulics,
    SetUnitModel,
    SetUnitParams,
    UnitSpec,
    UpdateStudioUnit,
    pbr_temperature_invalid,
    spec_for,
)
from app.modules.process_stack.editor import _lock
from app.modules.workspaces.service import get_workspace

_COMPOSITION_TOL = 1e-6


@lru_cache(maxsize=1)
def _capability_manifest() -> dict[str, Any]:
    path = Path(__file__).with_name("dwsim_10_2_9_manifest.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # a missing checked-in contract is a packaging defect
        raise RuntimeError("DWSIM 10.2.9 capability manifest is unavailable") from exc
    if value.get("runtime") != "DWSIM 10.2.9" or not isinstance(value.get("objects"), dict):
        raise RuntimeError("DWSIM capability manifest has an invalid identity")
    return value


class DraftError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 422, **detail: Any):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail


def _now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=1), encoding="utf-8")
    with tmp.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _read_json(path: Path, code: str, message: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DraftError(code, message, 404) from exc


def drafts_root(workspace_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", workspace_id) or workspace_id in {".", ".."} or (
        get_workspace(workspace_id) is None
    ):
        raise DraftError("workspace_not_found", "Workspace was not found", 404)
    return _drafts_path(workspace_id)


def _drafts_path(workspace_id: str) -> Path:
    return build_paths().workspaces_dir / workspace_id / "process" / "drafts"


def draft_dir(workspace_id: str, draft_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9-]{36}", draft_id):
        raise DraftError("draft_not_found", "Process draft was not found", 404)
    path = drafts_root(workspace_id) / draft_id
    if not (path / "head.json").exists():
        raise DraftError("draft_not_found", "Process draft was not found", 404)
    return path


# ---------------------------------------------------------------- quantities


# Spec 170: explicit exact factors; time is stored in s and specific_rate in 1/s.
_TIME_TO_S = {"s": 1.0, "h": 3600.0, "d": 86400.0}
_PER_TIME_DIVISOR = {"1/s": 1.0, "1/h": 3600.0, "1/d": 86400.0}


def _si(quantity: DraftQuantity, kind: str, field: str) -> dict[str, Any]:
    si_unit, allowed = QUANTITY_UNITS[kind]
    if quantity.unit not in allowed:
        raise DraftError("unit_unsupported", f"{field}: unit {quantity.unit!r} is not offered; use one of {list(allowed)}",
                         field=field)
    if kind == "mass_concentration":
        value = quantity.value * {"kg/m3": 1.0, "g/L": 1.0, "mg/L": 0.001}[quantity.unit]
    elif kind == "molar_concentration":
        value = quantity.value * (1000.0 if quantity.unit == "kmol/m3" else 1.0)
    elif kind == "reaction_rate":
        value = quantity.value * {"kmol/[m3.h]": 1000.0 / 3600.0,
                                  "mol/[m3.s]": 1.0, "mol/[L.h]": 1.0 / 3.6}[quantity.unit]
    elif kind == "temperature_difference":
        value = quantity.value  # a difference: K and degC share the scale and there is no offset
    elif kind == "time":
        value = quantity.value * _TIME_TO_S[quantity.unit]
    elif kind == "specific_rate":
        value = quantity.value / _PER_TIME_DIVISOR[quantity.unit]
    elif kind == "inverse_length":
        value = quantity.value * {"1/m": 1.0, "1/cm": 100.0}[quantity.unit]
    elif kind in {"salinity", "ph", "percent"} or quantity.unit == si_unit:
        value = quantity.value
    else:
        try:
            value = magnitude(Quantity(value=quantity.value, unit=quantity.unit), si_unit)
        except (EvaluationRefusal, ValueError) as exc:
            raise DraftError("quantity_invalid", f"{field}: {quantity.unit} is not a {kind}", field=field) from exc
    if not math.isfinite(value):
        raise DraftError("quantity_invalid", f"{field}: value is not finite", field=field)
    return {"si": value, "value": quantity.value, "unit": quantity.unit}


def convert_si(value_si: float, kind: str, unit: str) -> float:
    """Server-side presentation conversion from SI storage into a display unit."""
    si_unit, _allowed = QUANTITY_UNITS[kind]
    if kind == "mass_concentration":
        return value_si / {"kg/m3": 1.0, "g/L": 1.0, "mg/L": 0.001}[unit]
    if kind == "molar_concentration":
        return value_si / (1000.0 if unit == "kmol/m3" else 1.0)
    if kind == "reaction_rate":
        return value_si / {"kmol/[m3.h]": 1000.0 / 3600.0,
                           "mol/[m3.s]": 1.0, "mol/[L.h]": 1.0 / 3.6}[unit]
    if kind == "temperature_difference":
        return value_si
    if kind == "time":
        return value_si / _TIME_TO_S[unit]
    if kind == "specific_rate":
        return value_si * _PER_TIME_DIVISOR[unit]
    if kind == "inverse_length":
        return value_si / {"1/m": 1.0, "1/cm": 100.0}[unit]
    if kind in {"salinity", "ph", "percent"} or unit == si_unit:
        return value_si
    return magnitude(Quantity(value=value_si, unit=si_unit), unit)


# ---------------------------------------------------------------- document ops


def empty_document(name: str) -> dict[str, Any]:
    return {"schema_version": 1, "name": name, "compounds": [], "property_package": None,
            "objects": {}, "reactions": {}}


def _by_tag(document: dict[str, Any], tag: str) -> dict[str, Any] | None:
    return next((item for item in document["objects"].values() if item["tag"] == tag), None)


def _object(document: dict[str, Any], object_id: str, kind: str | None = None) -> dict[str, Any]:
    item = document["objects"].get(object_id)
    if item is None or (kind and item["kind"] != kind):
        raise DraftError("object_not_found", f"{kind or 'object'} {object_id!r} is not in the draft", field=object_id)
    return item


def _new_id(document: dict[str, Any], prefix: str, requested: str | None) -> str:
    if requested:
        if requested in document["objects"]:
            raise DraftError("id_conflict", f"id {requested!r} already exists")
        return requested
    index = len(document["objects"]) + 1
    while f"{prefix}{index}" in document["objects"]:
        index += 1
    return f"{prefix}{index}"


def _unique_tag(document: dict[str, Any], tag: str, own_id: str | None = None) -> None:
    other = _by_tag(document, tag)
    if other is not None and other["id"] != own_id:
        raise DraftError("tag_conflict", f"tag {tag!r} is already used", field=tag)


def _unit_spec(unit: dict[str, Any]) -> UnitSpec:
    return spec_for(unit)


def _resolve_studio(version_id: str | None, resolve: studio_unit.Resolver | None) -> dict[str, Any]:
    if not version_id or resolve is None:
        raise DraftError("studio_version_required", "A StudioUnit is placed from a published Studio version", field="version_id")
    try:
        return resolve(version_id)
    except studio_unit.StudioUnitError as exc:
        raise DraftError(exc.code.lower(), exc.message, field="version_id") from exc


def _update_studio_unit(document: dict[str, Any], op: UpdateStudioUnit, resolve: studio_unit.Resolver | None) -> None:
    unit = _object(document, op.unit, "unit")
    if unit["type"] != STUDIO_UNIT:
        raise DraftError("studio_update_unsupported", "Only a StudioUnit can be updated to a newer version", field="unit")
    resolved = _resolve_studio(op.version_id, resolve)
    studio = unit["studio"]
    if resolved["model_spec_id"] != studio["model_spec_id"]:
        raise DraftError("studio_update_other_model", "The version belongs to a different Studio model", field="version_id")
    if resolved["version_number"] <= studio["version_number"]:
        raise DraftError("studio_update_not_newer", f"{studio['name']} v{resolved['version_number']} is not newer "
                         f"than the pinned v{studio['version_number']}", field="version_id")
    old, new = spec_for(unit), _studio_spec_of(resolved)
    for stream in document["objects"].values():
        for end, ports_old, ports_new in (("source", old.outlets, new.outlets), ("target", old.inlets, new.inlets)):
            endpoint = stream.get(end) if stream["kind"] == "stream" else None
            if endpoint and endpoint["unit"] == unit["id"] and stream["type"] != "EnergyStream" and (
                    endpoint["port"] >= len(ports_new) or ports_new[endpoint["port"]] != ports_old[endpoint["port"]]):
                raise DraftError("studio_update_ports_changed", f"v{resolved['version_number']} changes the port "
                                 f"{ports_old[endpoint['port']]} that {stream['tag']} uses; disconnect it first", field="version_id")
    unit.update(studio_unit.pin_fields(resolved))
    kept = {item["name"]: item for item in resolved["contract"]["parameters"]}
    units = {item["name"]: item["unit"] for item in studio["contract"]["parameters"]}
    unit["params"] = {name: value for name, value in unit["params"].items()
                      if name in kept and units.get(name) == kept[name]["unit"]
                      and not _outside(value["si"], kept[name])}


def _studio_spec_of(resolved: dict[str, Any]) -> UnitSpec:
    return spec_for({"type": STUDIO_UNIT, "tag": resolved["name"], **studio_unit.pin_fields(resolved)})


def _outside(value: float, variable: dict[str, Any]) -> bool:
    return (variable.get("min") is not None and value < variable["min"]
            or variable.get("max") is not None and value > variable["max"])


def _set_studio_params(unit: dict[str, Any], op: SetUnitParams) -> None:
    from app.modules.process_kernel.units import unit_registry

    if op.mode not in (None, "steady") or op.options or op.reactions:
        raise DraftError("mode_unsupported", "A StudioUnit has no modes, options or reactions", field="mode")
    if op.unset:
        raise DraftError("unset_unsupported", "A StudioUnit does not support unsetting parameters", field="unset")
    declared = {item["name"]: item for item in unit["studio"]["contract"]["parameters"]}
    for key, quantity in op.values.items():
        variable = declared.get(key)
        if variable is None:
            raise DraftError("param_unsupported", f"{unit['tag']} has no parameter {key!r}", field=key)
        try:
            value = float(unit_registry().Quantity(quantity.value, quantity.unit).to(variable["unit"]).magnitude)
        except Exception as exc:  # noqa: BLE001 - any Pint failure is one operator-facing unit refusal
            raise DraftError("quantity_invalid", f"{variable['label']} must be in {variable['unit']}", field=key) from exc
        if not math.isfinite(value) or _outside(value, variable):
            raise DraftError("quantity_out_of_range", f"{variable['label']} is outside its declared bounds", field=key)
        # For a StudioUnit "si" is the magnitude in the declared unit, which is also what the inspector shows.
        unit["params"][key] = {"si": value, "value": value, "unit": variable["unit"]}


def _occupant(document: dict[str, Any], unit_id: str, end: str, port: int, *, energy: bool = False) -> dict[str, Any] | None:
    return next(
        (item for item in document["objects"].values() if item["kind"] == "stream"
         and (item["type"] == "EnergyStream") is energy
         and (item.get(end) or {}).get("unit") == unit_id and item[end]["port"] == port),
        None,
    )


def _default_param(param: ParamSpec) -> dict[str, Any]:
    assert param.default is not None
    unit = QUANTITY_UNITS[param.kind][1][0]
    return {"si": param.default, "value": convert_si(param.default, param.kind, unit), "unit": unit}


def _reaction_item(reaction: Any, declared: set[str]) -> dict[str, Any]:
    item = reaction.model_dump(mode="json", exclude_none=True)
    # Keep legacy records byte-for-byte compatible: optional 180 fields are absent.
    if not item.get("rate_law"):
        item.pop("rate_law", None)
        item.pop("provenance", None)
        item.pop("validity", None)
    compounds = set(item["stoichiometry"]) | set(item.get("orders", {}))
    unknown = sorted(compounds - declared)
    if unknown:
        raise DraftError("compound_undeclared", f"Reaction compounds {unknown} are not declared in Thermo",
                         field="stoichiometry")
    base = item["base_reactant"]
    if base not in item["stoichiometry"] or item["stoichiometry"][base] >= 0:
        raise DraftError("reaction_base_invalid", "Base reactant must have a negative stoichiometric coefficient",
                         field="base_reactant")
    law = reaction.rate_law
    if law is None:
        if item["A_forward"]["unit"] != "kmol/[m3.h]" or item["E_forward"]["unit"] not in {"J/mol", "kJ/mol"}:
            raise DraftError("reaction_unit_unsupported", "Use A in kmol/[m3.h] and activation energy in J/mol or kJ/mol",
                             field="A_forward")
        return item
    parameter_kinds = [("v_max", "reaction_rate"), ("k_s", "molar_concentration")]
    if law.form == "haldane":
        parameter_kinds.append(("k_i", "molar_concentration"))
    for key, kind in parameter_kinds:
        if getattr(law, key) is not None:
            item["rate_law"][key] = _si(getattr(law, key), kind, key)
    for index, term in enumerate(law.inhibitions):
        item["rate_law"]["inhibitions"][index]["k_i"] = _si(term.k_i, "molar_concentration", "k_i")
    if law.temperature:
        item["rate_law"]["temperature"]["activation_energy"] = _si(law.temperature.activation_energy,
                                                                       "molar_energy", "activation_energy")
        item["rate_law"]["temperature"]["reference_temperature"] = _si(law.temperature.reference_temperature,
                                                                           "temperature", "reference_temperature")
    if reaction.validity:
        for key, kind in (("temperature_min", "temperature"), ("temperature_max", "temperature"),
                          ("substrate_max", "molar_concentration")):
            value = getattr(reaction.validity, key)
            if value is not None:
                item["validity"][key] = _si(value, kind, key)
    # Missing or out-of-domain parameters, substrate and provenance problems are pre-Run findings
    # (validate_document), so an operator can save a partly entered reaction and see what is left.
    return item


def apply_op(document: dict[str, Any], op: Any, resolve: studio_unit.Resolver | None = None) -> None:
    objects = document["objects"]
    if isinstance(op, AddUnit | AddStream) and len(objects) >= MAX_OBJECTS:
        raise DraftError("draft_too_large", f"A draft holds at most {MAX_OBJECTS} objects")
    if isinstance(op, UpdateStudioUnit):
        _update_studio_unit(document, op, resolve)
    elif isinstance(op, AddUnit) and op.type == STUDIO_UNIT:
        resolved = _resolve_studio(op.version_id, resolve)
        _unique_tag(document, op.tag)
        object_id = _new_id(document, "u", op.id)
        objects[object_id] = {"id": object_id, "kind": "unit", "type": STUDIO_UNIT, "tag": op.tag, "x": op.x, "y": op.y,
                              "mode": "steady", "params": {}, "options": {}, "reactions": [],
                              **studio_unit.pin_fields(resolved)}
    elif isinstance(op, AddUnit):
        if op.type not in UNIT_REGISTRY:
            reason = UNSUPPORTED_TYPES.get(op.type, "This equipment type is not in the supported registry.")
            raise DraftError("unsupported_type", f"{op.type}: {reason}", field="type")
        _unique_tag(document, op.tag)
        spec = UNIT_REGISTRY[op.type]
        object_id = _new_id(document, "u", op.id)
        params = {item.key: _default_param(item) for item in spec.params_for(spec.default_mode)
                  if item.default is not None}
        objects[object_id] = {"id": object_id, "kind": "unit", "type": op.type, "tag": op.tag, "x": op.x, "y": op.y,
                              "mode": spec.default_mode, "params": params, "options": {}, "reactions": []}
    elif isinstance(op, AddStream):
        _unique_tag(document, op.tag)
        object_id = _new_id(document, "s", op.id)
        stream_type = "EnergyStream" if op.stream_type == "energy" else "MaterialStream"
        objects[object_id] = {"id": object_id, "kind": "stream", "type": stream_type, "tag": op.tag,
                              "x": op.x, "y": op.y, "source": None, "target": None, "spec": {}}
    elif isinstance(op, Delete):
        item = _object(document, op.id)
        del objects[op.id]
        if item["kind"] == "unit":
            for stream in objects.values():
                for end in ("source", "target"):
                    if stream["kind"] == "stream" and (stream.get(end) or {}).get("unit") == op.id:
                        stream[end] = None
    elif isinstance(op, Move):
        item = _object(document, op.id)
        item["x"], item["y"] = op.x, op.y
    elif isinstance(op, Rename):
        item = _object(document, op.id)
        _unique_tag(document, op.tag, op.id)
        item["tag"] = op.tag
    elif isinstance(op, Connect):
        stream = _object(document, op.stream, "stream")
        unit = _object(document, op.unit, "unit")
        spec = _unit_spec(unit)
        stream_is_energy = stream["type"] == "EnergyStream"
        ports = ((spec.energy_outlets if op.end == "source" else spec.energy_inlets) if stream_is_energy
                 else (spec.outlets if op.end == "source" else spec.inlets))
        if op.port >= len(ports):
            raise DraftError("port_invalid", f"{spec.label} has no {'energy ' if stream_is_energy else ''}{'outlet' if op.end == 'source' else 'inlet'} "
                             f"port {op.port}", field="port")
        other_end = "target" if op.end == "source" else "source"
        if (stream.get(other_end) or {}).get("unit") == op.unit:
            raise DraftError("port_invalid", "A stream cannot leave and enter the same unit (recycles are unsupported)")
        occupant = _occupant(document, op.unit, op.end, op.port, energy=stream_is_energy)
        if occupant is not None and occupant["id"] != op.stream:
            raise DraftError("port_occupied", f"{unit['tag']} {ports[op.port]} is already connected to "
                             f"{occupant['tag']}", field="port")
        stream[op.end] = {"unit": op.unit, "port": op.port}
        if op.end == "source":
            # A stream with a source is computed by DWSIM; a feed specification would not be materialized.
            stream["spec"] = {}
    elif isinstance(op, Disconnect):
        stream = _object(document, op.stream, "stream")
        stream[op.end] = None
    elif isinstance(op, SetRoute):
        stream = _object(document, op.stream, "stream")
        points = [{"x": point.x, "y": point.y} for point in op.points]
        if any(a == b for a, b in zip(points, points[1:], strict=False)) or any(
            a["x"] != b["x"] and a["y"] != b["y"] for a, b in zip(points, points[1:], strict=False)
        ):
            raise DraftError("route_invalid", "Route points must form nonzero axis-parallel segments", field="points")
        stream["route"] = points
    elif isinstance(op, SetOrientation):
        unit = _object(document, op.id, "unit")
        for key in ("flip_x", "flip_y"):
            value = getattr(op, key)
            if value is True:
                unit[key] = True
            elif value is False:
                unit.pop(key, None)
    elif isinstance(op, SetStreamSpec):
        stream = _object(document, op.stream, "stream")
        material_input = any(getattr(op, key) is not None for key in STREAM_SPECS) or (
            op.composition is not None or op.composition_basis is not None)
        if stream["type"] == "EnergyStream":
            if material_input:
                raise DraftError("spec_on_energy_stream", "Energy streams accept only duty", field="spec")
            if "duty" in op.clear:
                stream["spec"].pop("duty", None)
            if op.duty is not None:
                stream["spec"]["duty"] = _si(op.duty, "power", "Energy flow")
            return
        if stream.get("source") is not None and material_input:
            raise DraftError("spec_on_product", f"{stream['tag']} is computed by its upstream unit; only feed "
                             "streams (no source) take specifications", field="spec")
        for cleared in op.clear:
            stream["spec"].pop(cleared, None)
        if op.duty is not None:
            raise DraftError("spec_unsupported", "Material streams do not accept a duty", field="duty")
        for keys in FEED_ALTERNATIVES.values():
            if sum(getattr(op, key) is not None for key in keys) > 1:
                labels = " and ".join(STREAM_SPECS[key][2].lower() for key in keys)
                raise DraftError("spec_alternatives_conflict", f"Specify {labels} as alternatives, not both",
                                 field=keys[0])
        for spec_key, (kind, _arg, label) in STREAM_SPECS.items():
            value = getattr(op, spec_key)
            if value is None:
                continue
            converted = _si(value, kind, label)
            if spec_key == "vapor_fraction":
                if not 0.0 <= converted["si"] <= 1.0:
                    raise DraftError("quantity_out_of_range", "Vapor fraction must be between 0 and 1", field=spec_key)
            elif spec_key in FEED_ALTERNATIVES["flow"]:
                if converted["si"] < 0:
                    raise DraftError("quantity_invalid", f"{label} cannot be negative", field=spec_key)
            elif converted["si"] <= 0:
                raise DraftError("quantity_invalid", f"{label} must be positive", field=spec_key)
            stream["spec"][spec_key] = converted
            # Choosing one alternative replaces the other: DWSIM holds exactly one basis per group.
            for keys in FEED_ALTERNATIVES.values():
                if spec_key in keys:
                    for other in keys:
                        if other != spec_key:
                            stream["spec"].pop(other, None)
        if op.composition_basis is not None:
            if op.composition_basis == "mole":
                stream["spec"]["composition_basis"] = "mole"
            else:
                stream["spec"].pop("composition_basis", None)
        if op.composition is not None:
            unknown = sorted(set(op.composition) - set(document["compounds"]))
            if unknown:
                raise DraftError("compound_undeclared", f"compounds {unknown} are not declared in the draft thermo",
                                 field="composition")
            stream["spec"]["composition"] = {name: float(value) for name, value in sorted(op.composition.items())
                                             if value > 0}
    elif isinstance(op, SetStreamCulture):
        stream = _object(document, op.stream, "stream")
        if stream["type"] == "EnergyStream" or stream.get("source") is not None:
            raise DraftError("culture_on_non_feed", "Culture can only be specified on material feed streams.", field="culture")
        if op.culture is None:
            stream["spec"].pop("culture", None)
        else:
            kinds = {"biomass": "mass_concentration", "nitrogen": "mass_concentration",
                     "phosphorus": "mass_concentration", "oxygen": "mass_concentration",
                     "dic": "molar_concentration", "ph": "ph", "salinity": "salinity"}
            unknown = sorted(set(op.culture) - set(kinds))
            if unknown:
                raise DraftError("culture_field_unsupported", f"Unsupported culture fields: {unknown}", field="culture")
            stored: dict[str, Any] = {}
            for name, quantity in op.culture.items():
                item = _si(quantity, kinds[name], name)
                if name == "ph" and not 0.0 <= item["si"] <= 14.0:
                    raise DraftError("quantity_out_of_range", "pH must be between 0 and 14.", field=name)
                if name == "salinity" and item["si"] > 300.0:
                    raise DraftError("quantity_out_of_range", "Salinity must be at most 300 g/kg.", field=name)
                if name != "ph" and item["si"] < 0.0:
                    raise DraftError("quantity_out_of_range", f"{name} concentration cannot be negative.", field=name)
                stored[name] = item
            stream["spec"]["culture"] = stored
    elif isinstance(op, SetUnitParams):
        unit = _object(document, op.unit, "unit")
        spec = _unit_spec(unit)
        if unit["type"] == STUDIO_UNIT:
            _set_studio_params(unit, op)
            return
        if op.unset:
            bad = sorted({key for key in op.unset if key not in PBR_T2_OPTIONAL_KEYS})
            if unit["type"] != "PhotobioreactorT1" or bad:
                raise DraftError("unset_unsupported", "Only the optional photobioreactor optics v2 inputs "
                                 f"{sorted(PBR_T2_OPTIONAL_KEYS)} can be unset" + (f"; not {bad}" if bad else ""),
                                 field="unset")
            for key in op.unset:
                unit["params"].pop(key, None)
        if op.mode is not None:
            if op.mode not in spec.modes:
                raise DraftError("mode_unsupported", f"{spec.label} mode {op.mode!r} is not supported; use one of "
                                 f"{list(spec.modes)}", field="mode")
            unit["mode"] = op.mode
            active = {param_spec.key for param_spec in spec.params_for(op.mode)}
            unit["params"] = {name: value for name, value in unit["params"].items() if name in active}
            for param_spec in spec.params_for(op.mode):
                if param_spec.key not in unit["params"] and param_spec.default is not None:
                    unit["params"][param_spec.key] = _default_param(param_spec)
        for key, quantity in op.values.items():
            if (unit["type"] == "PhotobioreactorT1" and key in {item.key for item in PBR_T2_SPECS}
                    and (unit.get("options") or {}).get("optics_model") != "t2"
                    and op.options.get("optics_model") != "t2"):
                raise DraftError("param_inactive", "T2 optics inputs require the T2 optics model.", field=key)
            param = next((item for item in (*spec.params, *(PBR_T2_SPECS if unit["type"] == "PhotobioreactorT1" else ()))
                          if item.key == key), None)
            if param is None:
                raise DraftError("param_unsupported", f"{spec.label} has no supported parameter {key!r}", field=key)
            if unit["mode"] not in param.modes:
                raise DraftError("param_inactive", f"{param.label} is not used in {spec.label} mode "
                                 f"{unit['mode']!r}", field=key)
            converted = _si(quantity, param.kind, param.label)
            below = param.minimum_si is not None and (
                converted["si"] <= param.minimum_si if param.exclusive_minimum else converted["si"] < param.minimum_si)
            if below or (param.maximum_si is not None and converted["si"] > param.maximum_si):
                raise DraftError("quantity_out_of_range", f"{param.label} is outside its supported range", field=key)
            if unit["type"] == "SpecifiedSeparator" and key in {"biomass_recovery", "concentration_factor"}:
                floor = 0.0 if key == "biomass_recovery" else 1.0
                if converted["si"] <= floor:
                    raise DraftError("quantity_out_of_range", f"{param.label} must be greater than {floor:g}", field=key)
            if unit["type"] == "PhotobioreactorT1" and key in {"tube_count", "tubes_across", "layers"} and converted["si"] != int(converted["si"]):
                raise DraftError("quantity_out_of_range", f"{param.label} must be a whole number", field=key)
            unit["params"][key] = converted
        if unit["type"] == "PhotobioreactorT1" and op.values and pbr_temperature_invalid(unit["params"]):
            raise DraftError("quantity_out_of_range",
                             "Mean culture temperature minus the diel amplitude must stay above 0 K",
                             field="temperature_amplitude")
        if op.options:
            if unit["type"] == "PhotobioreactorT1" and set(op.options) == {"optics_model"} and op.options["optics_model"] in {"t1", "t2"}:
                if op.options["optics_model"] == "t1":
                    unit["options"].pop("optics_model", None)
                    for t2_param in PBR_T2_SPECS:
                        unit["params"].pop(t2_param.key, None)
                else:
                    unit["options"]["optics_model"] = "t2"
            else:
                raise DraftError("option_unsupported", f"{spec.label} does not expose these verified options", field="options")
        if op.reactions is not None:
            missing = sorted(set(op.reactions) - set(document.get("reactions", {})))
            if missing:
                raise DraftError("reaction_not_found", f"Reaction ids {missing} are not defined", field="reactions")
            unit["reactions"] = list(dict.fromkeys(op.reactions))
    elif isinstance(op, SetUnitModel):
        unit = _object(document, op.unit, "unit")
        if unit["type"] != "PhotobioreactorT1":
            raise DraftError("unit_model_unsupported", "Only Photobioreactor (T1) accepts a biological model card.", field="model")
        if op.model is None:
            unit.pop("model", None)
        else:
            unit["model"] = op.model.model_dump(mode="json")
    elif isinstance(op, SetUnitHydraulics):
        unit = _object(document, op.unit, "unit")
        if unit["type"] != "PhotobioreactorT1":
            raise DraftError("unit_hydraulics_unsupported", "Only Photobioreactor (T1) accepts a CFD reduced-model hydraulics pin.", field="hydraulics")
        if op.hydraulics is None:
            unit.pop("hydraulics_model", None)
        else:
            unit["hydraulics_model"] = op.hydraulics.model_dump(mode="json")
    elif isinstance(op, SetReactions):
        reactions = {}
        for reaction_id, reaction in op.reactions.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", reaction_id):
                raise DraftError("reaction_id_invalid", f"Reaction id {reaction_id!r} is invalid", field="reactions")
            reactions[reaction_id] = _reaction_item(reaction, set(document["compounds"]))
        document["reactions"] = reactions
    elif isinstance(op, SetReactorReaction):
        unit = _object(document, op.unit, "unit")
        if unit["type"] not in {"PFR", "CSTR"}:
            raise DraftError("reaction_unit_unsupported", "Only PFR and CSTR accept reactions", field="unit")
        selected = list(unit.get("reactions", []))
        if op.reaction is None:
            unit["reactions"] = [value for value in selected if value != op.reaction_id]
            if not any(op.reaction_id in other.get("reactions", []) for other in objects.values()
                       if other["kind"] == "unit"):
                document["reactions"].pop(op.reaction_id, None)
        else:
            document["reactions"][op.reaction_id] = _reaction_item(op.reaction, set(document["compounds"]))
            if op.reaction_id not in selected:
                selected.append(op.reaction_id)
            unit["reactions"] = selected
    elif isinstance(op, SetThermo):
        if op.compounds is not None:
            unknown = [name for name in op.compounds if name not in COMPOUNDS]
            if unknown or len(set(op.compounds)) != len(op.compounds):
                raise DraftError("compound_unsupported", f"compounds {unknown or 'duplicated'} are not in the supported "
                                 "list", field="compounds")
            document["compounds"] = list(op.compounds)
            for stream in objects.values():
                composition = stream.get("spec", {}).get("composition")
                if composition and set(composition) - set(op.compounds):
                    stream["spec"].pop("composition")
        if op.property_package is not None:
            if op.property_package not in PROPERTY_PACKAGES:
                raise DraftError("package_unsupported", f"{op.property_package!r} is not a supported property package",
                                 field="property_package")
            document["property_package"] = op.property_package
    elif isinstance(op, SetSchedule | SetController | SetScenario):
        key = {SetSchedule: "schedules", SetController: "controllers", SetScenario: "scenarios"}[type(op)]
        value = copy.deepcopy(op.value)
        value["id"] = op.id
        document.setdefault(key, {})[op.id] = value
        _validate_dynamic_refs(document, key, value)
    elif isinstance(op, DeleteSchedule | DeleteController | DeleteScenario):
        key = {DeleteSchedule: "schedules", DeleteController: "controllers", DeleteScenario: "scenarios"}[type(op)]
        document.setdefault(key, {}).pop(op.id, None)
    elif isinstance(op, SetSolver):
        if op.solver is None:
            document.pop("solver", None)
        else:
            from app.modules.process_stack import tear_solver

            try:
                tear_solver.parse_settings(op.solver)
            except ValueError as exc:
                raise DraftError("solver_settings_invalid", str(exc), field="solver") from exc
            document["solver"] = copy.deepcopy(op.solver)
    else:  # pragma: no cover - the discriminated union is closed
        raise DraftError("op_unsupported", "Unsupported draft operation")


def _validate_dynamic_refs(document: dict[str, Any], kind: str, value: dict[str, Any]) -> None:
    """Cheap reference checks; physics, profile integrity and topology belong to prepare()."""
    from pydantic import ValidationError

    from app.modules.process_stack.dynamic_models import Controller, Scenario, Schedule

    models: dict[str, type[Schedule] | type[Controller] | type[Scenario]] = {
        "schedules": Schedule, "controllers": Controller, "scenarios": Scenario,
    }
    model = models[kind]
    try:
        model.model_validate(value)
    except ValidationError as exc:
        raise DraftError("dynamic_content_invalid", f"Invalid {kind[:-1]} content", field=kind,
                         errors=exc.errors(include_url=False)) from exc
    if kind == "scenarios":
        for tag in value.get("units", []):
            if not any(o.get("kind") == "unit" and o.get("type") in {"PhotobioreactorT1", "HoldupTank"}
                       and o.get("tag") == tag
                       for o in document.get("objects", {}).values()):
                raise DraftError("dynamic_unit_not_found", f"Scenario T1 unit tag {tag!r} was not found", field="units")
        for ref in value.get("profiles", []):
            if not isinstance(ref, dict) or not isinstance(ref.get("profile_id"), str) or not isinstance(ref.get("digest"), str):
                raise DraftError("dynamic_profile_invalid", "Scenario profiles require profile_id and digest", field="profiles")
        if value.get("schedule_id") and value["schedule_id"] not in document.get("schedules", {}):
            raise DraftError("dynamic_schedule_not_found", "Scenario schedule was not found", field="schedule_id")
        missing_controllers = sorted(set(value.get("controllers", [])) - set(document.get("controllers", {})))
        if missing_controllers:
            raise DraftError("dynamic_controller_not_found", f"Controllers {missing_controllers} were not found",
                             field="controllers")
    if kind == "controllers":
        for field in ("unit", "stream", "splitter"):
            tag = value.get(field)
            if tag is not None and not any(o.get("tag") == tag for o in document.get("objects", {}).values()):
                raise DraftError("dynamic_reference_not_found", f"Referenced tag {tag!r} was not found", field=field)


def apply_ops(document: dict[str, Any], ops: list[Any], resolve: studio_unit.Resolver | None = None) -> dict[str, Any]:
    result = copy.deepcopy(document)
    for index, op in enumerate(ops):
        try:
            apply_op(result, op, resolve)
        except DraftError as exc:
            exc.detail.setdefault("op_index", index)
            raise
    return result


# ---------------------------------------------------------------- validation


def validate_document(document: dict[str, Any], workspace_id: str | None = None,
                      resolve: studio_unit.Resolver | None = None) -> list[dict[str, Any]]:
    """Instant Jarvis-side pre-run findings; DWSIM's own check runs only on Validate.

    ``workspace_id`` is optional; without it the Photobioreactor model-card library cannot be consulted.
    """
    findings: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, tag: str = "", field: str = "") -> None:
        findings.append({"severity": severity, "code": code, "object": tag, "field": field, "message": message,
                         "source": "jarvis"})

    objects = document["objects"]
    studio_findings = studio_unit.pin_findings(document, workspace_id, resolve)
    if any(item["severity"] == "blocker" for item in studio_findings):
        return studio_findings  # a pin that does not verify makes the unit's ports untrustworthy
    if not objects:
        add("blocker", "DRAFT_EMPTY", "Add streams and equipment to build the flowsheet.")
    if document.get("solver") is not None:
        from app.modules.process_stack import tear_solver

        try:
            solver = tear_solver.parse_settings(document["solver"])
        except ValueError as exc:
            add("blocker", "SOLVER_SETTINGS_INVALID", str(exc), field="solver")
        else:
            recycles = {item["tag"] for item in objects.values() if item["kind"] == "unit" and item["type"] == "Recycle"}
            for tag in sorted(set(solver.seeds) - recycles):
                add("blocker", "SOLVER_SEED_UNKNOWN_RECYCLE", f"Solver seed names {tag}, which is not a Recycle.",
                    tag, "solver.seeds")
    if not document["compounds"]:
        add("blocker", "NO_COMPOUNDS", "Declare the compounds in Thermo.", field="compounds")
    if not document["property_package"]:
        add("blocker", "NO_PROPERTY_PACKAGE", "Choose a property package in Thermo.", field="property_package")
    for unit in sorted((item for item in objects.values() if item["kind"] == "unit"), key=lambda item: item["tag"]):
        spec = _unit_spec(unit)
        inlets = [port for port in range(len(spec.inlets)) if _occupant(document, unit["id"], "target", port)]
        if len(inlets) < spec.required_inlets:
            add("blocker", "UNIT_INLET_MISSING",
                f"{spec.label} needs {spec.required_inlets} connected inlet(s); {len(inlets)} connected.", unit["tag"],
                "inlets")
        for port, name in enumerate(spec.inlets):
            if port >= spec.required_inlets and not _occupant(document, unit["id"], "target", port):
                add("warning", "OPTIONAL_PORT_UNCONNECTED", f"Optional {name} inlet is not connected.",
                    unit["tag"], f"inlets[{port}]")
        required_params = spec.params_for(unit["mode"])
        if unit["type"] == "PhotobioreactorT1" and (unit.get("options") or {}).get("optics_model") == "t2":
            required_params += tuple(item for item in PBR_T2_SPECS if item.key not in PBR_T2_OPTIONAL_KEYS)
        for param in required_params:
            if param.key not in unit["params"]:
                add("blocker", "UNIT_PARAM_MISSING", f"Set {param.label}.", unit["tag"], param.key)
        if unit["type"] in {"PFR", "CSTR"} and not unit.get("reactions"):
            add("blocker", "REACTION_SET_MISSING", "Assign at least one kinetic reaction to the reactor.", unit["tag"],
                "reactions")
        assigned_ids = list(unit.get("reactions", []))
        assigned = [document.get("reactions", {}).get(rid, {}) for rid in assigned_ids]
        if any(reaction.get("rate_law") for reaction in assigned):
            if len(assigned) != 1:
                add("blocker", "KINETICS_REACTION_SET_UNSUPPORTED", "Use one typed rate law per reactor.",
                    unit["tag"], "reactions")
            if unit["mode"] not in {"isothermic", "outlet_temperature"}:
                add("blocker", "KINETICS_MODE_UNSUPPORTED", "This reactor mode is unsupported for typed kinetics.",
                    unit["tag"], "mode")
            if unit["type"] == "PFR" and unit["mode"] != "isothermic" and any(
                (reaction.get("rate_law") or {}).get("temperature") for reaction in assigned
            ):
                add("blocker", "KINETICS_TEMPERATURE_PROFILE_UNSUPPORTED",
                    "Temperature factors require an isothermic PFR.", unit["tag"], "mode")
            for rid, reaction in zip(assigned_ids, assigned, strict=True):
                if reaction.get("rate_law"):
                    for problem in kinetics.problems(reaction, document["compounds"]):
                        add("blocker", problem["code"], problem["message"], unit["tag"],
                            f"reactions.{rid}.{problem['field']}")
                    if (reaction.get("provenance") or {}).get("kind") in {"operator_estimate", "synthetic"}:
                        add("info", "KINETICS_PARAMETER_UNVERIFIED",
                            "Kinetic parameters are an operator estimate or synthetic, not measured or cited.",
                            unit["tag"], "reactions")
        needs_energy = (unit["type"] == "DistillationColumn" or
                        unit["type"] == "CSTR" or
                        unit["type"] == "PFR" and unit["mode"] == "heat_exchange" or
                        unit["type"] in {"Heater", "Cooler"} and unit["mode"] == "energy_stream")
        if needs_energy and not any(_occupant(document, unit["id"], "target", port, energy=True)
                                    for port in range(len(spec.energy_inlets))):
            add("blocker", "REACTOR_ENERGY_STREAM_MISSING" if unit["type"] in {"CSTR", "PFR"}
                else "UNIT_ENERGY_INLET_MISSING", f"Connect an energy stream to {spec.label}.", unit["tag"],
                "energy_inlets")
        elif not needs_energy:
            for port, name in enumerate(spec.energy_inlets):
                if not _occupant(document, unit["id"], "target", port, energy=True):
                    add("warning", "OPTIONAL_PORT_UNCONNECTED", f"Optional {name} energy inlet is not connected.",
                        unit["tag"], f"energy_inlets[{port}]")
        if unit["type"] == "DistillationColumn" and not any(
            _occupant(document, unit["id"], "source", port, energy=True) for port in range(len(spec.energy_outlets))
        ):
            add("blocker", "UNIT_ENERGY_OUTLET_MISSING", "Connect the condenser duty energy stream.", unit["tag"],
                "energy_outlets")
        elif unit["type"] != "DistillationColumn":
            for port, name in enumerate(spec.energy_outlets):
                if not _occupant(document, unit["id"], "source", port, energy=True):
                    add("warning", "OPTIONAL_PORT_UNCONNECTED", f"Optional {name} energy outlet is not connected.",
                        unit["tag"], f"energy_outlets[{port}]")
        outlet_count = sum(bool(_occupant(document, unit["id"], "source", port))
                           for port in range(len(spec.outlets)))
        for port, name in enumerate(spec.outlets):
            if unit["type"] == "Splitter" and port == 2:
                if not _occupant(document, unit["id"], "source", port):
                    add("warning", "OPTIONAL_PORT_UNCONNECTED", f"Optional {name} outlet is not connected.",
                        unit["tag"], name)
                continue
            if not _occupant(document, unit["id"], "source", port):
                add("blocker", "UNIT_OUTLET_MISSING", f"Connect a stream to the {name} outlet.", unit["tag"], name)
        if unit["type"] == "Splitter" and unit["mode"] == "split_ratios":
            ratio1 = unit["params"].get("split_ratio_1", {}).get("si")
            ratio2 = unit["params"].get("split_ratio_2", {}).get("si")
            if ratio1 is not None and ratio2 is not None:
                total = float(ratio1) + float(ratio2)
                invalid = total > 1.0 + _COMPOSITION_TOL or outlet_count == 2 and abs(total - 1.0) > _COMPOSITION_TOL
                if invalid:
                    add("blocker", "SPLIT_RATIOS_INVALID", "Split ratios must sum to 1 for two outlets and at most 1 for three.",
                        unit["tag"], "split_ratios")
    for stream in sorted((item for item in objects.values() if item["kind"] == "stream"), key=lambda item: item["tag"]):
        if stream["source"] is None and stream["target"] is None:
            add("blocker", "STREAM_DANGLING", "This stream is connected to nothing.", stream["tag"])
            continue
        if stream["type"] == "EnergyStream":
            if "duty" in stream["spec"]:
                consumer = objects.get(stream["target"]["unit"]) if stream.get("target") else None
                if consumer is None or consumer["mode"] != "energy_stream":
                    add("blocker", "ENERGY_DUTY_MODE_MISMATCH", "A specified energy duty requires a connected unit in EnergyStream mode.", stream["tag"], "duty")
            continue
        if stream["source"] is None:
            if "pressure" not in stream["spec"]:
                add("blocker", "FEED_SPEC_MISSING", "Feed needs pressure.", stream["tag"], "pressure")
            for keys in FEED_ALTERNATIVES.values():
                if not any(key in stream["spec"] for key in keys):
                    labels = " or ".join(STREAM_SPECS[key][2].lower() for key in keys)
                    add("blocker", "FEED_SPEC_MISSING", f"Feed needs {labels}.", stream["tag"], keys[0])
            composition = stream["spec"].get("composition")
            basis = "Mole" if stream["spec"].get("composition_basis") == "mole" else "Mass"
            if not composition:
                add("blocker", "FEED_COMPOSITION_MISSING", "Feed needs a composition.", stream["tag"], "composition")
            else:
                total = sum(composition.values())
                if abs(total - 1.0) > _COMPOSITION_TOL:
                    add("blocker", "COMPOSITION_SUM", f"{basis} fractions sum to {total:.6g}, not 1.", stream["tag"],
                        "composition")
                undeclared = sorted(set(composition) - set(document["compounds"]))
                if undeclared:
                    add("blocker", "COMPOUND_UNDECLARED", f"{undeclared} are not declared in Thermo.", stream["tag"],
                        "composition")
    from app.modules.process_stack import mixed

    graph: dict[str, list[str]] = {}
    for stream in objects.values():
        if stream["kind"] == "stream" and stream.get("source") and stream.get("target"):
            graph.setdefault(stream["source"]["unit"], []).append(stream["target"]["unit"])
    for tags in mixed.recycle_free_cycles(document):
        add("blocker", "RECYCLE_REQUIRED", f"Process loop {tags} requires a Recycle block.", tags[0],
            "connections")
    findings.extend(mixed.validation_findings(document))
    _advice(document, add, _cycle_members(graph))
    findings.extend(culture_engine.culture_findings(document))
    findings.extend(pbr_validation.pbr_findings(document, workspace_id))
    return [*findings, *studio_findings]


def _cycle_members(graph: dict[str, list[str]]) -> set[str]:
    """Units that lie on at least one directed loop of the material graph."""
    members: set[str] = set()
    for start in graph:
        stack, seen = list(graph.get(start, [])), set()
        while stack:
            node = stack.pop()
            if node == start:
                members.add(start)
                break
            if node not in seen:
                seen.add(node)
                stack.extend(graph.get(node, []))
    return members


def _si_of(item: dict[str, Any], key: str, section: str = "params") -> float | None:
    value = (item.get(section) or {}).get(key)
    return float(value["si"]) if isinstance(value, dict) and "si" in value else None


def _advice(document: dict[str, Any], add: Any, in_loop: set[str]) -> None:
    """Non-blocking warnings: DWSIM can run the draft, but the result may mean little.

    Simple deterministic sanity checks on declared inputs only; DWSIM stays the simulator.
    """
    objects = document["objects"]
    streams = [item for item in objects.values() if item["kind"] == "stream" and item["type"] != "EnergyStream"]
    feeds = [item for item in streams if item["source"] is None and item["target"] is not None]
    for stream in sorted(feeds, key=lambda item: item["tag"]):
        for key in FEED_ALTERNATIVES["flow"]:
            if _si_of(stream, key, "spec") == 0.0:
                add("warning", "FEED_FLOW_ZERO", "Feed flow is zero: DWSIM can solve it, but every downstream "
                    "stream and duty will be empty.", stream["tag"], key)
    used = {name for stream in feeds for name, value in (stream["spec"].get("composition") or {}).items() if value > 0}
    used |= {name for reaction in document.get("reactions", {}).values() for name in reaction["stoichiometry"]}
    if any(stream["spec"].get("composition") for stream in feeds):
        for name in document["compounds"]:
            if name not in used:
                add("warning", "COMPOUND_UNUSED", f"{name} is declared in Thermo but no feed or reaction contains it.",
                    "", "compounds")

    def inlet_feed(unit: dict[str, Any]) -> dict[str, Any] | None:
        stream = _occupant(document, unit["id"], "target", 0)
        return stream if stream is not None and stream["source"] is None else None

    for unit in sorted((item for item in objects.values() if item["kind"] == "unit"), key=lambda item: item["tag"]):
        spec, mode, tag = _unit_spec(unit), unit["mode"], unit["tag"]
        feed = inlet_feed(unit)
        no_effect = None
        if unit["type"] in {"Heater", "Cooler"}:
            if mode in {"heat_added", "heat_removed", "heat_added_removed"} and _si_of(unit, "heat_duty") == 0.0:
                no_effect = "heat duty is zero"
            elif mode == "temperature_change" and _si_of(unit, "temperature_change") == 0.0:
                no_effect = "temperature change is zero"
            elif mode == "outlet_temperature" and feed is not None and (
                _si_of(unit, "outlet_temperature") is not None
                and _si_of(unit, "outlet_temperature") == _si_of(feed, "temperature", "spec")
            ):
                no_effect = "outlet temperature equals the feed temperature"
        elif unit["type"] == "Pump":
            if mode == "pressure_increase" and _si_of(unit, "pressure_increase") == 0.0:
                no_effect = "pressure increase is zero"
            elif mode == "outlet_pressure" and feed is not None and _si_of(unit, "outlet_pressure") is not None and (
                _si_of(feed, "pressure", "spec") is not None
                and _si_of(unit, "outlet_pressure") <= _si_of(feed, "pressure", "spec")  # type: ignore[operator]
            ):
                add("warning", "PUMP_OUTLET_NOT_ABOVE_INLET", "Pump outlet pressure is not above the feed pressure.",
                    tag, "outlet_pressure")
        elif unit["type"] == "Valve":
            if mode == "pressure_drop" and _si_of(unit, "pressure_drop") == 0.0:
                no_effect = "pressure drop is zero"
            elif mode == "outlet_pressure" and feed is not None and _si_of(unit, "outlet_pressure") is not None and (
                _si_of(feed, "pressure", "spec") is not None
                and _si_of(unit, "outlet_pressure") >= _si_of(feed, "pressure", "spec")  # type: ignore[operator]
            ):
                add("warning", "VALVE_OUTLET_NOT_BELOW_INLET", "Valve outlet pressure is not below the feed pressure.",
                    tag, "outlet_pressure")
        elif unit["type"] == "HeatExchanger":
            if _si_of(unit, "area") == 0.0 or _si_of(unit, "overall_coefficient") == 0.0:
                no_effect = "area or overall heat transfer coefficient is zero"
        elif unit["type"] == "PFR":
            if _si_of(unit, "volume") == 0.0 or _si_of(unit, "length") == 0.0:
                add("warning", "REACTOR_VOLUME_ZERO", "Reactor volume or length is zero: nothing can react.", tag,
                    "volume")
        elif unit["type"] == "DistillationColumn":
            stages, feed_stage = _si_of(unit, "number_of_stages"), _si_of(unit, "feed_stage")
            if stages is not None and feed_stage is not None and not 0 < feed_stage < stages - 1:
                add("warning", "COLUMN_FEED_STAGE_EDGE", "The feed stage is the condenser, the reboiler or beyond "
                    "the column; feed an intermediate stage.", tag, "feed_stage")
            top, bottom = _si_of(unit, "top_pressure"), _si_of(unit, "bottom_pressure")
            if top is not None and bottom is not None and bottom < top:
                add("warning", "COLUMN_PRESSURE_INVERTED", "Bottom pressure is below top pressure.", tag,
                    "bottom_pressure")
            if _si_of(unit, "condenser_spec") == 0.0:
                add("warning", "COLUMN_ZERO_REFLUX", "Reflux ratio is zero: the column cannot separate.", tag,
                    "condenser_spec")
            if _si_of(unit, "reboiler_spec") == 0.0:
                add("warning", "COLUMN_ZERO_BOTTOMS", "Bottoms molar flow is zero.", tag, "reboiler_spec")
        elif unit["type"] == "Recycle" and unit["id"] not in in_loop:
            add("warning", "RECYCLE_NOT_IN_LOOP", "This Recycle block is not on a process loop, so it tears nothing.",
                tag, "connections")
        if no_effect:
            add("warning", "UNIT_NO_EFFECT", f"{spec.label}: {no_effect}, so it does nothing.", tag, mode or "")
        uses_energy = (unit["type"] in {"DistillationColumn", "CSTR"} or unit["type"] == "PFR" and mode == "heat_exchange"
                       or unit["type"] in {"Heater", "Cooler"} and mode == "energy_stream")
        if not uses_energy and any(_occupant(document, unit["id"], "target", port, energy=True)
                                   for port in range(len(spec.energy_inlets))):
            add("warning", "ENERGY_STREAM_IGNORED", f"The connected energy stream is ignored in {spec.label} mode "
                f"{mode!r}.", tag, "energy_inlets")


# ---------------------------------------------------------------- revisions


def _digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(document).encode()).hexdigest()


def _content_projection(document: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(document)
    for key in ("schedules", "controllers", "scenarios"):
        value.setdefault(key, {})
    for item in value.get("objects", {}).values():
        item.pop("x", None)
        item.pop("y", None)
        item.pop("route", None)
        item.pop("flip_x", None)
        item.pop("flip_y", None)
    return value


def content_digest(document: dict[str, Any]) -> str:
    """Digest semantic draft content while retaining the editor's layout-free currentness rule."""
    return hashlib.sha256(canonical(_content_projection(document)).encode()).hexdigest()


def current_digest(workspace_id: str, draft_id: str) -> str:
    """Return the latest layout-free semantic digest for a persisted Process draft."""
    directory = draft_dir(workspace_id, draft_id)
    head = _head(directory)
    record = load_revision(directory, head["revision"])
    return content_digest(record["document"])


def _write_revision(directory: Path, document: dict[str, Any], *, parent: str | None, actor: str,
                    ops: list[dict[str, Any]], provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    head = _read_json(directory / "head.json", "draft_not_found", "missing") if (directory / "head.json").exists() \
        else None
    seq = head["seq"] + 1 if head else 1
    digest = _digest(document)
    record = {"seq": seq, "revision": f"{seq}:{digest[:16]}", "document_sha256": digest, "parent_revision": parent,
              "actor": actor, "ops": ops, "created_at": _now(), "document": document}
    if provenance:
        record["provenance"] = provenance
    _write_json(directory / "revisions" / f"{seq}.json", record)
    _write_json(directory / "head.json", {key: value for key, value in record.items() if key != "document"}
                | {"name": document["name"]})
    return record


def _head(directory: Path) -> dict[str, Any]:
    return _read_json(directory / "head.json", "draft_not_found", "Process draft was not found")


def load_revision(directory: Path, revision: str) -> dict[str, Any]:
    match = re.fullmatch(r"(\d+):([0-9a-f]{16})", revision)
    if not match:
        raise DraftError("revision_not_found", "Draft revision was not found", 404)
    record = _read_json(directory / "revisions" / f"{int(match.group(1))}.json", "revision_not_found",
                        "Draft revision was not found")
    if record["revision"] != revision or _digest(record["document"]) != record["document_sha256"]:
        raise DraftError("revision_integrity_error", "Draft revision failed its content check", 409)
    return record


def create_draft(workspace_id: str, name: str) -> dict[str, Any]:
    draft_id = str(uuid4())
    directory = drafts_root(workspace_id) / draft_id
    with _lock(directory):
        _write_revision(directory, empty_document(name), parent=None, actor="operator", ops=[])
    return projection(workspace_id, draft_id)


def fork_drafts(source_workspace_id: str, target_workspace_id: str) -> dict[str, dict[str, str]]:
    """Copy every source draft into a not-yet-committed target workspace, keeping draft ids.

    Each fork is one fresh root revision holding the source head document, with provenance naming the
    source workspace and revision. Revision history, runs and proposals are not copied. Returns
    draft id -> {source_revision, revision}. The target workspace row may still be uncommitted, so its
    directory is derived without the workspace lookup.
    """
    forked: dict[str, dict[str, str]] = {}
    root = _drafts_path(target_workspace_id)
    source_root = drafts_root(source_workspace_id)
    for directory in sorted(source_root.iterdir() if source_root.exists() else []):
        if not (directory / "head.json").exists():
            continue
        with _lock(directory):
            head = _head(directory)
            source = load_revision(directory, head["revision"])
        target = root / directory.name
        with _lock(target):
            record = _write_revision(
                target, source["document"], parent=None, actor="design-version-fork",
                ops=[{"op": "fork", "source_revision": head["revision"]}],
                provenance={"forked_from": {"workspace_id": source_workspace_id, "draft_id": directory.name,
                                            "revision": head["revision"]}})
        forked[directory.name] = {"source_revision": head["revision"], "revision": record["revision"]}
    return forked


def list_drafts(workspace_id: str) -> list[dict[str, Any]]:
    root = drafts_root(workspace_id)
    rows = []
    for directory in sorted(root.iterdir() if root.exists() else []):
        if (directory / "head.json").exists():
            head = _head(directory)
            rows.append({"draft_id": directory.name, "name": head.get("name"), "revision": head["revision"],
                         "updated_at": head["created_at"]})
    return sorted(rows, key=lambda row: row["updated_at"], reverse=True)


def patch(workspace_id: str, draft_id: str, expected_revision: str, ops: list[Any], *,
          actor: str = "operator", provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    directory = draft_dir(workspace_id, draft_id)
    with _lock(directory):
        head = _head(directory)
        if expected_revision != head["revision"]:
            raise DraftError("revision_conflict", "Expected revision is stale", 409, current_revision=head["revision"])
        current = load_revision(directory, head["revision"])
        document = apply_ops(current["document"], ops, studio_unit.resolver(workspace_id))
        _write_revision(directory, document, parent=head["revision"], actor=actor,
                        ops=[op.model_dump(mode="json") if isinstance(op, BaseModel) else op for op in ops],
                        provenance=provenance)
    return projection(workspace_id, draft_id)


def restore(workspace_id: str, draft_id: str, expected_revision: str, source_revision: str) -> dict[str, Any]:
    directory = draft_dir(workspace_id, draft_id)
    with _lock(directory):
        head = _head(directory)
        if expected_revision != head["revision"]:
            raise DraftError("revision_conflict", "Expected revision is stale", 409, current_revision=head["revision"])
        source = load_revision(directory, source_revision)
        _write_revision(directory, source["document"], parent=head["revision"], actor="operator",
                        ops=[{"op": "restore", "source_revision": source_revision}])
    return projection(workspace_id, draft_id)


def list_revisions(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    directory = draft_dir(workspace_id, draft_id)
    rows = []
    for seq in range(_head(directory)["seq"], 0, -1):
        path = directory / "revisions" / f"{seq}.json"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            rows.append({key: record[key] for key in ("seq", "revision", "parent_revision", "actor", "created_at")}
                        | {"ops": [op.get("op") for op in record["ops"]]})
    return rows


# ---------------------------------------------------------------- runs


def runs_dir(directory: Path) -> Path:
    return directory / "runs"


def list_runs(directory: Path) -> list[dict[str, Any]]:
    root = runs_dir(directory)
    rows = []
    for path in root.glob("*/run.json") if root.exists() else []:
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    for row in rows:
        if not isinstance(row.get("outcome"), dict):
            # Runs recorded before spec 181 get their outcome on read, against their own revision.
            try:
                document = load_revision(directory, row["draft_revision"])["document"]
            except (DraftError, KeyError, TypeError):
                document = None
            row["outcome"] = run_outcome(row, document)
    return sorted(rows, key=lambda row: row["started_at"], reverse=True)


def run_outcome(run: dict[str, Any], document: dict[str, Any] | None = None) -> dict[str, Any]:
    """The single operator-facing outcome of a draft run (spec 181); see ``results_view.run_outcome``."""
    from app.modules.process_stack import results_view
    return results_view.run_outcome(run, document)


def _outcome(run: dict[str, Any]) -> dict[str, Any]:
    from app.modules.process_stack import results_view
    return results_view.outcome_of(run)


def record_run(directory: Path, run: dict[str, Any]) -> None:
    _write_json(runs_dir(directory) / run["run_id"] / "run.json", run)


def results_state(head: dict[str, Any], runs: list[dict[str, Any]], document: dict[str, Any] | None = None,
                  edits_since: int | None = None, workspace_id: str | None = None) -> dict[str, Any]:
    solved = _converged(runs)
    last = runs[0] if runs else None
    if solved is None:
        return {"state": "none", "last_attempt": _attempt(last), "outcome": _outcome(last) if last else None}
    seq = int(solved["draft_revision"].split(":", 1)[0])
    current = solved["draft_revision"] == head["revision"]
    if not current and document is not None:
        from app.modules.process_stack import mixed
        from app.modules.process_stack.draft_compiler import expected, fingerprint, process_view, result_fingerprint
        if solved.get("mixed_solve"):
            try:
                current = mixed.fingerprint(document, mixed.partition(document),
                                            workspace_id) == solved["process_fingerprint"]
            except (DraftError, KeyError, TypeError, ValueError):
                current = False
        elif solved.get("result_fingerprint"):
            try:
                current = result_fingerprint(document, dwsim_version=solved["dwsim_version"],
                                             mcp_sha256=solved["mcp_sha256"]) == solved["result_fingerprint"]
            except (DraftError, KeyError, TypeError, ValueError):
                current = False
        elif solved.get("process_fingerprint"):
            # Layout (positions, orientation, routes) never decides staleness; process meaning does.
            try:
                process = process_view(expected(document))
            except (DraftError, KeyError, TypeError, ValueError):
                # Incomplete newly-added equipment is a normal editable draft state after a run.
                # It cannot be materialized yet, so prior results are stale rather than a 500 projection.
                current = False
            else:
                current = fingerprint(process, dwsim_version=solved["dwsim_version"],
                                      mcp_sha256=solved["mcp_sha256"]) == solved["process_fingerprint"]
        else:  # runs recorded before spec 162 carry only the full materialization fingerprint
            current = fingerprint(expected(document), dwsim_version=solved["dwsim_version"],
                                  mcp_sha256=solved["mcp_sha256"]) == solved["materialization_fingerprint"]
    if (last is not solved and last is not None and last.get("action") == "run"
            and _outcome(last)["state"] in {"non_converged", "failed"} and _same_process(last, solved, head)):
        # A later non-converged or failed Run of the same process never leaves an older converged
        # run as the current answer, whatever engine solved either (spec 181 decision 2).
        current = False
    return {"state": "current" if current else "stale", "run_id": solved["run_id"],
            "draft_revision": solved["draft_revision"],
            "edits_since": 0 if current else edits_since if edits_since is not None else head["seq"] - seq,
            "materialization_fingerprint": solved.get("materialization_fingerprint"), "last_attempt": _attempt(last),
            "outcome": _outcome(last) if last else None}


def _converged(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest Run whose outcome is converged: the only run that can be the current answer."""
    return next((run for run in runs if run["action"] == "run" and run["status"] == "completed"
                 and _outcome(run)["state"] == "converged"), None)


def _same_process(attempt: dict[str, Any], solved: dict[str, Any], head: dict[str, Any]) -> bool:
    if attempt.get("draft_revision") == head["revision"]:
        return True
    for key in ("process_fingerprint", "result_fingerprint", "materialization_fingerprint"):
        if attempt.get(key) and attempt.get(key) == solved.get(key):
            return True
    return False


def _attempt(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {**{key: run.get(key) for key in ("run_id", "action", "status", "draft_revision", "started_at")},
            "outcome": _outcome(run)}


LAYOUT_OPS = frozenset({"move", "set_route", "set_orientation"})
_PATH_TEXT = re.compile(r"(?<![\w.])(?:[A-Za-z]:\\|/)[^\s'\"]*[/\\][^\s'\"]*")


def plain_text(value: Any, limit: int = 300) -> str:
    """Bounded, path-free plain text of a DWSIM/runtime message for the UI and the agent view."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    text = " ".join(_PATH_TEXT.sub("<path>", text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def dwsim_feedback(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """What DWSIM itself said on the last attempt: check findings, solver errors, failed objects."""
    if run is None:
        return None
    detail = run.get("error_detail") or {}
    solve = run.get("solve") or {}
    outcome = _outcome(run)
    return {
        "run_id": run.get("run_id"), "action": run.get("action"), "status": run.get("status"),
        "draft_revision": run.get("draft_revision"),
        "outcome": {key: outcome.get(key) for key in ("state", "label", "reason", "message", "residual",
                                                      "iterations", "worst_tear", "results_available")},
        "check_findings": [{key: item.get(key) for key in ("severity", "code", "object", "message", "fix")}
                           for item in (run.get("dwsim_check") or {}).get("findings", [])][:20],
        "solve_errors": list(dict.fromkeys(plain_text(item) for item in solve.get("errors", [])))[:20],
        "failed_objects": list({(item.get("tag"), plain_text(item.get("error") or "not calculated")):
                                 {"tag": item.get("tag"),
                                  "error": plain_text(item.get("error") or "not calculated")}
                                 for item in solve.get("failed_objects", [])}.values())[:20],
        "error": plain_text(run["error"]) if run.get("error") else None,
        "error_step": detail.get("step"),
        "dwsim_message": plain_text(detail["dwsim_message"]) if detail.get("dwsim_message") else None,
        "materialization_diffs": len(run.get("materialization_diffs") or []),
    }


def result_findings(document: dict[str, Any], solved: dict[str, Any] | None,
                    last: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Post-run advice from DWSIM's own results: degenerate products and failed objects."""
    findings: list[dict[str, Any]] = []
    tags = {item["tag"]: item for item in document["objects"].values()}
    if solved is not None:
        for tag, unit_result in sorted((solved.get("units") or {}).items()):
            for item in ((unit_result or {}).get("kinetics") or {}).get("findings") or []:
                findings.append({"severity": item.get("severity", "warning"), "code": item.get("code", ""),
                                 "object": tag, "field": "reactions", "message": item.get("message", ""),
                                 "source": "jarvis"})
        findings.extend(solved.get("culture_findings") or [])
        findings.extend(solved.get("mixed_findings") or [])
        for tag, result in sorted((solved.get("streams") or {}).items()):
            item = tags.get(tag)
            flow = result.get("mass_flow_kg_s")
            if item is not None and item.get("source") is not None and isinstance(flow, (int, float)) and abs(flow) < 1e-12:
                findings.append({"severity": "warning", "code": "PRODUCT_FLOW_ZERO", "object": tag, "field": "mass_flow",
                                 "message": f"DWSIM computed zero flow for this product (run on revision "
                                            f"{solved['draft_revision'].split(':', 1)[0]}).", "source": "dwsim_result"})
    if last is not None and last.get("status") == "failed":
        for item in (last.get("solve") or {}).get("failed_objects", [])[:20]:
            if str(item.get("code") or "").startswith("KINETICS_"):
                # A failed attempt has no accepted result, but cannot prevent the operator from retrying
                # after repairing the draft. Pre-Run blockers come only from validate_document.
                findings.append({"severity": "warning", "code": item["code"], "object": item.get("tag") or "",
                                 "field": "reactions", "message": "Last Run: " + plain_text(item.get("error") or item["code"]),
                                 "source": "jarvis"})
                continue
            findings.append({"severity": "warning", "code": "DWSIM_OBJECT_FAILED", "object": item.get("tag") or "",
                             "field": "", "message": f"DWSIM did not calculate it: "
                                                    f"{plain_text(item.get('error') or 'no error text')}",
                             "source": "dwsim_result"})
    return findings


# ---------------------------------------------------------------- projection


def _studio_palette(workspace_id: str) -> list[dict[str, Any]]:
    """Latest published Studio process-unit version per model, shaped for the Process palette (spec 190)."""
    from app.modules.studio import service as studio_service
    from app.modules.studio.unit_checks import HONEST_LIMITS

    latest: dict[str, dict[str, Any]] = {}
    for version in studio_service.published_units(workspace_id):
        latest[version["model_spec_id"]] = version  # published_units is oldest first
    rows = []
    for version in sorted(latest.values(), key=lambda item: item["name"].lower()):
        contract = version["contract"]
        rows.append({
            "type": STUDIO_UNIT, "label": version["name"], "owner": "jarvis_bio", "culture_rule": "studio",
            "model_spec_id": version["model_spec_id"], "version_id": version["version_id"],
            "version_label": version["version_label"], "version_number": version["version_number"],
            "source_sha256": version["source_sha256"], "contract_sha256": version["contract_sha256"],
            "inlets": [item["name"] for item in contract["inlets"]],
            "inlet_labels": [item["label"] for item in contract["inlets"]],
            "outlets": [item["name"] for item in contract["outlets"]],
            "outlet_labels": [item["label"] for item in contract["outlets"]],
            "required_inlets": len(contract["inlets"]), "energy_inlets": [], "energy_outlets": [],
            "params": [{"key": item["name"], "label": item["label"], "unit": item["unit"],
                        "description": item["description"], "minimum": item.get("min"), "maximum": item.get("max")}
                       for item in contract["parameters"]],
            "symbol": contract["symbol"], "culture_generation": contract["culture_generation"],
            "limits": HONEST_LIMITS, "steady_only": True})
    return rows


def registry_projection(workspace_id: str | None = None) -> dict[str, Any]:
    capabilities = _capability_manifest()
    return {
        **({"studio_units": _studio_palette(workspace_id)} if workspace_id else {}),
        "compiler_version": COMPILER_VERSION,
        "compounds": list(COMPOUNDS),
        "property_packages": list(PROPERTY_PACKAGES),
        "quantity_units": {kind: {"si": si, "display": list(display)} for kind, (si, display) in QUANTITY_UNITS.items()},
        "pbr_t2_params": [{"key": item.key, "label": item.label, "kind": item.kind,
                           "minimum": item.minimum_si, "maximum": item.maximum_si,
                           "exclusive_minimum": item.exclusive_minimum} for item in PBR_T2_SPECS],
        "kinetics": kinetics.form_table(),
        "stream_specs": [{"key": key, "label": label, "kind": kind} for key, (kind, _arg, label) in STREAM_SPECS.items()],
        "units": [
            {"type": spec.type, "label": spec.label, "owner": spec.owner, "culture_rule": spec.culture_rule,
             "inlets": list(spec.inlets), "outlets": list(spec.outlets),
             "energy_inlets": list(spec.energy_inlets), "energy_outlets": list(spec.energy_outlets),
             "energy_spec_modes": [], "required_inlets": spec.required_inlets, "modes": list(spec.modes),
             "params": [{"key": item.key, "label": item.label, "kind": item.kind, "modes": list(item.modes),
                         "default": item.default, "classification": "input", "minimum": item.minimum_si,
                         "maximum": item.maximum_si, "group": item.group,
                         "exclusive_minimum": item.exclusive_minimum} for item in spec.params],
             "reactions": spec.type in {"PFR", "CSTR"},
             "result_properties": capabilities["objects"].get(spec.dwsim_type, {}).get("result_properties", [])
             if spec.owner == "dwsim" else []}
            for spec in UNIT_REGISTRY.values()
        ],
        "dwsim_capabilities": capabilities,
        "unsupported": UNSUPPORTED_TYPES,
    }


def _display(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in sorted(document["objects"].values(), key=lambda value: (value["kind"], value["tag"])):
        rows.append(copy.deepcopy(item))
    return rows


def kinetics_scripts(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per reactor tag: the Jarvis-generated DWSIM script for its typed rate law, or why there is none yet."""
    scripts: dict[str, dict[str, Any]] = {}
    for unit in document["objects"].values():
        if unit["kind"] != "unit" or unit["type"] not in {"PFR", "CSTR"}:
            continue
        for rid in unit.get("reactions", []):
            reaction = document.get("reactions", {}).get(rid) or {}
            if not reaction.get("rate_law"):
                continue
            try:
                text: str | None = kinetics.render(reaction)
            except kinetics.KineticsError:
                text = None
            users = sum(1 for other in document["objects"].values()
                        if other["kind"] == "unit" and rid in other.get("reactions", []))
            scripts[unit["tag"]] = {"reaction_id": rid, "script_title": kinetics.script_title(unit["tag"], rid),
                                    "native_reaction_id": f"{rid}__{unit['tag']}" if users > 1 else rid,
                                    "script_text": text, "compilable": text is not None}
    return scripts


def dynamic_content(document: dict[str, Any]) -> dict[str, Any]:
    """Stored typed scenarios, schedules and controllers (spec 173), or empty when the draft has none."""
    content = {key: copy.deepcopy(document.get(key) or {}) for key in ("scenarios", "schedules", "controllers")}
    return content if any(content.values()) else {}


def projection(workspace_id: str, draft_id: str) -> dict[str, Any]:
    directory = draft_dir(workspace_id, draft_id)
    head = _head(directory)
    record = load_revision(directory, head["revision"])
    document = record["document"]
    runs = list_runs(directory)
    solved = _converged(runs)
    edits_since = None
    if solved is not None:
        solved_seq = int(solved["draft_revision"].split(":", 1)[0])
        edits_since = 0
        for seq in range(solved_seq + 1, head["seq"] + 1):
            revision = _read_json(directory / "revisions" / f"{seq}.json", "revision_not_found", "Draft revision was not found")
            edits_since += sum(1 for op in revision.get("ops", []) if op.get("op") not in LAYOUT_OPS)
    feed_basis = pbr_validation.pbr_feed_basis(document)
    scripts = kinetics_scripts(document)
    return {
        "workspace_id": workspace_id,
        "draft_id": draft_id,
        "name": document["name"],
        "revision": head["revision"],
        "seq": head["seq"],
        "compounds": document["compounds"],
        "property_package": document["property_package"],
        "objects": _display(document),
        "reactions": copy.deepcopy(document.get("reactions", {})),
        "kinetics_scripts": scripts,
        "findings": validate_document(document, workspace_id) + result_findings(document, solved, runs[0] if runs else None),
        **({"studio_units": studio_status} if (studio_status := studio_unit.projection(document, workspace_id)) else {}),
        "results": results_state(head, runs, document, edits_since, workspace_id),
        "dwsim": dwsim_feedback(runs[0] if runs else None),
        "proposals": [proposal for proposal in list_proposals(workspace_id, draft_id) if proposal["state"] == "pending"
                      or proposal["state"] == "stale"],
        **({"pbr_feed_basis": feed_basis} if feed_basis else {}),
        **({"solver": copy.deepcopy(document["solver"])} if document.get("solver") is not None else {}),
        **({"dynamic": dynamic} if (dynamic := dynamic_content(document)) else {}),
    }


# ---------------------------------------------------------------- proposals


def _current_value(item: dict[str, Any], prop: str, unit: str | None) -> Any:
    if item["kind"] == "stream":
        if prop == "composition":
            return item["spec"].get("composition")
        stored = item["spec"].get(prop)
        kind = STREAM_SPECS[prop][0]
    else:
        if prop == "mode":
            return item["mode"]
        stored = item["params"].get(prop)
        spec = _unit_spec(item)
        kind = next(param.kind for param in spec.params if param.key == prop)
    if stored is None:
        return None
    if unit is None:
        return {"value": stored["value"], "unit": stored["unit"]}
    return {"value": float(f"{convert_si(stored['si'], kind, unit):.12g}"), "unit": unit}


def _change_ops(document: dict[str, Any], changes: list[dict[str, Any]]) -> list[Any]:
    ops: list[Any] = []
    unit_ops: dict[str, SetUnitParams] = {}
    for change in changes:
        item = document["objects"][change["target_id"]]
        prop, proposed = change["property"], change["proposed"]
        if item["kind"] == "stream":
            if prop == "composition":
                ops.append(SetStreamSpec(op="set_stream_spec", stream=item["id"], composition=proposed))
            else:
                ops.append(SetStreamSpec.model_validate({"op": "set_stream_spec", "stream": item["id"], prop: proposed}))
        else:
            op = unit_ops.setdefault(item["id"], SetUnitParams(op="set_unit_params", unit=item["id"]))
            if prop == "mode":
                op.mode = proposed
            else:
                op.values[prop] = DraftQuantity.model_validate(proposed)
    # A mode change must precede its parameters, so unit ops carry both in one operation.
    return ops + list(unit_ops.values())


def _resolve_change(document: dict[str, Any], change: Any) -> dict[str, Any]:
    item = _by_tag(document, change.target)
    if item is None:
        raise DraftError("proposal_target_unknown", f"No object is tagged {change.target!r}", field=change.target)
    prop = change.property
    proposed = change.proposed
    if item["kind"] == "stream":
        if prop == "composition":
            if not isinstance(proposed, dict):
                raise DraftError("proposal_value_invalid", "composition takes {compound: mass fraction}", field=prop)
            unit = None
        elif prop in STREAM_SPECS:
            if not isinstance(proposed, DraftQuantity):
                raise DraftError("proposal_value_invalid", f"{prop} takes a value and unit", field=prop)
            unit = proposed.unit
        else:
            raise DraftError("proposal_property_unsupported", f"streams accept {[*STREAM_SPECS, 'composition']}",
                             field=prop)
    else:
        spec = _unit_spec(item)
        if prop == "mode":
            if not isinstance(proposed, str):
                raise DraftError("proposal_value_invalid", "mode takes a mode name", field=prop)
            unit = None
        elif any(param.key == prop for param in spec.params):
            if not isinstance(proposed, DraftQuantity):
                raise DraftError("proposal_value_invalid", f"{prop} takes a value and unit", field=prop)
            unit = proposed.unit
        else:
            raise DraftError("proposal_property_unsupported",
                             f"{spec.label} accepts {['mode', *[param.key for param in spec.params]]}", field=prop)
    try:
        current = _current_value(item, prop, unit)
    except (EvaluationRefusal, ValueError) as exc:
        raise DraftError("proposal_value_invalid", f"{prop}: unit {unit!r} does not fit", field=prop) from exc
    return {"target": item["tag"], "target_id": item["id"], "kind": item["kind"], "property": prop,
            "current": current,
            "proposed": proposed.model_dump() if isinstance(proposed, DraftQuantity) else proposed}


def _proposal_path(directory: Path, proposal_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", proposal_id):
        raise DraftError("proposal_not_found", "Proposal was not found", 404)
    return directory / "proposals" / f"{proposal_id}.json"


def create_proposal(workspace_id: str, draft_id: str, request: ProposalRequest) -> dict[str, Any]:
    """Validate and store a pending change set; the draft itself never changes here."""
    directory = draft_dir(workspace_id, draft_id)
    with _lock(directory):
        head = _head(directory)
        if request.base_revision != head["revision"]:
            raise DraftError("revision_conflict", "Proposal base revision is not the current draft revision", 409,
                             current_revision=head["revision"])
        document = load_revision(directory, head["revision"])["document"]
        changes = [_resolve_change(document, change) for change in request.changes]
        apply_ops(document, _change_ops(document, changes))  # dry run: refuse anything the owner would refuse
        proposal = {"proposal_id": uuid4().hex, "draft_id": draft_id, "base_revision": head["revision"],
                    "status": "pending", "changes": changes, "rationale": request.rationale,
                    "source": request.source, "created_at": _now(), "applied_revision": None}
        _write_json(_proposal_path(directory, proposal["proposal_id"]), proposal)
    return _proposal_view(proposal, head)


def _proposal_view(proposal: dict[str, Any], head: dict[str, Any]) -> dict[str, Any]:
    state = proposal["status"]
    if state == "pending" and proposal["base_revision"] != head["revision"]:
        state = "stale"
    return {**proposal, "state": state}


def list_proposals(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    directory = draft_dir(workspace_id, draft_id)
    head = _head(directory)
    rows = []
    for path in (directory / "proposals").glob("*.json") if (directory / "proposals").exists() else []:
        rows.append(_proposal_view(json.loads(path.read_text(encoding="utf-8")), head))
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)


def decide_proposal(workspace_id: str, draft_id: str, proposal_id: str, *, approve: bool,
                    accepted_changes: list[int] | None = None) -> dict[str, Any]:
    directory = draft_dir(workspace_id, draft_id)
    path = _proposal_path(directory, proposal_id)
    with _lock(directory):
        proposal = _read_json(path, "proposal_not_found", "Proposal was not found")
        if proposal["status"] != "pending":
            raise DraftError("proposal_decided", f"Proposal is already {proposal['status']}", 409)
        head = _head(directory)
        if not approve:
            proposal.update(status="rejected", decided_at=_now())
            _write_json(path, proposal)
            return {"proposal": _proposal_view(proposal, head), "draft": None}
        if proposal["base_revision"] != head["revision"]:
            raise DraftError("proposal_stale", "The draft changed after this proposal; it cannot be applied", 409,
                             current_revision=head["revision"])
        indices = list(range(len(proposal["changes"]))) if accepted_changes is None else sorted(set(accepted_changes))
        if not indices or any(index < 0 or index >= len(proposal["changes"]) for index in indices):
            raise DraftError("proposal_selection_invalid", "Select at least one listed change")
        selected = [proposal["changes"][index] for index in indices]
        document = load_revision(directory, head["revision"])["document"]
        ops = _change_ops(document, selected)
        updated = apply_ops(document, ops)
        record = _write_revision(directory, updated, parent=head["revision"],
                                 actor=f"hermes_proposal:{proposal_id}" if proposal["source"].startswith("hermes")
                                 else f"proposal:{proposal_id}",
                                 ops=[op.model_dump(mode="json") for op in ops])
        proposal.update(status="approved", decided_at=_now(), applied_revision=record["revision"],
                        accepted_changes=indices)
        _write_json(path, proposal)
        head = _head(directory)
    return {"proposal": _proposal_view(proposal, head), "draft": projection(workspace_id, draft_id)}


def latest_draft_id(workspace_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", workspace_id) or not (
        build_paths().workspaces_dir / workspace_id / "process" / "drafts"
    ).is_dir():
        return None
    rows = list_drafts(workspace_id)
    return rows[0]["draft_id"] if rows else None


def agent_view(workspace_id: str, draft_id: str | None) -> dict[str, Any]:
    """Compact read projection for Hermes: tags, typed parameters in their entered units, results state."""
    draft_id = draft_id or latest_draft_id(workspace_id)
    if draft_id is None:
        raise DraftError("draft_not_found", "This workspace has no process draft", 404)
    view = projection(workspace_id, draft_id)
    objects = []
    for item in view["objects"]:
        row: dict[str, Any] = {"tag": item["tag"], "type": item["type"]}
        if item["kind"] == "stream":
            row["from"] = _endpoint_tag(view, item["source"])
            row["to"] = _endpoint_tag(view, item["target"])
            row["spec"] = {key: ({"value": value["value"], "unit": value["unit"]}
                                  if isinstance(value, dict) and {"value", "unit"} <= value.keys() else value)
                           for key, value in item["spec"].items()}
        else:
            row["mode"] = item["mode"]
            row["params"] = {key: {"value": value["value"], "unit": value["unit"]}
                             for key, value in item["params"].items()}
        objects.append(row)
    def line(item: dict[str, Any]) -> str:
        return f"{item['code']} {item['object'] or 'flowsheet'}: {item['message']}"

    blockers = [line(item) for item in view["findings"] if item["severity"] == "blocker"]
    findings = view["findings"]
    # Exact duplicate findings can arrive through separate DWSIM result paths. Keep
    # distinct messages for an object, but don't make Hermes repeat the same fact.
    deduped_findings: list[dict[str, Any]] = []
    seen_findings: set[tuple[Any, ...]] = set()
    for item in findings:
        key = (item["severity"], item["code"], item["object"], item["message"], item.get("fix"))
        if key not in seen_findings:
            seen_findings.add(key)
            deduped_findings.append(item)

    warning_items = [item for item in deduped_findings if item["severity"] != "blocker"]
    no_text_failures = [item for item in warning_items
                        if item["code"] == "DWSIM_OBJECT_FAILED"
                        and item["message"] == "DWSIM did not calculate it: no error text"]
    warnings = [line(item) for item in warning_items if item not in no_text_failures]
    if no_text_failures:
        tags = ", ".join(dict.fromkeys(item["object"] for item in no_text_failures if item["object"]))
        warnings.append(f"DWSIM did not calculate: {tags or 'one or more objects'}")
    return {"draft_id": view["draft_id"], "revision": view["revision"], "compounds": view["compounds"],
            "property_package": view["property_package"], "objects": objects,
            "guidance": ("blockers stop Run; warnings mean DWSIM can run but the result may lack physical "
                         "meaning; dwsim lists what DWSIM itself reported on the last attempt"),
            "blockers": blockers[:20], "warnings": warnings[:20],
            "blocker_count": len(blockers), "warning_count": len(warnings),
            "findings": [f"{item['object']}: {item['message']}" for item in deduped_findings][:20],
            "dwsim": view["dwsim"],
            "results": view["results"],
            "current_results": _key_results(_solved_run(workspace_id, view), view["results"]),
            "to_change_values": (f"call jarvis_process_propose with base_revision '{view['revision']}' and changes "
                                 "[{target: <tag>, property: <name from proposable>, proposed: {value, unit}}]; "
                                 "the operator approves before anything changes"),
            "proposable": {"stream": [*STREAM_SPECS, "composition"],
                           **{spec.type: ["mode", *[param.key for param in spec.params]]
                              for spec in UNIT_REGISTRY.values() if spec.params}}}


def _solved_run(workspace_id: str, view: dict[str, Any]) -> dict[str, Any] | None:
    """The solved run the projection's results state refers to, if any."""
    run_id = view["results"].get("run_id")
    return get_run(workspace_id, view["draft_id"], run_id) if run_id else None


def _key_results(run: dict[str, Any] | None, results: dict[str, Any]) -> dict[str, Any] | None:
    """Bounded DWSIM result summary for Hermes, labelled with its revision and current/stale state."""
    if run is None:
        return None
    streams = {tag: {key: f"{value['value']} {value['unit']}" for key, value in (result.get("display") or {}).items()}
               | ({"vapor_fraction": round(result["vapor_fraction"], 4)}
                  if isinstance(result.get("vapor_fraction"), (int, float)) else {})
               for tag, result in sorted((run.get("streams") or {}).items())[:24]}
    units = {tag: {"calculated": result.get("calculated"),
                   **({"error": plain_text(result["error"], 160)} if result.get("error") else {})}
             for tag, result in sorted((run.get("units") or {}).items())[:24]}
    balance = run.get("mass_balance") or {}
    return {"state": results["state"], "run_id": run["run_id"], "draft_revision": run["draft_revision"],
            "streams": streams, "units": units,
            "mass_balance_residual_kg_s": balance.get("residual_kg_s") if balance.get("status") == "calculated" else None}


def _endpoint_tag(view: dict[str, Any], endpoint: dict[str, Any] | None) -> str | None:
    if endpoint is None:
        return None
    unit = next((item for item in view["objects"] if item["id"] == endpoint["unit"]), None)
    return f"{unit['tag']}:{endpoint['port']}" if unit else None


# ---------------------------------------------------------------- validate / run (the only DWSIM contact)


def execute(workspace_id: str, draft_id: str, revision: str, action: str) -> dict[str, Any]:
    """Compile the exact revision into DWSIM; validate, or solve only after a verified materialization."""
    return perform(prepare(workspace_id, draft_id, revision, action))


@dataclass
class PreparedRun:
    """A refused-early, ready-to-perform draft run: typed refusals happen before any run is recorded."""

    workspace_id: str
    draft_id: str
    directory: Path
    record: dict[str, Any]
    client: Any
    run: dict[str, Any]


def prepare(workspace_id: str, draft_id: str, revision: str, action: str) -> PreparedRun:
    from app.modules.process_stack import draft_compiler, editor

    if action not in {"validate", "run"}:
        raise DraftError("action_invalid", "action must be validate or run")
    directory = draft_dir(workspace_id, draft_id)
    record = load_revision(directory, revision)
    if action == "run":
        try:
            draft_compiler.refuse_dynamic_only_units(record["document"])
        except draft_compiler.MaterializationError as exc:
            raise DraftError(exc.code, str(exc), 422, **exc.detail) from exc
    blockers = [item for item in validate_document(record["document"], workspace_id) if item["severity"] == "blocker"]
    if blockers:
        raise DraftError("draft_invalid", "Resolve the draft findings before DWSIM can materialize it", 422,
                         findings=blockers)
    if action == "run":
        try:
            studio_unit.preflight(record["document"])
        except studio_unit.StudioUnitError as exc:
            raise DraftError(exc.code, exc.message, 503, **(exc.detail or {})) from exc
    try:
        client, mcp_sha256, dwsim_version = editor._client()
    except editor.EditorError as exc:
        raise DraftError(exc.code, "DWSIM runtime is unavailable", 503) from exc
    run_id = draft_compiler.new_run_id()
    started = _now()
    run: dict[str, Any] = {"run_id": run_id, "action": action, "draft_id": draft_id, "draft_revision": revision,
                           "draft_document_sha256": record["document_sha256"],
                           "compiler_version": COMPILER_VERSION, "dwsim_version": dwsim_version,
                           "mcp_sha256": mcp_sha256, "started_at": started}
    return PreparedRun(workspace_id, draft_id, directory, record, client, run)


def perform(prepared: PreparedRun, observer: Any = None) -> dict[str, Any]:
    """Run a prepared draft run and record it; ``observer`` sees each tear iteration as it happens (spec 210)."""
    from app.modules.process_stack import draft_compiler, mixed, mixed_runtime

    workspace_id, draft_id, directory = prepared.workspace_id, prepared.draft_id, prepared.directory
    record, client, run = prepared.record, prepared.client, prepared.run
    action, revision, run_id = run["action"], run["draft_revision"], run["run_id"]
    dwsim_version, mcp_sha256 = run["dwsim_version"], run["mcp_sha256"]
    try:
        with client:
            if mixed.needs_mixed_solve(record["document"]):
                outcome = mixed_runtime.run(record["document"], action=action, client=client,
                                            dwsim_version=dwsim_version, mcp_sha256=mcp_sha256,
                                            run_dir=runs_dir(directory) / run_id, workspace_id=workspace_id,
                                            **({"observer": observer} if observer is not None else {}))
            else:
                outcome = draft_compiler.materialize(
                    record["document"], action=action, client=client, dwsim_version=dwsim_version,
                    mcp_sha256=mcp_sha256, label=f"jarvis-draft-{draft_id[:8]}-{revision}",
                    keep_case=runs_dir(directory) / run_id / "solved.dwxmz" if action == "run" else None)
        run.update(outcome)
        if action == "run" and outcome.get("status") == "completed" and not mixed.needs_mixed_solve(record["document"]):
            culture_results, culture_findings = culture_engine.propagate(record["document"], outcome.get("streams", {}))
            run["culture"] = culture_results
            run["culture_findings"] = culture_findings
            run["result_fingerprint"] = draft_compiler.result_fingerprint(
                record["document"], dwsim_version=dwsim_version, mcp_sha256=mcp_sha256)
    except draft_compiler.MaterializationError as exc:
        run.update(status=exc.code, error=str(exc), error_detail=exc.detail)
    except Exception as exc:  # noqa: BLE001 - DWSIM process failures become a recorded, truthful attempt
        run.update(status="runtime_failed", error=type(exc).__name__,
                   error_detail={"dwsim_message": plain_text(str(exc))} if str(exc) else {})
    run["finished_at"] = _now()
    for result in (run.get("streams") or {}).values():
        result["display"] = _stream_display(result)
    run["outcome"] = run_outcome(run, record["document"])
    with _lock(directory):
        record_run(directory, run)
    return {"run": run, "draft": projection(workspace_id, draft_id)}


def _stream_display(result: dict[str, Any]) -> dict[str, Any]:
    """Operator-unit presentation of DWSIM SI results, converted server-side (no frontend math)."""
    display: dict[str, Any] = {}
    for key, si_key, kind, unit in (("temperature", "temperature_K", "temperature", "degC"),
                                    ("pressure", "pressure_Pa", "pressure", "bar"),
                                    ("mass_flow", "mass_flow_kg_s", "mass_flow", "kg/h"),
                                    ("molar_flow", "molar_flow_mol_s", "molar_flow", "kmol/h")):
        value = result.get(si_key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            display[key] = {"value": float(f"{convert_si(float(value), kind, unit):.6g}"), "unit": unit}
    # Volumetric flow is shown exactly as DWSIM reported it (value and unit), never derived here.
    volumetric = next((row for row in result.get("properties") or [] if row.get("name") == "Volumetric Flow"), None)
    if volumetric and isinstance(volumetric.get("value"), (int, float)) and math.isfinite(volumetric["value"]):
        display["volumetric_flow"] = {"value": float(f"{float(volumetric['value']):.6g}"), "unit": volumetric["unit"]}
    return display


def get_run(workspace_id: str, draft_id: str, run_id: str) -> dict[str, Any]:
    directory = draft_dir(workspace_id, draft_id)
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise DraftError("run_not_found", "Run was not found", 404)
    run = _read_json(runs_dir(directory) / run_id / "run.json", "run_not_found", "Run was not found")
    if not isinstance(run.get("outcome"), dict):
        try:
            document = load_revision(directory, run["draft_revision"])["document"]
        except (DraftError, KeyError, TypeError):
            document = None
        run["outcome"] = run_outcome(run, document)
    return run


def run_summaries(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    directory = draft_dir(workspace_id, draft_id)
    return [{key: run.get(key) for key in ("run_id", "action", "status", "draft_revision", "started_at",
                                           "materialization_fingerprint", "compile_seconds", "outcome")}
            for run in list_runs(directory)]


def run_results(workspace_id: str, draft_id: str, run_id: str) -> dict[str, Any]:
    """Results view of one run against the draft document at the run's own revision (spec 181)."""
    from app.modules.process_stack import results_view

    directory = draft_dir(workspace_id, draft_id)
    run = get_run(workspace_id, draft_id, run_id)
    document = load_revision(directory, run["draft_revision"])["document"]
    view = results_view.build(run, document)
    studio = studio_unit.run_provenance(run, document, workspace_id)
    return {**view, "studio_units": studio} if studio else view
