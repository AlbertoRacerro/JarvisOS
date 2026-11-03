"""Component-aware FRD and coordinate-aware INP parsing for verification."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.modules.bluecad.fem_verification_common import FemVerificationError

STRESS_COMPONENTS = ("SXX", "SYY", "SZZ", "SXY", "SYZ", "SZX")
DISPLACEMENT_COMPONENTS = ("D1", "D2", "D3")


def parse_inp_mesh(text: str) -> dict[str, Any]:
    """Parse coordinates, connectivity and named sets from a CalculiX INP."""

    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, dict[str, Any]] = {}
    node_sets: dict[str, set[int]] = {}
    element_sets: dict[str, set[int]] = {}
    element_set_entries: dict[str, list[int]] = {}
    section: str | None = None
    active_set: str | None = None
    active_type: str | None = None
    generated = False

    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("**") or line.startswith("*******"):
            continue
        if line.startswith("*"):
            header, params, flags = _parse_inp_header(line)
            section = None
            active_set = None
            active_type = None
            generated = "GENERATE" in flags
            if header == "NODE":
                section = "node"
            elif header == "ELEMENT":
                section = "element"
                active_type = params.get("TYPE", "").upper()
                active_set = params.get("ELSET")
                if not active_type:
                    raise FemVerificationError(
                        "INP_ELEMENT_TYPE_MISSING", {"line": line_number}
                    )
                if active_set:
                    element_sets.setdefault(active_set, set())
                    element_set_entries.setdefault(active_set, [])
            elif header == "NSET":
                section = "nset"
                active_set = params.get("NSET")
                if not active_set:
                    raise FemVerificationError(
                        "INP_SET_NAME_MISSING",
                        {"line": line_number, "header": header},
                    )
                node_sets.setdefault(active_set, set())
            elif header == "ELSET":
                section = "elset"
                active_set = params.get("ELSET")
                if not active_set:
                    raise FemVerificationError(
                        "INP_SET_NAME_MISSING",
                        {"line": line_number, "header": header},
                    )
                element_sets.setdefault(active_set, set())
                element_set_entries.setdefault(active_set, [])
            continue

        if section == "node":
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                raise FemVerificationError(
                    "INP_NODE_RECORD_INVALID",
                    {"line": line_number, "record": line},
                )
            try:
                node_id = int(parts[0])
                coordinate_values = [float(value) for value in parts[1:4]]
            except ValueError as exc:
                raise FemVerificationError(
                    "INP_NODE_RECORD_INVALID",
                    {"line": line_number, "record": line},
                ) from exc
            coordinates = (
                coordinate_values[0],
                coordinate_values[1],
                coordinate_values[2],
            )
            if node_id in nodes:
                raise FemVerificationError("INP_DUPLICATE_NODE", {"node_id": node_id})
            if not all(math.isfinite(value) for value in coordinates):
                raise FemVerificationError(
                    "INP_NODE_COORDINATE_INVALID", {"node_id": node_id}
                )
            nodes[node_id] = coordinates
            continue

        if section is None:
            continue
        values = _integer_values(line, line_number)
        if section == "element":
            if len(values) < 2:
                raise FemVerificationError(
                    "INP_ELEMENT_RECORD_INVALID",
                    {"line": line_number, "record": line},
                )
            element_id = values[0]
            if element_id in elements:
                raise FemVerificationError(
                    "INP_DUPLICATE_ELEMENT", {"element_id": element_id}
                )
            elements[element_id] = {
                "type": active_type or "",
                "nodes": values[1:],
            }
            if active_set:
                element_sets[active_set].add(element_id)
                element_set_entries[active_set].append(element_id)
        elif section in {"nset", "elset"} and active_set:
            expanded = _expand_generated(values, line_number) if generated else values
            if section == "nset":
                node_sets[active_set].update(expanded)
            else:
                element_sets[active_set].update(expanded)
                element_set_entries[active_set].extend(expanded)

    if not nodes:
        raise FemVerificationError("INP_NODES_MISSING", {})
    if not elements:
        raise FemVerificationError("INP_ELEMENTS_MISSING", {})
    missing_nodes = sorted(
        {
            node_id
            for element in elements.values()
            for node_id in element["nodes"]
            if node_id not in nodes
        }
    )
    if missing_nodes:
        raise FemVerificationError(
            "INP_CONNECTIVITY_NODE_MISSING", {"node_ids": missing_nodes}
        )
    return {
        "node_coordinates": nodes,
        "elements": elements,
        "node_sets": node_sets,
        "element_sets": element_sets,
        "element_set_entries": element_set_entries,
    }


def parse_frd_blocks(text: str) -> list[dict[str, Any]]:
    """Parse native CalculiX FRD blocks while retaining component names."""

    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith("-4"):
            index += 1
            continue
        fields = stripped.split()
        if len(fields) < 2:
            raise FemVerificationError("FRD_BLOCK_HEADER_INVALID", {"line": index + 1})
        block_name = fields[1].upper()
        index += 1
        components: list[str] = []
        while index < len(lines) and lines[index].strip().startswith("-5"):
            component_fields = lines[index].strip().split()
            if len(component_fields) < 2:
                raise FemVerificationError(
                    "FRD_COMPONENT_HEADER_INVALID", {"line": index + 1}
                )
            component = component_fields[1].upper()
            if component != "ALL":
                if component in components:
                    raise FemVerificationError(
                        "FRD_DUPLICATE_COMPONENT",
                        {"block": block_name, "component": component},
                    )
                components.append(component)
            index += 1
        if not components:
            raise FemVerificationError("FRD_COMPONENTS_MISSING", {"block": block_name})

        records: dict[int, dict[str, float]] = {}
        terminated = False
        while index < len(lines):
            current = lines[index].rstrip("\n")
            current_stripped = current.strip()
            if current_stripped == "-3":
                terminated = True
                index += 1
                break
            if current_stripped.startswith("-4") or current_stripped.startswith("100"):
                break
            if current_stripped.startswith("-1"):
                node_id, values = _parse_frd_primary_record(current, index + 1)
                index += 1
                while index < len(lines) and lines[index].strip().startswith("-2"):
                    values.extend(_parse_frd_continuation(lines[index], index + 1))
                    index += 1
                if node_id in records:
                    raise FemVerificationError(
                        "FRD_DUPLICATE_NODE_RECORD",
                        {"block": block_name, "node_id": node_id},
                    )
                if len(values) != len(components):
                    raise FemVerificationError(
                        "FRD_COMPONENT_VALUE_COUNT_MISMATCH",
                        {
                            "block": block_name,
                            "node_id": node_id,
                            "component_count": len(components),
                            "value_count": len(values),
                        },
                    )
                if not all(math.isfinite(value) for value in values):
                    raise FemVerificationError(
                        "FRD_NONFINITE_VALUE",
                        {"block": block_name, "node_id": node_id},
                    )
                records[node_id] = dict(zip(components, values, strict=True))
                continue
            index += 1
        if not terminated:
            raise FemVerificationError("FRD_BLOCK_UNTERMINATED", {"block": block_name})
        if not records:
            raise FemVerificationError("FRD_RECORDS_MISSING", {"block": block_name})
        blocks.append(
            {
                "name": block_name,
                "components": tuple(components),
                "records": records,
            }
        )
    if not blocks:
        raise FemVerificationError("FRD_BLOCKS_MISSING", {})
    return blocks


def latest_frd_block(
    blocks: Sequence[Mapping[str, Any]],
    name: str,
    *,
    required_components: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return the latest named block and validate its exact component set."""

    target = name.upper()
    matches = [
        block for block in blocks if str(block.get("name", "")).upper() == target
    ]
    if not matches:
        raise FemVerificationError("FRD_REQUIRED_BLOCK_MISSING", {"block": target})
    block = dict(matches[-1])
    if required_components is not None:
        required = tuple(component.upper() for component in required_components)
        actual = tuple(str(value).upper() for value in block.get("components", ()))
        missing = sorted(set(required) - set(actual))
        unsupported = sorted(set(actual) - set(required))
        if missing or unsupported or len(actual) != len(required):
            raise FemVerificationError(
                "FRD_COMPONENT_SET_INVALID",
                {
                    "block": target,
                    "missing": missing,
                    "unsupported": unsupported,
                    "actual": list(actual),
                },
            )
    return block


def stress_block(blocks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the latest exact six-component Cartesian stress block."""

    return latest_frd_block(blocks, "STRESS", required_components=STRESS_COMPONENTS)


def displacement_block(blocks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the latest exact three-component displacement block."""

    return latest_frd_block(blocks, "DISP", required_components=DISPLACEMENT_COMPONENTS)


def _parse_inp_header(line: str) -> tuple[str, dict[str, str], set[str]]:
    parts = [part.strip() for part in line[1:].split(",")]
    header = parts[0].upper()
    params: dict[str, str] = {}
    flags: set[str] = set()
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            params[key.strip().upper()] = value.strip()
        elif part:
            flags.add(part.upper())
    return header, params, flags


def _integer_values(line: str, line_number: int) -> list[int]:
    values: list[int] = []
    for token in line.split(","):
        stripped = token.strip()
        if not stripped:
            continue
        try:
            values.append(int(stripped))
        except ValueError as exc:
            raise FemVerificationError(
                "INP_INTEGER_RECORD_INVALID",
                {"line": line_number, "token": stripped},
            ) from exc
    return values


def _expand_generated(values: Sequence[int], line_number: int) -> list[int]:
    if len(values) != 3 or values[2] <= 0 or values[1] < values[0]:
        raise FemVerificationError(
            "INP_GENERATE_RECORD_INVALID",
            {"line": line_number, "values": list(values)},
        )
    return list(range(values[0], values[1] + 1, values[2]))


def _parse_frd_primary_record(line: str, line_number: int) -> tuple[int, list[float]]:
    match = re.match(r"^\s*-1\s+(\d+)(.*)$", line)
    if match is None:
        raise FemVerificationError("FRD_PRIMARY_RECORD_INVALID", {"line": line_number})
    return int(match.group(1)), _parse_frd_value_tail(match.group(2), line_number)


def _parse_frd_continuation(line: str, line_number: int) -> list[float]:
    match = re.match(r"^\s*-2(.*)$", line)
    if match is None:
        raise FemVerificationError("FRD_CONTINUATION_INVALID", {"line": line_number})
    return _parse_frd_value_tail(match.group(1), line_number)


def _parse_frd_value_tail(tail: str, line_number: int) -> list[float]:
    values: list[float] = []
    padded = tail.rstrip()
    for offset in range(0, len(padded), 12):
        field = padded[offset : offset + 12].strip()
        if not field:
            continue
        normalized = field.replace("D", "E").replace("d", "e")
        try:
            values.append(float(normalized))
            continue
        except ValueError:
            match = re.fullmatch(r"(.+)([+-])(\d{3})", normalized)
            if match is None:
                raise FemVerificationError(
                    "FRD_NUMERIC_FIELD_INVALID",
                    {"line": line_number, "field": field},
                ) from None
            values.append(float(f"{match.group(1)}e{match.group(2)}{match.group(3)}"))
    return values
