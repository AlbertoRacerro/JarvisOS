"""GeometrySpec v0.1 loading, validation, and canonicalization utilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from app.modules.bluecad.capped_manifold import PARAM_NAMES as _ALLOWED_CAPPED_MANIFOLD_PARAMS

GEOMETRY_SPEC_VERSION = "bluecad_geometry_spec_v0_1"
SPEC_ID_PREFIX = "sha256:"
SUPPORTED_PART_KINDS = frozenset(
    {
        "tube_run",
        "bend",
        "joint",
        "manifold",
        "capped_manifold",
        "float",
        "anchor_mount",
        "harvest_module",
    }
)
_ALLOWED_TOP_LEVEL_KEYS = frozenset({"spec_version", "spec_id", "name", "parts", "connections", "declared"})
_ALLOWED_PART_KEYS = frozenset({"part_id", "kind", "params", "frame"})
_ALLOWED_FRAME_KEYS = frozenset({"origin", "direction"})
_ALLOWED_CONNECTION_KEYS = frozenset({"from", "to"})
_ALLOWED_DECLARED_KEYS = frozenset({"total_volume_mm3", "bbox_mm", "min_wall_t"})
_ALLOWED_TOTAL_VOLUME_KEYS = frozenset({"value", "rel_tol"})
_ALLOWED_BBOX_KEYS = frozenset({"min", "max", "abs_tol"})
_ALLOWED_TUBE_PARAMS = frozenset({"outer_d", "wall_t", "length"})
_ALLOWED_BEND_PARAMS = frozenset({"outer_d", "wall_t", "bend_radius", "angle"})
_ALLOWED_JOINT_PARAMS = frozenset({"joint_type", "outer_d", "wall_t", "socket_len"})
_ALLOWED_MANIFOLD_PARAMS = frozenset({"outer_d_main", "wall_t", "length", "n_out", "out_d", "out_wall_t", "spacing"})
_ALLOWED_FLOAT_PARAMS = frozenset({"outer_d", "length", "n_mounts", "pad_d"})
_ALLOWED_ANCHOR_MOUNT_PARAMS = frozenset({"base_w", "base_l", "base_t", "eye_d"})
_ALLOWED_HARVEST_MODULE_PARAMS = frozenset({"outer_d", "height", "wall_t", "port_d"})


@dataclass(frozen=True)
class SpecValidationError(ValueError):
    """Structured validation failure raised before any CAD-kernel call."""

    detail: dict[str, Any]
    code: str = "SPEC_INVALID"

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def load_geometry_spec(path: str | Path) -> dict[str, Any]:
    """Load, validate, canonicalize, and stamp a GeometrySpec JSON file."""

    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SpecValidationError({"path": str(path), "message": "GeometrySpec must be valid JSON."}) from exc
    return canonicalize_geometry_spec(payload)


def canonicalize_geometry_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical GeometrySpec with a deterministic sha256 spec_id."""

    if not isinstance(spec, Mapping):
        _invalid("$", "GeometrySpec must be a JSON object.")
    normalized = deepcopy(dict(spec))
    validate_geometry_spec(normalized)
    normalized.pop("spec_id", None)
    canonical_without_id = canonical_json(normalized)
    normalized["spec_id"] = SPEC_ID_PREFIX + hashlib.sha256(canonical_without_id.encode("utf-8")).hexdigest()
    return normalized


def geometry_spec_id(spec: Mapping[str, Any]) -> str:
    """Compute the stable GeometrySpec id from the canonical JSON without spec_id."""

    return canonicalize_geometry_spec(spec)["spec_id"]


def canonical_json(value: Any) -> str:
    """Encode finite JSON using the RouterPolicy digest canonicalization style."""

    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SpecValidationError({"path": "$", "message": "GeometrySpec must contain only finite JSON values."}) from exc


def validate_geometry_spec(spec: Mapping[str, Any]) -> None:
    """Validate the stage-1 GeometrySpec contract without invoking the CAD kernel."""

    _ensure_no_extra_keys(spec, _ALLOWED_TOP_LEVEL_KEYS, "$")
    _require(spec, "spec_version", "$")
    if spec["spec_version"] != GEOMETRY_SPEC_VERSION:
        _invalid("$.spec_version", f"spec_version must be {GEOMETRY_SPEC_VERSION}.")
    if "spec_id" in spec and not isinstance(spec["spec_id"], str):
        _invalid("$.spec_id", "spec_id must be a string when provided.")
    if "name" in spec and not isinstance(spec["name"], str):
        _invalid("$.name", "name must be a string when provided.")

    parts = spec.get("parts")
    if not isinstance(parts, list) or not parts:
        _invalid("$.parts", "parts must be a non-empty array.")
    seen_part_ids: set[str] = set()
    for index, part in enumerate(parts):
        _validate_part(part, index, seen_part_ids)

    connections = spec.get("connections", [])
    if not isinstance(connections, list):
        _invalid("$.connections", "connections must be an array when provided.")
    for index, connection in enumerate(connections):
        _validate_connection(connection, index, seen_part_ids)

    if "declared" in spec:
        _validate_declared(spec["declared"])

    _assert_finite_json(spec)
    canonical_json(dict(spec))


def _validate_part(part: Any, index: int, seen_part_ids: set[str]) -> None:
    path = f"$.parts[{index}]"
    if not isinstance(part, Mapping):
        _invalid(path, "part must be an object.")
    _ensure_no_extra_keys(part, _ALLOWED_PART_KEYS, path)
    for key in ("part_id", "kind", "params"):
        _require(part, key, path)
    part_id = part["part_id"]
    if not isinstance(part_id, str) or not part_id:
        _invalid(f"{path}.part_id", "part_id must be a non-empty string.")
    if part_id in seen_part_ids:
        _invalid(f"{path}.part_id", "part_id must be unique.", part_id=part_id)
    seen_part_ids.add(part_id)

    kind = part["kind"]
    if kind not in SUPPORTED_PART_KINDS:
        _invalid(f"{path}.kind", "unsupported part kind.", kind=kind, supported=sorted(SUPPORTED_PART_KINDS))
    params = part["params"]
    if not isinstance(params, Mapping):
        _invalid(f"{path}.params", "params must be an object.")
    if kind == "tube_run":
        _validate_positive_params(params, _ALLOWED_TUBE_PARAMS, path, required=_ALLOWED_TUBE_PARAMS)
        _validate_wall(params, path)
    elif kind == "bend":
        _validate_positive_params(params, _ALLOWED_BEND_PARAMS, path, required=_ALLOWED_BEND_PARAMS)
        _validate_wall(params, path)
        if params["angle"] <= 0:
            _invalid(f"{path}.params.angle", "angle must be positive.")
    elif kind == "joint":
        _validate_positive_params(params, _ALLOWED_JOINT_PARAMS, path, required=_ALLOWED_JOINT_PARAMS)
        if params.get("joint_type") != "socket":
            _invalid(f"{path}.params.joint_type", "only socket joints are supported in v0.")
        _validate_wall(params, path)
    elif kind == "manifold":
        _validate_positive_params(params, _ALLOWED_MANIFOLD_PARAMS, path, required=_ALLOWED_MANIFOLD_PARAMS)
        _validate_wall({"outer_d": params["outer_d_main"], "wall_t": params["wall_t"]}, path)
        _validate_wall({"outer_d": params["out_d"], "wall_t": params["out_wall_t"]}, path)
        _validate_int_bounds(params["n_out"], f"{path}.params.n_out", minimum=1, maximum=12)
    elif kind == "capped_manifold":
        _validate_positive_params(
            params,
            _ALLOWED_CAPPED_MANIFOLD_PARAMS,
            path,
            required=_ALLOWED_CAPPED_MANIFOLD_PARAMS,
        )
        if params["main_wall_t"] * 2 >= params["main_outer_d"]:
            _invalid(
                f"{path}.params.main_wall_t",
                "main_wall_t must be less than half of main_outer_d.",
            )
        if params["branch_wall_t"] * 2 >= params["branch_outer_d"]:
            _invalid(
                f"{path}.params.branch_wall_t",
                "branch_wall_t must be less than half of branch_outer_d.",
            )
        _validate_int_bounds(params["branch_count"], f"{path}.params.branch_count", minimum=1, maximum=12)
        branch_pitch = float(params["branch_outer_d"]) + float(params["branch_gap"])
        header_length = (
            float(params["branch_outer_d"])
            + 2.0 * float(params["end_gap"])
            + branch_pitch * (int(params["branch_count"]) - 1)
        )
        if not isfinite(branch_pitch) or not isfinite(header_length):
            _invalid(f"{path}.params", "derived branch pitch and header length must be finite.")
    elif kind == "float":
        _validate_positive_params(params, _ALLOWED_FLOAT_PARAMS, path, required=_ALLOWED_FLOAT_PARAMS)
        _validate_int_bounds(params["n_mounts"], f"{path}.params.n_mounts", minimum=1, maximum=12)
    elif kind == "anchor_mount":
        _validate_positive_params(params, _ALLOWED_ANCHOR_MOUNT_PARAMS, path, required=_ALLOWED_ANCHOR_MOUNT_PARAMS)
    elif kind == "harvest_module":
        _validate_positive_params(params, _ALLOWED_HARVEST_MODULE_PARAMS, path, required=_ALLOWED_HARVEST_MODULE_PARAMS)
        _validate_wall(params, path)
        if params["port_d"] >= params["outer_d"]:
            _invalid(f"{path}.params.port_d", "port_d must be less than outer_d.")

    if "frame" in part:
        _validate_frame(part["frame"], f"{path}.frame")


def _validate_connection(connection: Any, index: int, part_ids: set[str]) -> None:
    path = f"$.connections[{index}]"
    if not isinstance(connection, Mapping):
        _invalid(path, "connection must be an object.")
    _ensure_no_extra_keys(connection, _ALLOWED_CONNECTION_KEYS, path)
    for key in ("from", "to"):
        _require(connection, key, path)
        endpoint = connection[key]
        if not isinstance(endpoint, str) or "." not in endpoint:
            _invalid(f"{path}.{key}", "connection endpoint must use '<part_id>.<port_name>'.")
        part_id, port_name = endpoint.split(".", 1)
        if part_id not in part_ids:
            _invalid(f"{path}.{key}", "connection references an unknown part_id.", part_id=part_id)
        if not port_name:
            _invalid(f"{path}.{key}", "connection endpoint must include a port name.")


def _validate_declared(declared: Any) -> None:
    if not isinstance(declared, Mapping):
        _invalid("$.declared", "declared must be an object when provided.")
    _ensure_no_extra_keys(declared, _ALLOWED_DECLARED_KEYS, "$.declared")
    if "total_volume_mm3" in declared:
        block = declared["total_volume_mm3"]
        if not isinstance(block, Mapping):
            _invalid("$.declared.total_volume_mm3", "total_volume_mm3 must be an object.")
        _ensure_no_extra_keys(block, _ALLOWED_TOTAL_VOLUME_KEYS, "$.declared.total_volume_mm3")
        for key in ("value", "rel_tol"):
            _require(block, key, "$.declared.total_volume_mm3")
            _require_finite_number(block[key], f"$.declared.total_volume_mm3.{key}")
        if block["value"] <= 0 or block["rel_tol"] < 0:
            _invalid("$.declared.total_volume_mm3", "value must be positive and rel_tol must be non-negative.")
    if "bbox_mm" in declared:
        block = declared["bbox_mm"]
        if not isinstance(block, Mapping):
            _invalid("$.declared.bbox_mm", "bbox_mm must be an object.")
        _ensure_no_extra_keys(block, _ALLOWED_BBOX_KEYS, "$.declared.bbox_mm")
        for key in ("min", "max"):
            _validate_vector3(block.get(key), f"$.declared.bbox_mm.{key}")
        _require(block, "abs_tol", "$.declared.bbox_mm")
        _require_finite_number(block["abs_tol"], "$.declared.bbox_mm.abs_tol")
        if block["abs_tol"] < 0:
            _invalid("$.declared.bbox_mm.abs_tol", "abs_tol must be non-negative.")
    if "min_wall_t" in declared:
        _require_finite_number(declared["min_wall_t"], "$.declared.min_wall_t")
        if declared["min_wall_t"] <= 0:
            _invalid("$.declared.min_wall_t", "min_wall_t must be positive.")


def _validate_positive_params(params: Mapping[str, Any], allowed: frozenset[str], path: str, *, required: frozenset[str]) -> None:
    _ensure_no_extra_keys(params, allowed, f"{path}.params")
    for key in required:
        _require(params, key, f"{path}.params")
        if key == "joint_type":
            if not isinstance(params[key], str):
                _invalid(f"{path}.params.{key}", "joint_type must be a string.")
            continue
        _require_finite_number(params[key], f"{path}.params.{key}")
        if params[key] <= 0:
            _invalid(f"{path}.params.{key}", "numeric parameters must be positive.")


def _validate_wall(params: Mapping[str, Any], path: str) -> None:
    if params["wall_t"] * 2 >= params["outer_d"]:
        _invalid(f"{path}.params.wall_t", "wall_t must be less than half of outer_d.")


def _validate_int_bounds(value: Any, path: str, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(path, "value must be an integer.")
    if value < minimum or value > maximum:
        _invalid(path, f"value must be between {minimum} and {maximum}.")


def _validate_frame(frame: Any, path: str) -> None:
    if not isinstance(frame, Mapping):
        _invalid(path, "frame must be an object.")
    _ensure_no_extra_keys(frame, _ALLOWED_FRAME_KEYS, path)
    _validate_vector3(frame.get("origin"), f"{path}.origin")
    _validate_vector3(frame.get("direction"), f"{path}.direction")
    if all(float(value) == 0.0 for value in frame["direction"]):
        _invalid(f"{path}.direction", "direction must be non-zero.")


def _validate_vector3(value: Any, path: str) -> None:
    if not isinstance(value, list) or len(value) != 3:
        _invalid(path, "value must be a 3-number array.")
    for index, item in enumerate(value):
        _require_finite_number(item, f"{path}[{index}]")


def _assert_finite_json(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int | float):
        if not isfinite(float(value)):
            _invalid(path, "number must be finite.")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _invalid(path, "object keys must be strings.")
            _assert_finite_json(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_finite_json(item, f"{path}[{index}]")
        return
    _invalid(path, "value must be JSON-compatible.")


def _require_finite_number(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _invalid(path, "value must be a finite number.")
    if not isfinite(float(value)):
        _invalid(path, "value must be finite.")


def _require(mapping: Mapping[str, Any], key: str, path: str) -> None:
    if key not in mapping:
        _invalid(path, "missing required property.", property=key)


def _ensure_no_extra_keys(mapping: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    extra = sorted(set(mapping) - allowed)
    if extra:
        _invalid(path, "unexpected property.", properties=extra)


def _invalid(path: str, message: str, **extra: Any) -> None:
    detail = {"path": path, "message": message}
    detail.update(extra)
    raise SpecValidationError(detail)
