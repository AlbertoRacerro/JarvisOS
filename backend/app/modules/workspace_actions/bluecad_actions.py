"""BLUECAD GeometrySpec transforms and child candidate derivation."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.bluecad.candidate_build import build_geometry_candidate
from app.modules.bluecad.ledger import (
    brief_digest,
    get_candidate,
    park_candidate,
)
from app.modules.bluecad.spec import canonical_json, canonicalize_geometry_spec
from app.modules.events.service import utc_now
from app.modules.workspace_actions.models import ActionOrigin, ActionOutcome, ActionRequest, ChangeLine

_LENGTH_PARAMS = frozenset(
    {
        "outer_d",
        "wall_t",
        "length",
        "bend_radius",
        "socket_len",
        "outer_d_main",
        "out_d",
        "out_wall_t",
        "spacing",
        "main_outer_d",
        "main_wall_t",
        "branch_outer_d",
        "branch_wall_t",
        "branch_gap",
        "end_gap",
        "cap_thickness",
        "branch_stub_length",
        "base_w",
        "base_l",
        "base_t",
        "eye_d",
        "pad_d",
        "height",
        "port_d",
    }
)
_INTEGER_PARAMS = frozenset({"n_out", "n_mounts", "branch_count"})
_ANGLE_PARAMS = frozenset({"angle"})


def _base_spec(workspace_id: str, candidate_id: str) -> dict:
    candidate = get_candidate(workspace_id, candidate_id)
    if candidate is None or candidate.status != "valid" or not candidate.spec_artifact_id:
        raise ValueError("A valid base candidate with a GeometrySpec artifact is required.")
    with open_sqlite_connection() as connection:
        artifact = connection.execute(
            "SELECT stored_path FROM artifacts WHERE id=? AND workspace_id=? AND status='registered'",
            (candidate.spec_artifact_id, workspace_id),
        ).fetchone()
    if artifact is None:
        raise ValueError("Base candidate GeometrySpec artifact is unavailable.")
    return canonicalize_geometry_spec(json.loads(Path(artifact["stored_path"]).read_text(encoding="utf-8")))


def _part(spec: dict, part_id: str) -> dict:
    result = next((part for part in spec["parts"] if part["part_id"] == part_id), None)
    if result is None:
        raise ValueError(f"Unknown part {part_id!r} in the base candidate.")
    return result


def _unit(value: float, unit: str) -> float:
    return value * {"mm": 1.0, "cm": 10.0, "m": 1000.0}.get(unit, 1.0)


def _outer_extent(params: dict) -> float:
    for key in ("outer_d", "outer_d_main", "main_outer_d", "base_w", "eye_d"):
        if key in params:
            return float(params[key])
    return max((float(value) for key, value in params.items() if key in {"base_l", "length", "height"}), default=0.0)


def _transform(spec: dict, request: ActionRequest) -> tuple[dict, list[ChangeLine]]:
    result = json.loads(canonical_json(spec))
    lines: list[ChangeLine] = []
    for action in request.actions:
        if action.op not in {"duplicate_part", "set_part_param", "move_part", "delete_part"}:
            raise ValueError("Process actions cannot execute on the BLUECAD surface.")
        part = _part(result, action.part)
        params = part["params"]
        if action.op == "duplicate_part":
            suffix = 2
            while any(item["part_id"] == f"{part['part_id']}_{suffix}" for item in result["parts"]):
                suffix += 1
            clone = json.loads(canonical_json(part))
            clone["part_id"] = f"{part['part_id']}_{suffix}"
            frame = clone.setdefault("frame", {"origin": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0]})
            origin = frame.get("origin", [0.0, 0.0, 0.0])
            direction = frame.get("direction", [1.0, 0.0, 0.0])
            extent = _outer_extent(params)
            length = float(params.get("length", extent))
            gap = action.gap_mm if action.gap_mm is not None else extent * 0.5 if action.placement == "beside" else 0.0
            if action.placement == "above":
                offset = [0.0, 0.0, extent + gap]
            elif action.placement == "along":
                norm = math.sqrt(sum(float(x) ** 2 for x in direction)) or 1.0
                offset = [float(x) / norm * (length + gap) for x in direction]
            else:
                hx, hy = float(direction[0]), float(direction[1])
                norm = math.hypot(hx, hy)
                perp = [0.0, 1.0] if norm == 0 else [-hy / norm, hx / norm]
                offset = [perp[0] * (extent + gap), perp[1] * (extent + gap), 0.0]
            frame["origin"] = [float(origin[i]) + offset[i] for i in range(3)]
            result["parts"].append(clone)
            wall = params.get("wall_t")
            description = f"Ø{extent:g}×{length:g} mm" + (f", wall {float(wall):g} mm" if wall is not None else "")
            lines.append(ChangeLine(label=f"{clone['part_id']} — new {clone['kind']}", after=description))
        elif action.op == "set_part_param":
            if action.param not in params or not isinstance(params[action.param], (int, float)):
                raise ValueError(f"{part['kind']} supports parameters: {', '.join(sorted(params))}.")
            before = params[action.param]
            value = float(action.value)
            if action.param in _LENGTH_PARAMS:
                if action.unit not in {"mm", "cm", "m"}:
                    raise ValueError(f"Parameter {action.param!r} requires mm, cm, or m.")
                value = _unit(value, action.unit)
            elif action.param in _ANGLE_PARAMS:
                if action.unit != "deg":
                    raise ValueError(f"Parameter {action.param!r} requires deg.")
            elif action.param in _INTEGER_PARAMS:
                if action.unit != "unitless" or not value.is_integer():
                    raise ValueError(f"Parameter {action.param!r} requires a whole unitless value.")
                value = int(value)
            elif action.unit != "unitless":
                raise ValueError(f"Parameter {action.param!r} requires unitless.")
            params[action.param] = value
            unit = "deg" if action.param in _ANGLE_PARAMS else "mm" if action.param in _LENGTH_PARAMS else "unitless"
            lines.append(
                ChangeLine(
                    label=f"{part['part_id']} {action.param}", before=f"{before:g} {unit}", after=f"{value:g} {unit}"
                )
            )
        elif action.op == "move_part":
            frame = part.setdefault("frame", {"origin": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0]})
            origin = frame.get("origin", [0.0, 0.0, 0.0])
            delta = [_unit(action.dx, action.unit), _unit(action.dy, action.unit), _unit(action.dz, action.unit)]
            frame["origin"] = [float(origin[i]) + delta[i] for i in range(3)]
            lines.append(ChangeLine(label=f"{part['part_id']} position", after=f"{frame['origin']} mm"))
        elif action.op == "delete_part":
            if len(result["parts"]) <= 1:
                raise ValueError("The last part cannot be deleted.")
            result["parts"] = [item for item in result["parts"] if item["part_id"] != action.part]
            result["connections"] = [
                item
                for item in result.get("connections", [])
                if not any(str(item.get(end, "")).startswith(action.part + ".") for end in ("from", "to"))
            ]
            lines.append(ChangeLine(label=f"{action.part} — delete {part['kind']}", before="present", after="deleted"))
    return canonicalize_geometry_spec(result), lines


def apply_bluecad_request(
    workspace_id: str, request: ActionRequest, origin: ActionOrigin, digest: str
) -> ActionOutcome:
    spec = _base_spec(workspace_id, request.base_revision)
    transformed, lines = _transform(spec, request)
    summaries = []
    for action in request.actions:
        if action.op == "duplicate_part":
            part_kind = _part(spec, action.part)["kind"]
            description = f"Duplicate {part_kind} {action.placement} itself (new {part_kind} part"
            if action.gap_mm is not None:
                description += f", {action.gap_mm:g} mm gap"
            summaries.append(description + ")")
        elif action.op == "set_part_param":
            part_kind = _part(spec, action.part)["kind"]
            summaries.append(f"Set {part_kind} {action.param} to {action.value:g} {action.unit}")
        elif action.op == "move_part":
            part_kind = _part(spec, action.part)["kind"]
            summaries.append(f"Move {part_kind} by ({action.dx:g}, {action.dy:g}, {action.dz:g}) {action.unit}")
        elif action.op == "delete_part":
            summaries.append(f"Delete {_part(spec, action.part)['kind']} part")
    tier: Literal["immediate", "confirm"] = "confirm" if origin.kind == "relay" else "immediate"
    now = utc_now()
    outcome = ActionOutcome(
        action_id=str(uuid4()),
        workspace_id=workspace_id,
        surface="bluecad",
        state="proposed" if tier == "confirm" else "applied",
        tier=tier,
        summary="; ".join(summaries)[:400] or "BLUECAD action",
        changes=lines,
        base_revision=request.base_revision,
        candidate_id=request.base_revision,
        origin=origin,
        request_digest=digest,
        request=request.model_dump(mode="json"),
        undo_available=tier == "immediate",
        created_at=now,
        updated_at=now,
    )
    if tier == "immediate":
        outcome.child_candidate_id = _create_candidate(workspace_id, request.base_revision, transformed, origin, digest)
    return outcome


def execute_bluecad(workspace_id: str, request: ActionRequest, origin: ActionOrigin, digest: str) -> str:
    spec = _base_spec(workspace_id, request.base_revision)
    transformed, _ = _transform(spec, request)
    return _create_candidate(workspace_id, request.base_revision, transformed, origin, digest)


def _create_candidate(workspace_id: str, parent_id: str, spec: dict, origin: ActionOrigin, digest: str) -> str:
    child_id, attempt_id = str(uuid4()), str(uuid4())
    now = utc_now()
    brief = f"Agent-derived BLUECAD child of {parent_id}; action request {digest}."
    route_class = "agent:relay" if origin.kind == "relay" else "agent:local"
    with open_sqlite_connection() as connection:
        connection.execute(
            """INSERT INTO bluecad_candidates
            (id,workspace_id,brief_text,brief_digest,status,origin,parent_candidate_id,loop_config_json,created_at,updated_at,notes)
            VALUES(?,?,?,?, 'generating','agent_action',?,'{}',?,?,?)""",
            (
                child_id,
                workspace_id,
                brief,
                brief_digest(brief),
                parent_id,
                now,
                now,
                f"actor={origin.kind}; request_digest={digest}",
            ),
        )
        connection.execute(
            """INSERT INTO bluecad_attempts
            (id,candidate_id,attempt_no,route_class,proposal_outcome,started_at,error_detail_json)
            VALUES(?,?,1,?,'not_applicable',?,?)""",
            (attempt_id, child_id, route_class, now, json.dumps({"action_request_digest": digest})),
        )
        connection.commit()
    try:
        passed = build_geometry_candidate(
            workspace_id,
            child_id,
            attempt_id,
            spec,
            producer_notes="Generated by governed workspace action.",
            failure_reason="agent_action_failed",
        )
        if not passed:
            raise ValueError("Derived GeometrySpec failed deterministic validation.")
        return child_id
    except Exception:
        park_candidate(child_id, "agent_action_failed", notes="agent_action_build_failed")
        raise
