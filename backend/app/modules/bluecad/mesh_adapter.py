"""BLUECAD Gmsh mesh adapter using only the subprocess tool registry."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.modules.bluecad.export import sha256_file
from app.modules.bluecad.registry import ToolRegistryError, run_tool

_LABEL_RE = re.compile(r"^[A-Za-z0-9_]+$")
_VOLUME_ELEMENT_PREFIXES = ("C3D", "DC3D")
_HIGH_ORDER_INVALID_LOG_PATTERNS = (
    re.compile(
        r"\b(?P<count>\d+)\s+elements?\s+with\s+jac\.?\s*<\s*0\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:(?P<count>\d+)\s+)?negative(?:\s+|-)?jacobians?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:(?P<count>\d+)\s+)?inverted\s+(?:high[- ]order\s+)?elements?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:(?P<count>\d+)\s+)?elements?\s+(?:(?:is|are)\s+)?(?:completely\s+)?inverted\b",
        re.IGNORECASE,
    ),
)


def mesh_analysis_spec(
    analysis_spec: dict[str, Any],
    out_dir: str | Path,
    *,
    registry_path: str | Path | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Mesh an AnalysisSpec geometry with Gmsh and return a MeshResult payload."""
    out_path = Path(out_dir)
    geometry = analysis_spec["geometry"]
    step_path = Path(geometry["step_path"])
    manifest_path = Path(geometry["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    refs = _referenced_groups(analysis_spec)
    _validate_labels(refs)
    mesh_config = analysis_spec["mesh"]
    target_size = float(mesh_config["target_size"])
    element_order = _element_order(mesh_config)

    attempts: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}
    final_errors: list[dict[str, Any]] = []
    for attempt_no, size in enumerate((target_size, target_size * 0.5), start=1):
        attempt_dir = out_path if attempt_no == 1 else out_path / f"retry_{attempt_no}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        geo_path = attempt_dir / "mesh.geo"
        inp_path = attempt_dir / "mesh.inp"
        msh_path = attempt_dir / "mesh.msh"
        log_path = attempt_dir / "gmsh.log"
        geo_path.write_text(
            _geo_text(step_path, manifest, refs, size, mesh_config.get("refinements", {})),
            encoding="utf-8",
        )
        try:
            run = run_tool(
                "gmsh",
                _gmsh_args(geo_path, inp_path, element_order),
                attempt_dir,
                timeout_s,
                registry_path,
            )
        except ToolRegistryError as exc:
            final_errors = [{"code": exc.code, "detail": exc.detail or {"message": exc.message}}]
            attempts.append(_attempt(attempt_no, size, geo_path, inp_path, msh_path, log_path, None, final_errors))
            return _result("error", final_errors, attempts, _artifacts(attempt_dir, artifacts))
        log_path.write_text((run.stdout or "") + (run.stderr or ""), encoding="utf-8")
        if not msh_path.exists():
            msh_path.write_text("", encoding="utf-8")
        errors, counts, warnings = _post_check(run.returncode, inp_path, refs, log_path, element_order)
        attempt = _attempt(attempt_no, size, geo_path, inp_path, msh_path, log_path, run.returncode, errors, counts, warnings)
        attempts.append(attempt)
        artifacts = _artifacts(attempt_dir, artifacts)
        if not errors:
            return _result("pass", [], attempts, artifacts)
        final_errors = errors
        if not any(error["code"] == "MESH_FAIL" for error in errors):
            break
        if attempt_no == 2:
            break
    return _result("fail", final_errors, attempts, artifacts)


def _element_order(mesh_config: dict[str, Any]) -> int:
    value = mesh_config.get("element_order", 1)
    if type(value) is not int or value not in (1, 2):
        raise ValueError("mesh.element_order must be integer 1 or 2")
    return value


def _gmsh_args(geo_path: Path, inp_path: Path, element_order: int) -> list[str]:
    args = ["-3", str(geo_path)]
    if element_order == 2:
        args.extend(["-order", "2"])
    args.extend(["-format", "inp", "-o", str(inp_path), "-save_all"])
    return args


def _referenced_groups(spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    groups = [("BC", item["port_label"], "nodes") for item in spec.get("bcs", [])]
    for item in spec.get("loads", []):
        groups.append(("LOAD", item["port_label"], "elements" if item.get("type") == "pressure" else "nodes"))
    return groups


def _validate_labels(refs: list[tuple[str, str, str]]) -> None:
    for _, label, _ in refs:
        if not all(_LABEL_RE.fullmatch(part) for part in label.split(".")):
            raise ValueError(f"Unsafe BLUECAD port label: {label!r}")


def _group_label(label: str) -> str:
    return label.replace(".", "_")


def _port(manifest: dict[str, Any], label: str) -> dict[str, Any]:
    if "." not in label:
        raise ValueError(f"Port label must be '<part>.<port>': {label}")
    part_id, port_id = label.split(".", 1)
    return manifest["resolved_ports"][part_id][port_id]


def _geo_text(
    step_path: Path,
    manifest: dict[str, Any],
    refs: list[tuple[str, str, str]],
    target_size: float,
    refinements: dict[str, Any],
) -> str:
    has_pressure = any(group_kind == "elements" for _, _, group_kind in refs)
    lines = [
        f'Merge "{step_path.as_posix()}";',
        f"Mesh.CharacteristicLengthMax = {target_size:.9g};",
        f"Mesh.CharacteristicLengthMin = {target_size:.9g};",
        f"Mesh.SaveGroupsOfElements = {1 if has_pressure else -1000};",
        "Mesh.SaveGroupsOfNodes = -100;",
    ]
    for prefix, label, _ in refs:
        port = _port(manifest, label)
        origin = [float(v) for v in port["origin"]]
        half = 0.75 * float(port.get("outer_d") or port.get("pad_d"))
        box = [
            origin[0] - half,
            origin[1] - half,
            origin[2] - half,
            origin[0] + half,
            origin[1] + half,
            origin[2] + half,
        ]
        lines.append(
            f'Physical Surface("{prefix}_{_group_label(label)}") = '
            f'Surface In BoundingBox {{{", ".join(f"{v:.9g}" for v in box)}}};'
        )
        if label in refinements:
            lines.append(f"// refinement {label} {float(refinements[label]):.9g}")
    lines.extend(['Physical Volume("BODY") = Volume{:};', "Mesh 3;", 'Save "mesh.msh";'])
    return "\n".join(lines) + "\n"


def _post_check(
    returncode: int,
    inp_path: Path,
    refs: list[tuple[str, str, str]],
    log_path: Path,
    element_order: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    log_lines = log_path.read_text(encoding="utf-8").splitlines()
    warnings = [line for line in log_lines if "warning" in line.lower() or "quality" in line.lower()]
    if returncode != 0:
        return ([{"code": "TIMEOUT" if returncode == 124 else "MESH_FAIL", "detail": {"returncode": returncode}}], {}, warnings)
    try:
        text = inp_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ([{"code": "PARSE_ERROR", "detail": {"message": str(exc)}}], {}, warnings)
    try:
        counts = _parse_inp_counts(text, refs)
    except ValueError as exc:
        return ([{"code": "PARSE_ERROR", "detail": {"message": str(exc)}}], {}, warnings)
    errors: list[dict[str, Any]] = []
    if counts["elements_total"] <= 0:
        errors.append({"code": "MESH_FAIL", "detail": {"message": "zero volume elements"}})
    if element_order == 2 and set(counts["volume_element_types"]) != {"C3D10"}:
        errors.append(
            {
                "code": "MESH_ELEMENT_ORDER_MISMATCH",
                "detail": {
                    "requested_order": 2,
                    "expected_volume_type": "C3D10",
                    "actual_volume_types": counts["volume_element_types"],
                },
            }
        )
    if element_order == 2:
        high_order_error = _high_order_invalid_error(log_lines)
        if high_order_error is not None:
            errors.append(high_order_error)
    if counts["physical_groups"].get("BODY", 0) <= 0:
        errors.append({"code": "MESH_GROUP_EMPTY", "detail": {"group": "BODY"}})
    for prefix, label, _ in refs:
        name = f"{prefix}_{_group_label(label)}"
        if counts["physical_groups"].get(name, 0) <= 0:
            errors.append({"code": "MESH_GROUP_EMPTY", "detail": {"group": name}})
    return errors, counts, warnings


def _high_order_invalid_error(log_lines: list[str]) -> dict[str, Any] | None:
    diagnostics: list[str] = []
    reported_invalid_elements = 0
    for line in log_lines:
        for pattern in _HIGH_ORDER_INVALID_LOG_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            count_text = match.groupdict().get("count")
            if count_text is not None:
                count = int(count_text)
                if count <= 0:
                    break
                reported_invalid_elements = max(reported_invalid_elements, count)
            diagnostics.append(line)
            break
    if not diagnostics:
        return None
    detail: dict[str, Any] = {"requested_order": 2, "diagnostics": diagnostics}
    if reported_invalid_elements > 0:
        detail["reported_invalid_elements"] = reported_invalid_elements
    return {"code": "MESH_HIGH_ORDER_INVALID", "detail": detail}


def _parse_inp_counts(text: str, refs: list[tuple[str, str, str]]) -> dict[str, Any]:
    nodes: set[int] = set()
    volume_elements: set[int] = set()
    volume_element_types: dict[str, int] = {}
    inline_element_sets: dict[str, set[int]] = {}
    explicit_element_sets: dict[str, set[int]] = {}
    node_sets: dict[str, set[int]] = {}
    section: str | None = None
    active_group: str | None = None
    active_element_type: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("**") or line.startswith("*******"):
            continue
        if line.startswith("*"):
            header, params = _parse_header(line)
            section = None
            active_group = None
            active_element_type = None
            if header == "NODE":
                section = "node"
            elif header == "ELEMENT":
                section = "element"
                active_element_type = params.get("TYPE", "").upper()
                active_group = params.get("ELSET")
                if active_group:
                    inline_element_sets.setdefault(active_group, set())
            elif header == "ELSET":
                section = "elset"
                active_group = params.get("ELSET")
                if not active_group:
                    raise ValueError("ELSET section missing ELSET name")
                explicit_element_sets.setdefault(active_group, set())
            elif header == "NSET":
                section = "nset"
                active_group = params.get("NSET")
                if not active_group:
                    raise ValueError("NSET section missing NSET name")
                node_sets.setdefault(active_group, set())
            continue

        values = _integer_values(line)
        if not values:
            continue
        if section == "node":
            nodes.add(values[0])
        elif section == "element":
            element_id = values[0]
            if active_group:
                inline_element_sets[active_group].add(element_id)
            if active_element_type and active_element_type.startswith(_VOLUME_ELEMENT_PREFIXES):
                volume_elements.add(element_id)
                volume_element_types[active_element_type] = volume_element_types.get(active_element_type, 0) + 1
        elif section == "elset" and active_group:
            explicit_element_sets[active_group].update(values)
        elif section == "nset" and active_group:
            node_sets[active_group].update(values)

    physical_groups: dict[str, int] = {}
    expected = {"BODY"} | {f"{prefix}_{_group_label(label)}" for prefix, label, _ in refs}
    for name in expected:
        members = set()
        members.update(inline_element_sets.get(name, set()))
        members.update(explicit_element_sets.get(name, set()))
        members.update(node_sets.get(name, set()))
        physical_groups[name] = len(members)
    return {
        "nodes_total": len(nodes),
        "elements_total": len(volume_elements),
        "volume_element_types": dict(sorted(volume_element_types.items())),
        "physical_groups": physical_groups,
    }


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


def _attempt(
    attempt_no: int,
    size: float,
    geo_path: Path,
    inp_path: Path,
    msh_path: Path,
    log_path: Path,
    returncode: int | None,
    errors: list[dict[str, Any]],
    counts: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "attempt_no": attempt_no,
        "target_size": size,
        "gmsh_returncode": returncode,
        "counts": counts or {},
        "warnings": warnings or [],
        "errors": errors,
        "artifacts": _artifact_map(
            {"bluecad_geo": geo_path, "mesh_inp": inp_path, "mesh_msh": msh_path, "gmsh_log": log_path}
        ),
    }


def _artifact_map(paths: dict[str, Path]) -> dict[str, Any]:
    return {
        role: {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for role, path in paths.items()
        if path.exists()
    }


def _artifacts(attempt_dir: Path, previous: dict[str, Any]) -> dict[str, Any]:
    current = _artifact_map(
        {
            "bluecad_geo": attempt_dir / "mesh.geo",
            "mesh_inp": attempt_dir / "mesh.inp",
            "mesh_msh": attempt_dir / "mesh.msh",
            "gmsh_log": attempt_dir / "gmsh.log",
        }
    )
    return current or previous


def _result(verdict: str, errors: list[dict[str, Any]], attempts: list[dict[str, Any]], artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_mesh_result_v0_1",
        "verdict": verdict,
        "errors": errors,
        "attempts": attempts,
        "artifacts": artifacts,
    }
