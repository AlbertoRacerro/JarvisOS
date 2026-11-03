"""BLUECAD v0 assembly placement and port conformity."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Any

from app.modules.bluecad.builders import build_part
from app.modules.bluecad.models import BluecadError, BuiltPart, PortFrame

REL_TOL = 1e-6
ABS_TOL = 1e-6


def assemble_parts(spec: dict[str, Any]) -> dict[str, BuiltPart]:
    local_parts = {part.part_id: part for part in (build_part(item) for item in spec["parts"])}
    part_specs = {part["part_id"]: part for part in spec["parts"]}
    connections = spec.get("connections", [])
    graph = _connection_graph(connections)
    placements: dict[str, tuple[float, tuple[float, float, float]]] = {}
    first_id = spec["parts"][0]["part_id"]
    placements[first_id] = _frame_placement(part_specs[first_id].get("frame"))
    queue: deque[str] = deque([first_id])
    while queue:
        known_id = queue.popleft()
        known_part = _placed(local_parts[known_id], placements[known_id])
        for connection, known_endpoint, candidate_endpoint in graph[known_id]:
            candidate_id, candidate_port_name = candidate_endpoint
            known_port = known_part.ports[known_endpoint[1]]
            candidate_local_port = local_parts[candidate_id].ports[candidate_port_name]
            candidate_placement = _placement_for_connection(candidate_local_port, known_port)
            if candidate_id in placements:
                _assert_consistent_placement(local_parts[candidate_id], placements[candidate_id], candidate_placement, connection)
                continue
            placements[candidate_id] = candidate_placement
            queue.append(candidate_id)
    for part in spec["parts"]:
        part_id = part["part_id"]
        if part_id not in placements:
            placements[part_id] = _frame_placement(part.get("frame"))
    placed = {part_id: _placed(local_parts[part_id], placement) for part_id, placement in placements.items()}
    for connection in connections:
        left_part_id, left_port_name = _split_endpoint(connection["from"])
        right_part_id, right_port_name = _split_endpoint(connection["to"])
        left = _port(placed, left_part_id, left_port_name)
        right = _port(placed, right_part_id, right_port_name)
        _assert_ports_conform(left, right, connection)
        _assert_ports_coincident_and_opposed(left, right, connection)
    return {part["part_id"]: placed[part["part_id"]] for part in spec["parts"]}


def _connection_graph(connections: list[dict[str, str]]) -> dict[str, list[tuple[dict[str, str], tuple[str, str], tuple[str, str]]]]:
    graph: dict[str, list[tuple[dict[str, str], tuple[str, str], tuple[str, str]]]] = defaultdict(list)
    for connection in connections:
        left = _split_endpoint(connection["from"])
        right = _split_endpoint(connection["to"])
        graph[left[0]].append((connection, left, right))
        graph[right[0]].append((connection, right, left))
    return graph


def _frame_placement(frame: dict[str, Any] | None) -> tuple[float, tuple[float, float, float]]:
    if frame is None:
        return (0.0, (0.0, 0.0, 0.0))
    direction = frame["direction"]
    angle = math.atan2(float(direction[1]), float(direction[0]))
    origin = tuple(float(value) for value in frame["origin"])
    return (angle, origin)


def _placement_for_connection(candidate_local_port: PortFrame, known_port: PortFrame) -> tuple[float, tuple[float, float, float]]:
    target_angle = math.atan2(-known_port.direction[1], -known_port.direction[0])
    local_angle = math.atan2(candidate_local_port.direction[1], candidate_local_port.direction[0])
    rotation = target_angle - local_angle
    rotated_origin = _rotate(candidate_local_port.origin, rotation)
    translation = tuple(known_port.origin[axis] - rotated_origin[axis] for axis in range(3))
    return (rotation, translation)


def _assert_consistent_placement(part: BuiltPart, existing: tuple[float, tuple[float, float, float]], candidate: tuple[float, tuple[float, float, float]], connection: dict[str, str]) -> None:
    existing_part = _placed(part, existing)
    candidate_part = _placed(part, candidate)
    for port_name in part.ports:
        if not _points_close(existing_part.ports[port_name].origin, candidate_part.ports[port_name].origin) or not _directions_close(existing_part.ports[port_name].direction, candidate_part.ports[port_name].direction):
            raise BluecadError("PORT_MISMATCH", {"connection": connection, "message": "conflicting assembly placement paths"})


def _placed(part: BuiltPart, placement: tuple[float, tuple[float, float, float]]) -> BuiltPart:
    rotation, translation = placement
    return part.placed(rotation, translation)


def _rotate(value: tuple[float, float, float], angle: float) -> tuple[float, float, float]:
    cos_t = math.cos(angle)
    sin_t = math.sin(angle)
    return (cos_t * value[0] - sin_t * value[1], sin_t * value[0] + cos_t * value[1], value[2])


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    part_id, port_name = endpoint.split(".", 1)
    return part_id, port_name


def _port(parts: dict[str, BuiltPart], part_id: str, port_name: str) -> PortFrame:
    try:
        return parts[part_id].ports[port_name]
    except KeyError as exc:
        raise BluecadError("PORT_MISMATCH", {"part_id": part_id, "port": port_name, "message": "unknown port"}) from exc


def _assert_ports_conform(left: PortFrame, right: PortFrame, connection: dict[str, str]) -> None:
    if left.interface != right.interface:
        raise BluecadError(
            "PORT_MISMATCH",
            {
                "connection": connection,
                "from": {"interface": left.interface},
                "to": {"interface": right.interface},
                "message": "connected ports must use matching interfaces",
            },
        )
    if left.interface == "tube":
        if not _rel_close(float(left.outer_d), float(right.outer_d)) or not _rel_close(float(left.wall_t), float(right.wall_t)):
            raise BluecadError(
                "PORT_MISMATCH",
                {
                    "connection": connection,
                    "from": {"interface": left.interface, "outer_d": left.outer_d, "wall_t": left.wall_t},
                    "to": {"interface": right.interface, "outer_d": right.outer_d, "wall_t": right.wall_t},
                    "message": "connected tube ports must have matching outer_d and wall_t",
                },
            )
        return
    if not _rel_close(float(left.pad_d), float(right.pad_d)):
        raise BluecadError(
            "PORT_MISMATCH",
            {
                "connection": connection,
                "from": {"interface": left.interface, "pad_d": left.pad_d},
                "to": {"interface": right.interface, "pad_d": right.pad_d},
                "message": "connected pad ports must have matching pad_d",
            },
        )


def _assert_ports_coincident_and_opposed(left: PortFrame, right: PortFrame, connection: dict[str, str]) -> None:
    if not _points_close(left.origin, right.origin) or not _directions_opposed(left.direction, right.direction):
        raise BluecadError("PORT_MISMATCH", {"connection": connection, "message": "connected ports must be coincident and opposed"})


def _rel_close(a: float, b: float) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=REL_TOL * scale)


def _points_close(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    return all(math.isclose(a[axis], b[axis], abs_tol=ABS_TOL) for axis in range(3))


def _directions_close(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    return all(math.isclose(a[axis], b[axis], abs_tol=ABS_TOL) for axis in range(3))


def _directions_opposed(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    return _directions_close(a, tuple(-value for value in b))
