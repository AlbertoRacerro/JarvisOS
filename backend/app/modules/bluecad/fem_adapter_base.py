"""BLUECAD CalculiX FEM adapter.

The solver deck is assembled only from mesh input plus schema-shaped AnalysisSpec
fields. ``force_total`` loads are approximated in v0 by dividing each component
uniformly across nodes found in the target ``LOAD_<label>`` node set.
Expected units follow BLUECAD v0: geometry in mm, force in N, mass in kg, stress
in MPa; no unit conversion is performed here.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from app.modules.bluecad.export import sha256_file
from app.modules.bluecad.mesh_adapter import _group_label
from app.modules.bluecad.registry import ToolRegistryError, resolve_tool, run_tool

_ERROR_PATTERN = re.compile(r"\*error|error:", re.IGNORECASE)
_DIVERGED_PATTERN = re.compile(r"diverg|non.?conver|no convergence", re.IGNORECASE)
_SUPPORTED_METRICS = {"max_displacement", "max_von_mises"}
_VOLUME_ELEMENT_PREFIXES = ("C3D", "DC3D")


def solve_static_analysis(
    analysis_spec: dict[str, Any],
    mesh_result: dict[str, Any],
    out_dir: str | Path,
    *,
    registry_path: str | Path | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Assemble a static CalculiX deck, run registered ccx, and summarize results."""
    if analysis_spec.get("analysis_type") != "static":
        return _error("SOLVE_ERROR", {"message": "only static analysis is supported"}, {})
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, Any] = {}
    log_path = out_path / "analysis.log"
    try:
        tool = resolve_tool("calculix", registry_path)
        mesh_path = Path(mesh_result["artifacts"]["mesh_inp"]["path"]).resolve()
        mesh = _parse_mesh(mesh_path.read_text(encoding="utf-8"))
        mesh_include = Path(os.path.relpath(mesh_path, out_path.resolve()))
        inp_path = out_path / "analysis.inp"
        inp_path.write_text(_deck_text(analysis_spec, mesh_include, mesh), encoding="utf-8")
        artifacts.update(_artifact_map({"inp": inp_path}))
        run = run_tool(
            "calculix",
            [inp_path.stem],
            out_path,
            timeout_s or float(analysis_spec.get("timeout_s", 60)),
            registry_path,
        )
        log_path.write_text((run.stdout or "") + (run.stderr or ""), encoding="utf-8")
        artifacts.update(_artifact_map({"log": log_path}))
        if run.timed_out:
            return _error("TIMEOUT", {"returncode": run.returncode}, artifacts, run.returncode, tool)
        log_text = log_path.read_text(encoding="utf-8")
        if run.returncode != 0 or _ERROR_PATTERN.search(log_text):
            code = "SOLVE_DIVERGED" if _DIVERGED_PATTERN.search(log_text) else "SOLVE_ERROR"
            return _error(code, {"returncode": run.returncode}, artifacts, run.returncode, tool)
        frd_path = out_path / "analysis.frd"
        dat_path = out_path / "analysis.dat"
        artifacts.update(_artifact_map({"frd": frd_path, "dat": dat_path}))
        parsed = _parse_outputs(frd_path, dat_path, mesh)
    except ToolRegistryError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        artifacts.update(_artifact_map({"log": log_path}))
        return _error("SOLVE_ERROR", {"registry_code": exc.code, **(exc.detail or {})}, artifacts)
    except (OSError, KeyError, ValueError) as exc:
        artifacts.update(_artifact_map({"log": log_path}))
        return _error("PARSE_ERROR", {"message": str(exc)}, artifacts)
    return {
        "schema_version": "bluecad_result_summary_v0_1",
        "verdict": "pass",
        "errors": [],
        "solver": {"tool_id": "calculix", "version": tool["version_pin"], "returncode": run.returncode},
        "analysis_type": "static",
        **parsed,
        "artifacts": artifacts,
    }


def append_tier3_checks(
    report: dict[str, Any],
    result_summary: dict[str, Any],
    pass_criteria: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, Any]:
    """Append deterministic Tier 3 checks to a validation report."""
    checks = list(report.get("checks", []))
    errors = list(report.get("errors", []))
    criteria = _criteria_list(pass_criteria)
    for item in criteria:
        metric = item["metric"]
        if metric not in _SUPPORTED_METRICS:
            errors.append({"code": "UNKNOWN_METRIC", "detail": {"metric": metric}})
            checks.append({"id": f"T3_{metric.upper()}", "tier": 3, "status": "error", "detail": {"metric": metric}})
            continue
        actual = float(result_summary[metric]["value"])
        expected = float(item["value"])
        op = item["op"]
        passed = _compare(actual, op, expected)
        checks.append(
            {
                "id": f"T3_{metric.upper()}",
                "tier": 3,
                "status": "pass" if passed else "fail",
                "detail": {"metric": metric, "op": op, "actual": actual, "value": expected},
                "hint": f"{metric} must be {op} {expected:.9g}",
            }
        )
    verdict = (
        "error"
        if errors or any(check["status"] == "error" for check in checks)
        else ("fail" if any(check["status"] == "fail" for check in checks) else report.get("verdict", "pass"))
    )
    return {**report, "checks": checks, "errors": errors, "verdict": verdict}


def _deck_text(spec: dict[str, Any], mesh_path: Path, mesh: dict[str, Any]) -> str:
    material = spec["material"]
    _require_element_set(mesh, "BODY")
    lines = [
        "** BLUECAD generated static deck",
        f"*INCLUDE, INPUT={mesh_path.as_posix()}",
        f'*MATERIAL, NAME={material["name"]}',
        "*ELASTIC",
        f'{material["E"]}, {material["nu"]}',
        "*DENSITY",
        f'{material["rho"]}',
        f'*SOLID SECTION, ELSET=BODY, MATERIAL={material["name"]}',
        "*BOUNDARY",
    ]
    fixed_sets: list[str] = []
    for bc in spec.get("bcs", []):
        name = f'BC_{_group_label(bc["port_label"])}'
        _require_node_set(mesh, name)
        if bc.get("kind", "fixed") != "fixed":
            raise ValueError("only fixed boundary conditions are supported")
        fixed_sets.append(name)
        lines.append(f"{name}, 1, 3, 0")
    lines.extend(["*STEP", "*STATIC"])
    for load in spec.get("loads", []):
        name = f'LOAD_{_group_label(load["port_label"])}'
        if load.get("type", "force_total") == "force_total":
            _require_node_set(mesh, name)
            nodes = sorted(mesh["node_sets"][name])
            force = load.get("force", load.get("vector_n"))
            if not force or not nodes:
                raise ValueError(f"load {name} has no force vector or nodes")
            lines.append("*CLOAD")
            for node in nodes:
                for dof, value in enumerate(force, start=1):
                    if value:
                        lines.append(f"{node}, {dof}, {float(value) / len(nodes):.12g}")
        elif load.get("type") == "pressure":
            _require_element_set(mesh, name)
            lines.extend(["*DLOAD", f"{name}, P, {float(load['pressure']):.12g}"])
        else:
            raise ValueError("unsupported load type")
    lines.extend(["*NODE FILE", "U", "*EL FILE", "S"])
    if fixed_sets:
        lines.extend([f"*NODE PRINT, NSET={fixed_sets[0]}", "RF"])
    lines.extend(["*END STEP", ""])
    return "\n".join(lines)


def _parse_mesh(text: str) -> dict[str, Any]:
    nodes: set[int] = set()
    elements: dict[int, dict[str, Any]] = {}
    node_sets: dict[str, set[int]] = {}
    element_sets: dict[str, set[int]] = {}
    section: str | None = None
    active_set: str | None = None
    active_type: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("**") or line.startswith("*******"):
            continue
        if line.startswith("*"):
            header, params = _parse_header(line)
            section = None
            active_set = None
            active_type = None
            if header == "NODE":
                section = "node"
            elif header == "ELEMENT":
                section = "element"
                active_type = params.get("TYPE", "").upper()
                active_set = params.get("ELSET")
                if active_set:
                    element_sets.setdefault(active_set, set())
            elif header == "NSET":
                section = "nset"
                active_set = params.get("NSET")
                if not active_set:
                    raise ValueError("NSET section missing NSET name")
                node_sets.setdefault(active_set, set())
            elif header == "ELSET":
                section = "elset"
                active_set = params.get("ELSET")
                if not active_set:
                    raise ValueError("ELSET section missing ELSET name")
                element_sets.setdefault(active_set, set())
            continue

        values = _integer_values(line)
        if not values:
            continue
        if section == "node":
            nodes.add(values[0])
        elif section == "element":
            element_id = values[0]
            connectivity = values[1:]
            elements[element_id] = {"type": active_type or "", "nodes": connectivity}
            if active_set:
                element_sets[active_set].add(element_id)
        elif section == "nset" and active_set:
            node_sets[active_set].update(values)
        elif section == "elset" and active_set:
            element_sets[active_set].update(values)

    for set_name, element_ids in element_sets.items():
        if set_name.startswith(("BC_", "LOAD_")) and set_name not in node_sets:
            member_nodes: set[int] = set()
            for element_id in element_ids:
                member_nodes.update(elements.get(element_id, {}).get("nodes", []))
            if member_nodes:
                node_sets[set_name] = member_nodes

    node_to_elements: dict[int, set[int]] = {}
    for element_id, element in elements.items():
        if str(element["type"]).startswith(_VOLUME_ELEMENT_PREFIXES):
            for node in element["nodes"]:
                node_to_elements.setdefault(node, set()).add(element_id)
    return {
        "nodes": nodes,
        "elements": elements,
        "node_sets": node_sets,
        "element_sets": element_sets,
        "node_to_elements": node_to_elements,
    }


def _parse_outputs(frd_path: Path, dat_path: Path, mesh: dict[str, Any]) -> dict[str, Any]:
    if not frd_path.exists():
        raise ValueError("missing frd output")
    text = frd_path.read_text(encoding="utf-8", errors="replace")
    displacement, stress = _parse_native_frd(text)
    if not displacement or not stress:
        displacement, stress = _parse_legacy_frd(text)
    if not displacement or not stress:
        raise ValueError("frd missing displacement or stress records")

    max_u_node, max_u = max(
        ((node_id, math.sqrt(sum(component * component for component in values[:3]))) for node_id, values in displacement.items()),
        key=lambda item: item[1],
    )
    max_vm_node, max_vm = max(
        ((node_id, _von_mises(values)) for node_id, values in stress.items()),
        key=lambda item: item[1],
    )
    adjacent = sorted(mesh["node_to_elements"].get(max_vm_node, set()))
    if not adjacent:
        raise ValueError(f"stress node {max_vm_node} is not attached to a volume element")
    reactions = _parse_reactions(dat_path) if dat_path.exists() else []
    return {
        "max_displacement": {"node_id": max_u_node, "value": max_u},
        "max_von_mises": {"element_id": adjacent[0], "node_id": max_vm_node, "value": max_vm},
        "reactions": reactions,
    }


def _parse_native_frd(text: str) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
    """Parse CalculiX fixed-width nodal DISP/STRESS result blocks."""
    lines = text.splitlines()
    displacement: dict[int, list[float]] = {}
    stress: dict[int, list[float]] = {}
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith("-4"):
            index += 1
            continue
        fields = stripped.split()
        if len(fields) < 2:
            index += 1
            continue
        block_name = fields[1].upper()
        index += 1
        components: list[str] = []
        while index < len(lines) and lines[index].strip().startswith("-5"):
            component_fields = lines[index].strip().split()
            if len(component_fields) >= 2 and component_fields[1].upper() != "ALL":
                components.append(component_fields[1].upper())
            index += 1
        records: dict[int, list[float]] = {}
        while index < len(lines):
            current = lines[index].rstrip("\n")
            current_stripped = current.strip()
            if current_stripped == "-3" or current_stripped.startswith("-4") or current_stripped.startswith("100"):
                break
            if current_stripped.startswith("-1"):
                node_id, values = _parse_frd_primary_record(current)
                index += 1
                while index < len(lines) and lines[index].strip().startswith("-2"):
                    values.extend(_parse_frd_continuation(lines[index]))
                    index += 1
                records[node_id] = values[: len(components) or None]
                continue
            index += 1
        if block_name == "DISP" and records:
            displacement = records
        elif block_name == "STRESS" and records:
            stress = records
        if index < len(lines) and lines[index].strip() == "-3":
            index += 1
    return displacement, stress


def _parse_frd_primary_record(line: str) -> tuple[int, list[float]]:
    match = re.match(r"^\s*-1\s+(\d+)(.*)$", line)
    if match is None:
        raise ValueError(f"invalid FRD result record: {line!r}")
    node_id = int(match.group(1))
    return node_id, _parse_frd_value_tail(match.group(2))


def _parse_frd_continuation(line: str) -> list[float]:
    match = re.match(r"^\s*-2(.*)$", line)
    if match is None:
        raise ValueError(f"invalid FRD continuation record: {line!r}")
    return _parse_frd_value_tail(match.group(1))


def _parse_frd_value_tail(tail: str) -> list[float]:
    values: list[float] = []
    if not tail:
        return values
    padded = tail.rstrip()
    for offset in range(0, len(padded), 12):
        field = padded[offset : offset + 12].strip()
        if not field:
            continue
        try:
            values.append(float(field))
        except ValueError:
            match = re.fullmatch(r"(.+)([+-])(\d{3})", field)
            if match is None:
                raise ValueError(f"invalid FRD numeric field: {field!r}") from None
            values.append(float(f"{match.group(1)}e{match.group(2)}{match.group(3)}"))
    return values


def _parse_legacy_frd(text: str) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
    displacement: dict[int, list[float]] = {}
    stress: dict[int, list[float]] = {}
    for raw in text.splitlines():
        parts = raw.split()
        if not parts or parts[0].startswith("#"):
            continue
        tag = parts[0].upper()
        if tag == "DISP" and len(parts) == 5:
            displacement[int(parts[1])] = [float(value) for value in parts[2:5]]
        elif tag == "STRESS" and len(parts) >= 4:
            stress[int(parts[2])] = [float(parts[3]), 0.0, 0.0, 0.0, 0.0, 0.0]
    return displacement, stress


def _parse_reactions(dat_path: Path) -> list[dict[str, Any]]:
    reactions: list[dict[str, Any]] = []
    for raw in dat_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split()
        if parts and parts[0].upper() == "REACTION" and len(parts) == 5:
            reactions.append({"node_id": int(parts[1]), "force": [float(value) for value in parts[2:5]]})
    return reactions


def _von_mises(values: list[float]) -> float:
    if len(values) < 6:
        raise ValueError("stress record has fewer than six tensor components")
    s_xx, s_yy, s_zz, s_xy, s_yz, s_zx = values[:6]
    return math.sqrt(
        0.5 * ((s_xx - s_yy) ** 2 + (s_yy - s_zz) ** 2 + (s_zz - s_xx) ** 2)
        + 3.0 * (s_xy**2 + s_yz**2 + s_zx**2)
    )


def _parse_header(line: str) -> tuple[str, dict[str, str]]:
    parts = [part.strip() for part in line[1:].split(",")]
    header = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            params[key.strip().upper()] = value.strip()
    return header, params


def _integer_values(line: str) -> list[int]:
    values: list[int] = []
    for token in line.split(","):
        stripped = token.strip()
        if not stripped:
            continue
        try:
            values.append(int(stripped))
        except ValueError:
            if values:
                break
    return values


def _criteria_list(value: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    return [{"metric": metric, "op": "<=", "value": limit} for metric, limit in value.items()]


def _compare(actual: float, op: str, expected: float) -> bool:
    return {"<=": actual <= expected, "<": actual < expected, ">=": actual >= expected, ">": actual > expected, "==": actual == expected}[op]


def _require_node_set(mesh: dict[str, Any], name: str) -> None:
    if name not in mesh["node_sets"] or not mesh["node_sets"][name]:
        raise ValueError(f"mesh node set missing or empty: {name}")


def _require_element_set(mesh: dict[str, Any], name: str) -> None:
    if name not in mesh["element_sets"] or not mesh["element_sets"][name]:
        raise ValueError(f"mesh element set missing or empty: {name}")


def _artifact_map(paths: dict[str, Path]) -> dict[str, Any]:
    return {
        role: {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for role, path in paths.items()
        if path.exists()
    }


def _error(
    code: str,
    detail: dict[str, Any],
    artifacts: dict[str, Any],
    returncode: int | None = None,
    tool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_result_summary_v0_1",
        "verdict": "error",
        "errors": [{"code": code, "detail": detail}],
        "solver": {"tool_id": "calculix", "version": (tool or {}).get("version_pin"), "returncode": returncode},
        "artifacts": artifacts,
    }
