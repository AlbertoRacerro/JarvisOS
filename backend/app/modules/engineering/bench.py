"""Headless bench command over engineering evaluators (spec 282).

Provides a single CLI entry point to list evaluators, run an evaluation
into an isolated output folder with bounded stdout view, and compare two runs.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from app.modules.bluecad.fem_adapter import solve_static_analysis
from app.modules.bluecad.mesh_adapter import mesh_analysis_spec
from app.modules.bluecad.registry import ToolRegistryError, resolve_tool
from app.modules.bluecad.result_adapters import (
    fem_evaluator_descriptor,
    fem_result_to_evaluation_result,
    mesh_evaluator_descriptor,
    mesh_result_to_evaluation_result,
    solid_build_result_to_evaluation_result,
    solid_evaluator_descriptor,
)
from app.modules.bluecad.service import build_geometry_spec_file
from app.modules.engineering.evaluator_contracts import (
    AvailabilityState,
    EngineeringEvaluator,
    EvaluationFailure,
    EvaluationRequest,
    EvaluationResult,
    NumericalDiagnostics,
    result_verdict,
    validate_evaluation_result,
)
from app.modules.engineering.operator_service import SAFE_ID, evaluator_registry
from app.modules.engineering.refs import EvaluationResultRef

if TYPE_CHECKING:
    from app.modules.ai.agent_contracts import StructuredToolCall

BLUECAD_EVALUATORS = {"bluecad.solid", "bluecad.mesh", "bluecad.fem"}
MAX_VIEW_CHARS = 8000

_ACTIVE_BENCH_WORKERS: set[str] = set()
_ACTIVE_BENCH_LOCK = threading.Lock()


def acquire_bench_worker(worker_id: str) -> bool:
    with _ACTIVE_BENCH_LOCK:
        if worker_id in _ACTIVE_BENCH_WORKERS:
            return False
        _ACTIVE_BENCH_WORKERS.add(worker_id)
        return True


def release_bench_worker(worker_id: str) -> None:
    with _ACTIVE_BENCH_LOCK:
        _ACTIVE_BENCH_WORKERS.discard(worker_id)


def _has_path_keys(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_str = str(key)
            if key_str.endswith("_path") or key_str == "path" or _has_path_keys(val):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _has_path_keys(item):
                return True
    return False


class BenchInputError(Exception):
    """Refusal on bad input or usage."""

    def __init__(self, code: str, hint: str) -> None:
        super().__init__(hint)
        self.code = code
        self.hint = hint


def emit_error(code: str, hint: str) -> None:
    single_line_hint = " ".join(hint.splitlines()).strip()
    payload = {
        "code": code,
        "hint": single_line_hint,
    }
    print(json.dumps(payload, indent=2))


def _check_within_root(path: Path, root: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise BenchInputError(
            "PATH_OUTSIDE_ROOT",
            f"Path for {label} ({resolved_path}) is outside working root ({resolved_root}).",
        ) from exc
    return resolved_path


def _check_fresh_out_dir(out_dir: Path) -> Path:
    if out_dir.exists():
        if not out_dir.is_dir():
            raise BenchInputError("OUT_DIR_INVALID", f"--out path '{out_dir}' exists and is not a directory.")
        if any(out_dir.iterdir()):
            raise BenchInputError("OUT_DIR_NOT_EMPTY", f"--out directory '{out_dir}' must be fresh or empty.")
    return out_dir


def _check_bluecad_availability(
    evaluator_id: str, registry_path: Path | None
) -> tuple[AvailabilityState, str | None, str]:
    if evaluator_id == "bluecad.solid":
        tool_code = None
        try:
            resolve_tool("build123d", registry_path)
        except ToolRegistryError as exc:
            tool_code = exc.code
        except Exception as exc:
            tool_code = f"REGISTRY_ERROR_{type(exc).__name__.upper()}"

        if tool_code in {"TOOL_UNKNOWN", "TOOL_BINARY_MISSING"}:
            return "not_installed", tool_code, "unavailable"
        if tool_code == "TOOL_DISABLED":
            return "disabled", tool_code, "unavailable"
        if tool_code is not None:
            return "unhealthy", tool_code, "unavailable"

        try:
            import build123d

            version = getattr(build123d, "__version__", "0.11.1")
            return "available", None, version
        except ImportError:
            return "not_installed", "BUILD123D_NOT_INSTALLED", "unavailable"
        except Exception as exc:
            return "unhealthy", f"IMPORT_ERROR_{type(exc).__name__.upper()}", "unavailable"

    tool_id = "gmsh" if evaluator_id == "bluecad.mesh" else "calculix"
    fallback_version = "4.15.2" if evaluator_id == "bluecad.mesh" else "2.22"
    try:
        tool = resolve_tool(tool_id, registry_path)
        version = str(tool.get("version_pin") or fallback_version)
        return "available", None, version
    except ToolRegistryError as exc:
        state: AvailabilityState = (
            "not_installed"
            if exc.code in {"TOOL_BINARY_MISSING", "TOOL_UNKNOWN"}
            else "disabled"
            if exc.code == "TOOL_DISABLED"
            else "unhealthy"
        )
        return state, exc.code, "unavailable"
    except Exception as exc:
        return "unhealthy", f"TOOL_ERROR_{type(exc).__name__.upper()}", "unavailable"


def format_bounded_view(
    *,
    evaluator_id: str,
    status: str,
    verdict: str,
    failure: dict[str, Any] | None,
    checks: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    result_path: Path,
) -> str:
    failed_checks = [c for c in checks if c.get("status") == "fail"]
    warning_checks = [c for c in checks if c.get("status") == "warn"]
    other_checks = [c for c in checks if c.get("status") not in {"fail", "warn"}]

    view: dict[str, Any] = {
        "evaluator_id": evaluator_id,
        "status": status,
        "verdict": verdict,
        "failure": failure,
        "failed_checks": failed_checks,
        "warning_checks": warning_checks,
        "other_checks": other_checks,
        "outputs": outputs,
        "artifacts": artifacts,
        "result_path": str(result_path),
        "truncated": False,
    }

    formatted = json.dumps(view, indent=2)
    if len(formatted) <= MAX_VIEW_CHARS:
        return formatted

    # Cut lower-priority sections: all-pass checks, then outputs, then artifact paths
    view["truncated"] = True
    view["other_checks"] = []
    formatted = json.dumps(view, indent=2)
    if len(formatted) <= MAX_VIEW_CHARS:
        return formatted

    view["outputs"] = []
    formatted = json.dumps(view, indent=2)
    if len(formatted) <= MAX_VIEW_CHARS:
        return formatted

    view["artifacts"] = []
    formatted = json.dumps(view, indent=2)
    if len(formatted) <= MAX_VIEW_CHARS:
        return formatted

    compact = json.dumps(view)
    if len(compact) <= MAX_VIEW_CHARS:
        return compact

    # Still too large: keep every failed and warning check id, status and numbers, drop their prose,
    # then cap the failure message. The view stays valid JSON; result.json holds the full text.
    keep = ("id", "family", "status", "actual", "limit", "unit")
    view["failed_checks"] = [{k: c.get(k) for k in keep} for c in failed_checks]
    view["warning_checks"] = [{k: c.get(k) for k in keep} for c in warning_checks]
    if failure and failure.get("message"):
        view["failure"] = {**failure, "message": str(failure["message"])[:200]}
    return json.dumps(view, separators=(",", ":"))


def list_evaluators(*, tool_registry: Path | None = None) -> list[dict[str, Any]]:
    registry = evaluator_registry()
    rows: list[dict[str, Any]] = []

    # 1. Six registry evaluators
    for evaluator_id, evaluator in registry.items():
        desc = evaluator.descriptor()
        try:
            avail = evaluator.availability()
            state = avail.state
            reason = avail.reason_code
            version = avail.backend_version or desc.backend_version
        except Exception as exc:
            state = "unknown"
            reason = f"STATUS_ERROR_{type(exc).__name__.upper()}"
            version = desc.backend_version

        rows.append(
            {
                "id": evaluator_id,
                "evaluator_id": evaluator_id,
                "backend_kind": str(desc.backend_kind),
                "backend_name": str(desc.backend_name),
                "name": str(desc.backend_name),
                "backend_version": str(version),
                "version": str(version),
                "fidelity": str(desc.fidelity),
                "availability_state": str(state),
                "state": str(state),
                "reason_code": reason,
                "required_checks": list(desc.required_checks),
                "advisory_checks": list(desc.advisory_checks),
                "request_kind": "evaluation_request",
            }
        )

    # 2. Three BLUECAD evaluators
    descriptors = {
        "bluecad.solid": (solid_evaluator_descriptor(tool_registry), "geometry_spec"),
        "bluecad.mesh": (mesh_evaluator_descriptor(tool_registry), "analysis_spec"),
        "bluecad.fem": (fem_evaluator_descriptor(tool_registry), "analysis_spec"),
    }

    for evaluator_id, (desc, req_kind) in descriptors.items():
        state, reason, version = _check_bluecad_availability(evaluator_id, tool_registry)
        rows.append(
            {
                "id": evaluator_id,
                "evaluator_id": evaluator_id,
                "backend_kind": str(desc.backend_kind),
                "backend_name": str(desc.backend_name),
                "name": str(desc.backend_name),
                "backend_version": version if version != "unavailable" else str(desc.backend_version),
                "version": version if version != "unavailable" else str(desc.backend_version),
                "fidelity": str(desc.fidelity),
                "availability_state": state,
                "state": state,
                "reason_code": reason,
                "required_checks": list(desc.required_checks),
                "advisory_checks": list(desc.advisory_checks),
                "request_kind": req_kind,
            }
        )

    return rows


def handle_list(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="bench list")
    parser.add_argument("--tool-registry", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)

    root = Path.cwd() if args.root is None else args.root.resolve()
    reg_path: Path | None = None
    if args.tool_registry is not None:
        try:
            reg_path = _check_within_root(args.tool_registry, root, "--tool-registry")
            if not reg_path.is_file():
                emit_error("INPUT_NOT_FOUND", f"Tool registry file '{reg_path}' does not exist.")
                return 2
        except BenchInputError as exc:
            emit_error(exc.code, exc.hint)
            return 2

    rows = list_evaluators(tool_registry=reg_path)
    print(json.dumps(rows, indent=2))
    return 0


def _run_registry_evaluator(
    evaluator_id: str,
    evaluator: EngineeringEvaluator,
    request_data: dict[str, Any],
    out_dir: Path,
) -> tuple[EvaluationResult, str]:
    started = datetime.now(UTC)
    try:
        req = EvaluationRequest.model_validate(request_data)
    except ValidationError as exc:
        fields = [ ".".join(str(loc) for loc in err["loc"]) for err in exc.errors() ]
        fields_str = ", ".join(fields)
        if len(fields_str) > 200:
            fields_str = fields_str[:197] + "..."
        raise BenchInputError(
            "INVALID_REQUEST",
            f"Request schema validation failed for fields: {fields_str}",
        ) from exc

    desc = evaluator.descriptor()
    avail = evaluator.availability()
    if avail.state != "available":
        ref_req = req.request_ref
        res_ref = EvaluationResultRef(
            authority_owner=ref_req.authority_owner,
            object_type="evaluation_result",
            object_id=f"res-{ref_req.object_id}",
            workspace_id=ref_req.workspace_id,
            revision="1",
        )
        result = EvaluationResult(
            result_ref=res_ref,
            request_ref=ref_req,
            evaluator_id=evaluator_id,
            backend_version=desc.backend_version,
            status="refused",
            failure=EvaluationFailure(
                category="not_available",
                backend_code=avail.reason_code or "EVALUATOR_UNAVAILABLE",
                message="Evaluator is not available to run.",
            ),
            fidelity=desc.fidelity,
            checks=(),
            outputs=(),
            output_artifacts=(),
            numerical=NumericalDiagnostics(),
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        verdict = result_verdict(desc, result)
        return result, verdict

    result = evaluator.evaluate(req)
    validate_evaluation_result(req, result)
    verdict = result_verdict(desc, result)
    return result, verdict


def _run_bluecad_solid(
    request_path: Path,
    out_dir: Path,
    tool_registry: Path | None,
    timeout_s: float,
) -> tuple[EvaluationResult, str]:
    build_result = build_geometry_spec_file(request_path, out_dir, timeout_s=timeout_s)
    eval_res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor(tool_registry)
    verdict = result_verdict(desc, eval_res)
    return eval_res, verdict


def _resolve_geometry_paths(
    request_data: dict[str, Any], req_dir: Path, root: Path, *, required: bool
) -> dict[str, Any]:
    """Resolve geometry.step_path and geometry.manifest_path against the request folder, inside the root."""
    geometry = request_data.get("geometry")
    if not isinstance(geometry, dict):
        if required:
            raise BenchInputError("INVALID_REQUEST", "the analysis spec requires a 'geometry' mapping.")
        return request_data
    resolved = dict(geometry)
    for key in ("step_path", "manifest_path"):
        raw = geometry.get(key)
        if not raw:
            if required:
                raise BenchInputError("INVALID_REQUEST", f"geometry.{key} is required; point it at the bluecad.solid output.")
            continue
        path = _check_within_root(Path(raw) if Path(raw).is_absolute() else req_dir / raw, root, f"geometry.{key}")
        if not path.is_file():
            raise BenchInputError("INPUT_NOT_FOUND", f"geometry.{key} not found: '{path}'")
        resolved[key] = str(path)
    return {**request_data, "geometry": resolved}


def _run_bluecad_mesh(
    request_data: dict[str, Any],
    request_path: Path,
    out_dir: Path,
    root: Path,
    tool_registry: Path | None,
    timeout_s: float,
) -> tuple[EvaluationResult, str]:
    analysis_spec = _resolve_geometry_paths(request_data, request_path.parent, root, required=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    mesh_result = mesh_analysis_spec(
        analysis_spec,
        out_dir,
        registry_path=tool_registry,
        timeout_s=timeout_s,
    )
    (out_dir / "mesh_result.json").write_text(
        json.dumps(mesh_result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    eval_res = mesh_result_to_evaluation_result(mesh_result, analysis_spec)
    desc = mesh_evaluator_descriptor(tool_registry)
    verdict = result_verdict(desc, eval_res)
    return eval_res, verdict


def _run_bluecad_fem(
    request_data: dict[str, Any],
    request_path: Path,
    from_dir: Path | None,
    out_dir: Path,
    root: Path,
    tool_registry: Path | None,
    timeout_s: float,
) -> tuple[EvaluationResult, str]:
    if from_dir is None:
        raise BenchInputError(
            "MISSING_FROM_DIR",
            "bluecad.fem requires --from DIR naming an earlier mesh output folder.",
        )
    from_path = _check_within_root(from_dir, root, "--from")
    if not from_path.is_dir():
        raise BenchInputError("INPUT_NOT_FOUND", f"--from directory '{from_path}' does not exist.")

    mesh_result_path = from_path / "mesh_result.json"
    if not mesh_result_path.is_file():
        raise BenchInputError("INPUT_NOT_FOUND", f"mesh_result.json not found in --from folder '{from_path}'.")

    mesh_result = json.loads(mesh_result_path.read_text(encoding="utf-8"))
    try:
        mesh_inp = Path(mesh_result["artifacts"]["mesh_inp"]["path"])
    except (KeyError, TypeError) as exc:
        raise BenchInputError("INVALID_REQUEST", "mesh_result.json in --from has no artifacts.mesh_inp.path; rerun bluecad.mesh.") from exc
    _check_within_root(mesh_inp if mesh_inp.is_absolute() else from_path / mesh_inp, root, "mesh_inp artifact")
    request_data = _resolve_geometry_paths(request_data, request_path.parent, root, required=False)

    out_dir.mkdir(parents=True, exist_ok=True)
    fem_result = solve_static_analysis(
        request_data,
        mesh_result,
        out_dir,
        registry_path=tool_registry,
        timeout_s=timeout_s,
    )
    (out_dir / "fem_result.json").write_text(
        json.dumps(fem_result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    eval_res = fem_result_to_evaluation_result(fem_result, request_data)
    desc = fem_evaluator_descriptor(tool_registry)
    verdict = result_verdict(desc, eval_res)
    return eval_res, verdict


def run_evaluation(
    evaluator_id: str,
    request_data: dict[str, Any],
    out_dir: Path,
    *,
    from_dir: Path | None = None,
    root: Path | None = None,
    tool_registry: Path | None = None,
    timeout_s: float = 60.0,
    request_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if root is None:
        root = Path.cwd().resolve()

    registry = evaluator_registry()

    if evaluator_id not in registry and evaluator_id not in BLUECAD_EVALUATORS:
        raise BenchInputError(
            "UNKNOWN_EVALUATOR",
            f"Unknown evaluator '{evaluator_id}'. Run 'bench list' to see available evaluators.",
        )

    if evaluator_id in registry:
        result, verdict = _run_registry_evaluator(evaluator_id, registry[evaluator_id], request_data, out_dir)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        if request_path is None:
            request_path = out_dir / "request.json"
            request_path.write_text(json.dumps(request_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        if evaluator_id == "bluecad.solid":
            result, verdict = _run_bluecad_solid(request_path, out_dir, tool_registry, timeout_s)
        elif evaluator_id == "bluecad.mesh":
            result, verdict = _run_bluecad_mesh(request_data, request_path, out_dir, root, tool_registry, timeout_s)
        elif evaluator_id == "bluecad.fem":
            result, verdict = _run_bluecad_fem(
                request_data, request_path, from_dir, out_dir, root, tool_registry, timeout_s
            )
        else:
            raise BenchInputError("UNKNOWN_EVALUATOR", f"Unhandled evaluator '{evaluator_id}'")

    out_dir.mkdir(parents=True, exist_ok=True)
    res_dict = result.model_dump(mode="json")
    res_dict["verdict"] = verdict
    result_file = out_dir / "result.json"
    result_file.write_text(json.dumps(res_dict, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    view_text = format_bounded_view(
        evaluator_id=result.evaluator_id,
        status=result.status,
        verdict=verdict,
        failure=result.failure.model_dump(mode="json") if result.failure else None,
        checks=[c.model_dump(mode="json") for c in result.checks],
        outputs=[o.model_dump(mode="json") for o in result.outputs],
        artifacts=[a.model_dump(mode="json") for a in result.output_artifacts],
        result_path=result_file,
    )
    bounded_view = json.loads(view_text)
    return res_dict, bounded_view


def handle_run(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="bench run")
    parser.add_argument("evaluator_id", type=str)
    parser.add_argument("request_json", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--from", dest="from_dir", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--tool-registry", type=Path, default=None)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    args = parser.parse_args(argv)

    root = Path.cwd() if args.root is None else args.root.resolve()

    try:
        req_path = _check_within_root(args.request_json, root, "request file")
        out_path = _check_within_root(args.out, root, "--out")
        _check_fresh_out_dir(out_path)

        reg_path: Path | None = None
        if args.tool_registry is not None:
            reg_path = _check_within_root(args.tool_registry, root, "--tool-registry")
            if not reg_path.is_file():
                raise BenchInputError("INPUT_NOT_FOUND", f"Tool registry '{reg_path}' does not exist.")

        if not req_path.is_file():
            raise BenchInputError("INPUT_NOT_FOUND", f"Request file '{req_path}' does not exist.")

        try:
            req_text = req_path.read_text(encoding="utf-8")
            req_data = json.loads(req_text)
        except json.JSONDecodeError as exc:
            raise BenchInputError("INVALID_JSON", f"Request file is not valid JSON: {exc}") from exc

        res_dict, bounded_view = run_evaluation(
            args.evaluator_id,
            req_data,
            out_path,
            from_dir=args.from_dir,
            root=root,
            tool_registry=reg_path,
            timeout_s=args.timeout_s,
            request_path=req_path,
        )

    except BenchInputError as exc:
        emit_error(exc.code, exc.hint)
        return 2
    except Exception as exc:  # native engines and evaluators may raise on malformed input
        emit_error(f"ENGINE_ERROR_{type(exc).__name__.upper()}", f"{args.evaluator_id} raised: {exc}"[:240])
        return 2

    view_text = (
        json.dumps(bounded_view, indent=2)
        if len(json.dumps(bounded_view, indent=2)) <= MAX_VIEW_CHARS
        else json.dumps(bounded_view, separators=(",", ":"))
    )
    print(view_text)

    # Exit codes:
    # 0 for usable and usable_with_warnings
    # 1 for unchecked and not_usable
    # 2 for a result that is not succeeded and for any usage or input error
    if res_dict.get("status") != "succeeded":
        return 2
    if res_dict.get("verdict") in {"usable", "usable_with_warnings"}:
        return 0
    return 1


def compare_runs(
    dir_a: Path,
    dir_b: Path,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    if root is not None:
        dir_a = _check_within_root(dir_a, root, "dirA")
        dir_b = _check_within_root(dir_b, root, "dirB")

    res_a_path = dir_a / "result.json"
    res_b_path = dir_b / "result.json"

    if not res_a_path.is_file():
        raise BenchInputError("INPUT_NOT_FOUND", f"Result file missing in dirA: '{res_a_path}'")
    if not res_b_path.is_file():
        raise BenchInputError("INPUT_NOT_FOUND", f"Result file missing in dirB: '{res_b_path}'")

    try:
        res_a = json.loads(res_a_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchInputError("INVALID_JSON", f"Result file in dirA is not valid JSON: {exc}") from exc

    try:
        res_b = json.loads(res_b_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchInputError("INVALID_JSON", f"Result file in dirB is not valid JSON: {exc}") from exc

    outputs_a = {item["name"]: item["value"] for item in res_a.get("outputs", []) if "name" in item and "value" in item}
    outputs_b = {item["name"]: item["value"] for item in res_b.get("outputs", []) if "name" in item and "value" in item}

    outputs_comparison: dict[str, Any] = {}
    for name in sorted(set(outputs_a.keys()) | set(outputs_b.keys())):
        val_a_obj = outputs_a.get(name)
        val_b_obj = outputs_b.get(name)
        if val_a_obj is not None and val_b_obj is not None:
            unit_a = val_a_obj.get("unit")
            unit_b = val_b_obj.get("unit")
            num_a = float(val_a_obj.get("value", 0.0))
            num_b = float(val_b_obj.get("value", 0.0))
            if unit_a == unit_b:
                diff_abs = abs(num_b - num_a)
                diff_rel = abs(num_b - num_a) / abs(num_a) if num_a != 0.0 else (0.0 if num_b == 0.0 else None)
                outputs_comparison[name] = {
                    "a": {"value": num_a, "unit": unit_a},
                    "b": {"value": num_b, "unit": unit_b},
                    "unit": unit_a,
                    "absolute_difference": diff_abs,
                    "relative_difference": diff_rel,
                    "mark": "match",
                }
            else:
                outputs_comparison[name] = {
                    "a": {"value": num_a, "unit": unit_a},
                    "b": {"value": num_b, "unit": unit_b},
                    "absolute_difference": None,
                    "relative_difference": None,
                    "mark": "unit_mismatch",
                }
        else:
            outputs_comparison[name] = {
                "a": {"value": val_a_obj.get("value"), "unit": val_a_obj.get("unit")} if val_a_obj else None,
                "b": {"value": val_b_obj.get("value"), "unit": val_b_obj.get("unit")} if val_b_obj else None,
                "absolute_difference": None,
                "relative_difference": None,
                "mark": "missing",
            }

    checks_a = {item["id"]: item for item in res_a.get("checks", []) if "id" in item}
    checks_b = {item["id"]: item for item in res_b.get("checks", []) if "id" in item}

    checks_comparison: dict[str, Any] = {}
    for check_id in sorted(set(checks_a.keys()) | set(checks_b.keys())):
        ca = checks_a.get(check_id)
        cb = checks_b.get(check_id)
        checks_comparison[check_id] = {
            "status_a": ca.get("status") if ca else None,
            "actual_a": ca.get("actual") if ca else None,
            "status_b": cb.get("status") if cb else None,
            "actual_b": cb.get("actual") if cb else None,
        }

    compare_doc: dict[str, Any] = {
        "evaluator_a": res_a.get("evaluator_id"),
        "status_a": res_a.get("status"),
        "verdict_a": res_a.get("verdict"),
        "evaluator_b": res_b.get("evaluator_id"),
        "status_b": res_b.get("status"),
        "verdict_b": res_b.get("verdict"),
        "outputs": outputs_comparison,
        "checks": checks_comparison,
        "truncated": False,
    }

    formatted = json.dumps(compare_doc, indent=2)
    if len(formatted) > MAX_VIEW_CHARS:
        # Keep differing checks first, then outputs with the largest relative difference, as valid JSON.
        compare_doc["truncated"] = True
        compare_doc["checks"] = {
            k: v for k, v in checks_comparison.items() if v["status_a"] != v["status_b"] or v["status_a"] in {"fail", "warn"}
        }
        ranked = sorted(
            outputs_comparison.items(),
            key=lambda item: -(item[1]["relative_difference"] if item[1]["relative_difference"] is not None else float("inf")),
        )
        while True:
            compare_doc["outputs"] = dict(ranked)
            formatted = json.dumps(compare_doc, separators=(",", ":"))
            if len(formatted) <= MAX_VIEW_CHARS or not ranked:
                break
            ranked = ranked[: len(ranked) // 2]

    return compare_doc


def handle_compare(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="bench compare")
    parser.add_argument("dir_a", type=Path)
    parser.add_argument("dir_b", type=Path)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)

    root = Path.cwd() if args.root is None else args.root.resolve()

    try:
        compare_doc = compare_runs(args.dir_a, args.dir_b, root=root)
    except BenchInputError as exc:
        emit_error(exc.code, exc.hint)
        return 2

    if compare_doc.get("truncated"):
        print(json.dumps(compare_doc, separators=(",", ":")))
    else:
        print(json.dumps(compare_doc, indent=2))
    return 0


def show_run(
    run_dir: Path,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    if root is not None:
        run_dir = _check_within_root(run_dir, root, "run directory")
    res_path = run_dir / "result.json"
    if not res_path.is_file():
        raise BenchInputError("INPUT_NOT_FOUND", f"Result file missing in run directory: '{res_path}'")
    try:
        res_data = json.loads(res_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchInputError("INVALID_JSON", f"Result file is not valid JSON: {exc}") from exc

    view_text = format_bounded_view(
        evaluator_id=res_data.get("evaluator_id", "unknown"),
        status=res_data.get("status", "unknown"),
        verdict=res_data.get("verdict", "unknown"),
        failure=res_data.get("failure"),
        checks=res_data.get("checks", []),
        outputs=res_data.get("outputs", []),
        artifacts=res_data.get("output_artifacts", res_data.get("artifacts", [])),
        result_path=res_path,
    )
    return json.loads(view_text)


# Evaluators the agent may run: they write only inside the run folder (or nothing) and finish within the
# tool timeout. DWSIM, OpenFOAM and the day/night simulator write elsewhere or run on their own deadlines.
AGENT_RUNNABLE_EVALUATORS = frozenset(BLUECAD_EVALUATORS | {
    "fluids.pipe_pressure_drop", "ht.internal_convection", "coolprop.pure_fluid_properties"})
# An absolute or parent-relative path: "/a/b/c", "../run_x/c", "C:\\a\\c". Units ("W/(m**2*K)", "kg/m3") have a
# word character or ")" before their slash and are left alone.
_PATH = re.compile(r"(?<![\w.)\]])(?:\.\.[/\\]|/|[A-Za-z]:\\)(?:[^\s\"'/\\]+[/\\])*([^\s\"'/\\]+)")


def _strip_paths(value: Any) -> Any:
    if isinstance(value, str):
        return _PATH.sub(r"\1", value)
    if isinstance(value, dict):
        return {key: _strip_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_paths(item) for item in value]
    return value


def _agent_view(view: dict[str, Any]) -> dict[str, Any]:
    """The agent never sees a filesystem path: every absolute path is reduced to its last component."""
    if "result_path" in view:
        view = {**view, "result_path": "result.json"}
    return _strip_paths(view)


def dispatch_bench_tool(
    call: StructuredToolCall,
    workspace_id: str,
    error: str,
) -> tuple[dict[str, Any] | None, str]:
    """Bench tool entry for the agent; an unexpected error is a refusal, never an exception."""
    try:
        return _dispatch_bench_tool(call, workspace_id, error)
    except Exception as exc:
        return None, f"bench_error_{type(exc).__name__.lower()}"[:96]


def _dispatch_bench_tool(
    call: StructuredToolCall,
    workspace_id: str,
    error: str,
) -> tuple[dict[str, Any] | None, str]:
    from app.modules.engineering.operator_service import workspace_dir

    try:
        studies_dir = workspace_dir(workspace_id)
    except Exception:
        return None, "workspace_not_found"

    bench_root = (studies_dir.parent / "bench").resolve()
    bench_root.mkdir(parents=True, exist_ok=True)
    root = bench_root

    if call.capability_id == "jarvis.bench_read":
        for key in call.arguments:
            if str(key).endswith("_path") or key == "path":
                return None, "path_argument_refused"

        op = call.arguments.get("op")
        if not isinstance(op, str):
            return None, "invalid_arguments"

        if op == "list":
            rows = list_evaluators()
            return {"op": "list", "rows": rows}, error

        if op == "show":
            run_id = call.arguments.get("run_id")
            if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id) or run_id in {".", ".."}:
                return None, "invalid_arguments"
            run_dir = (bench_root / run_id).resolve()
            if not run_dir.is_relative_to(bench_root) or not run_dir.is_dir():
                return None, "run_not_found"
            try:
                view = show_run(run_dir, root=bench_root)
                return {"op": "show", "run_id": run_id, "view": _agent_view(view)}, error
            except BenchInputError:
                return None, "run_not_found"
            except Exception:
                return None, "show_failed"

        if op == "compare":
            run_a = call.arguments.get("run_id_a")
            run_b = call.arguments.get("run_id_b")
            if not isinstance(run_a, str) or not SAFE_ID.fullmatch(run_a) or run_a in {".", ".."}:
                return None, "invalid_arguments"
            if not isinstance(run_b, str) or not SAFE_ID.fullmatch(run_b) or run_b in {".", ".."}:
                return None, "invalid_arguments"
            dir_a = (bench_root / run_a).resolve()
            dir_b = (bench_root / run_b).resolve()
            if not dir_a.is_relative_to(bench_root) or not dir_a.is_dir():
                return None, "run_not_found"
            if not dir_b.is_relative_to(bench_root) or not dir_b.is_dir():
                return None, "run_not_found"
            try:
                compare_doc = compare_runs(dir_a, dir_b, root=bench_root)
                return {"op": "compare", "run_id_a": run_a, "run_id_b": run_b, "view": compare_doc}, error
            except BenchInputError:
                return None, "run_not_found"
            except Exception:
                return None, "compare_failed"

        return None, "unknown_operation"

    if call.capability_id == "jarvis.bench_run":
        for key in call.arguments:
            if str(key).endswith("_path") or key == "path":
                return None, "path_argument_refused"

        raw_request = call.arguments.get("request")
        if not isinstance(raw_request, dict):
            return None, "invalid_arguments"
        if _has_path_keys(raw_request):
            return None, "path_in_request_forbidden"

        request_data = copy.deepcopy(raw_request)

        timeout_s = call.arguments.get("timeout_s", 60.0)
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            return None, "invalid_arguments"
        if timeout_s > 100.0 or timeout_s <= 0.0:
            return None, "timeout_exceeded"

        evaluator_id = call.arguments.get("evaluator_id")
        if not isinstance(evaluator_id, str):
            return None, "invalid_arguments"

        registry = evaluator_registry()
        if evaluator_id not in registry and evaluator_id not in BLUECAD_EVALUATORS:
            return None, "unknown_evaluator"
        if evaluator_id not in AGENT_RUNNABLE_EVALUATORS:
            return None, "evaluator_not_agent_runnable"
        if evaluator_id in registry:
            # The grant names the workspace and the tool bounds the time; the request cannot widen either.
            request_ref = request_data.get("request_ref")
            if not isinstance(request_ref, dict):
                return None, "invalid_arguments"
            now = datetime.now(UTC)
            request_data["request_ref"] = {**request_ref, "workspace_id": workspace_id}
            request_data["requested_at"] = now.isoformat()
            request_data["deadline_at"] = (now + timedelta(seconds=float(timeout_s))).isoformat()

        geometry_run = call.arguments.get("geometry_run")
        if evaluator_id == "bluecad.mesh" and not geometry_run:
            return None, "missing_geometry_run"

        if geometry_run is not None:
            if not isinstance(geometry_run, str) or not SAFE_ID.fullmatch(geometry_run) or geometry_run in {".", ".."}:
                return None, "invalid_run_id"
            solid_dir = (bench_root / geometry_run).resolve()
            if not solid_dir.is_relative_to(bench_root) or not solid_dir.is_dir():
                return None, "run_not_found"
            res_file = solid_dir / "result.json"
            if not res_file.is_file():
                return None, "run_not_found"
            try:
                res_data = json.loads(res_file.read_text(encoding="utf-8"))
            except Exception:
                return None, "invalid_run_result"
            if res_data.get("evaluator_id") != "bluecad.solid":
                return None, "wrong_evaluator"
            step_file = solid_dir / "model.step"
            manifest_file = solid_dir / "manifest.json"
            if not step_file.is_file() or not manifest_file.is_file():
                return None, "missing_geometry_files"
            raw_geometry = request_data.get("geometry", {})
            if not isinstance(raw_geometry, dict):
                return None, "invalid_arguments"
            geometry = dict(raw_geometry)
            geometry["step_path"] = str(step_file)
            geometry["manifest_path"] = str(manifest_file)
            request_data["geometry"] = geometry

        from_run = call.arguments.get("from_run")
        if evaluator_id == "bluecad.fem" and not from_run:
            return None, "missing_from_run"

        from_dir: Path | None = None
        if from_run is not None:
            if not isinstance(from_run, str) or not SAFE_ID.fullmatch(from_run) or from_run in {".", ".."}:
                return None, "invalid_run_id"
            from_dir = (bench_root / from_run).resolve()
            if not from_dir.is_relative_to(bench_root) or not from_dir.is_dir():
                return None, "run_not_found"
            res_file = from_dir / "result.json"
            if not res_file.is_file():
                return None, "run_not_found"
            try:
                res_data = json.loads(res_file.read_text(encoding="utf-8"))
            except Exception:
                return None, "invalid_run_result"
            if res_data.get("evaluator_id") != "bluecad.mesh":
                return None, "wrong_evaluator"
            if not (from_dir / "mesh_result.json").is_file():
                return None, "missing_mesh_result"

        worker_id = call.session_ref.hermes_session_id if call.session_ref else "default"
        if not acquire_bench_worker(worker_id):
            return None, "busy"

        run_id = f"run_{uuid4().hex[:12]}"
        out_dir = bench_root / run_id

        try:
            res_dict, bounded_view = run_evaluation(
                evaluator_id,
                request_data,
                out_dir,
                from_dir=from_dir,
                root=root,
                timeout_s=float(timeout_s),
            )
            result_payload = {
                "run_id": run_id,
                "status": res_dict.get("status"),
                "verdict": res_dict.get("verdict"),
                "view": _agent_view(bounded_view),
            }
            return result_payload, error
        except BenchInputError as exc:
            return None, f"bench_input_error_{exc.code.lower()}"
        except Exception as exc:
            return None, f"engine_error_{type(exc).__name__.lower()}"
        finally:
            release_bench_worker(worker_id)

    return None, error


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(prog="python -m app.modules.engineering.bench")
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser("list")
    subparsers.add_parser("run")
    subparsers.add_parser("compare")

    if not argv:
        emit_error("USAGE_ERROR", "A subcommand (list, run, compare) is required.")
        return 2

    cmd = argv[0]
    rest = argv[1:]
    if cmd == "list":
        return handle_list(rest)
    if cmd == "run":
        return handle_run(rest)
    if cmd == "compare":
        return handle_compare(rest)

    emit_error("UNKNOWN_SUBCOMMAND", f"Unknown subcommand '{cmd}'. Choose from: list, run, compare.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

