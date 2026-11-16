"""Headless bench command over engineering evaluators (spec 282).

Provides a single CLI entry point to list evaluators, run an evaluation
into an isolated output folder with bounded stdout view, and compare two runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from app.modules.engineering.operator_service import evaluator_registry
from app.modules.engineering.refs import EvaluationResultRef

BLUECAD_EVALUATORS = {"bluecad.solid", "bluecad.mesh", "bluecad.fem"}
MAX_VIEW_CHARS = 8000


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

    return formatted[:MAX_VIEW_CHARS]


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
        "bluecad.solid": (solid_evaluator_descriptor(reg_path), "geometry_spec"),
        "bluecad.mesh": (mesh_evaluator_descriptor(reg_path), "analysis_spec"),
        "bluecad.fem": (fem_evaluator_descriptor(reg_path), "analysis_spec"),
    }

    for evaluator_id, (desc, req_kind) in descriptors.items():
        state, reason, version = _check_bluecad_availability(evaluator_id, reg_path)
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


def _run_bluecad_mesh(
    request_data: dict[str, Any],
    request_path: Path,
    out_dir: Path,
    root: Path,
    tool_registry: Path | None,
    timeout_s: float,
) -> tuple[EvaluationResult, str]:
    geometry = request_data.get("geometry")
    if not isinstance(geometry, dict):
        raise BenchInputError("INVALID_REQUEST", "bluecad.mesh request requires a 'geometry' mapping.")

    step_raw = geometry.get("step_path")
    manifest_raw = geometry.get("manifest_path")
    if not step_raw or not manifest_raw:
        raise BenchInputError(
            "INVALID_REQUEST",
            "bluecad.mesh geometry requires both 'step_path' and 'manifest_path'.",
        )

    req_dir = request_path.parent
    step_path = (req_dir / step_raw).resolve() if not Path(step_raw).is_absolute() else Path(step_raw).resolve()
    manifest_path = (
        (req_dir / manifest_raw).resolve() if not Path(manifest_raw).is_absolute() else Path(manifest_raw).resolve()
    )

    _check_within_root(step_path, root, "geometry.step_path")
    _check_within_root(manifest_path, root, "geometry.manifest_path")

    if not step_path.is_file():
        raise BenchInputError("INPUT_NOT_FOUND", f"Geometry step_path not found: '{step_path}'")
    if not manifest_path.is_file():
        raise BenchInputError("INPUT_NOT_FOUND", f"Geometry manifest_path not found: '{manifest_path}'")

    analysis_spec = dict(request_data)
    analysis_spec["geometry"] = dict(geometry)
    analysis_spec["geometry"]["step_path"] = str(step_path)
    analysis_spec["geometry"]["manifest_path"] = str(manifest_path)

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

        registry = evaluator_registry()
        evaluator_id = args.evaluator_id

        if evaluator_id not in registry and evaluator_id not in BLUECAD_EVALUATORS:
            raise BenchInputError(
                "UNKNOWN_EVALUATOR",
                f"Unknown evaluator '{evaluator_id}'. Run 'bench list' to see available evaluators.",
            )

        if evaluator_id in registry:
            result, verdict = _run_registry_evaluator(evaluator_id, registry[evaluator_id], req_data, out_path)
        elif evaluator_id == "bluecad.solid":
            result, verdict = _run_bluecad_solid(req_path, out_path, reg_path, args.timeout_s)
        elif evaluator_id == "bluecad.mesh":
            result, verdict = _run_bluecad_mesh(req_data, req_path, out_path, root, reg_path, args.timeout_s)
        elif evaluator_id == "bluecad.fem":
            result, verdict = _run_bluecad_fem(
                req_data, args.from_dir, out_path, root, reg_path, args.timeout_s
            )
        else:
            raise BenchInputError("UNKNOWN_EVALUATOR", f"Unhandled evaluator '{evaluator_id}'")

    except BenchInputError as exc:
        emit_error(exc.code, exc.hint)
        return 2

    # Write out_path / result.json
    out_path.mkdir(parents=True, exist_ok=True)
    res_dict = result.model_dump(mode="json")
    res_dict["verdict"] = verdict
    result_file = out_path / "result.json"
    result_file.write_text(json.dumps(res_dict, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Output bounded view
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
    print(view_text)

    # Exit codes:
    # 0 for usable and usable_with_warnings
    # 1 for unchecked and not_usable
    # 2 for a result that is not succeeded and for any usage or input error
    if result.status != "succeeded":
        return 2
    if verdict in {"usable", "usable_with_warnings"}:
        return 0
    return 1


def handle_compare(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="bench compare")
    parser.add_argument("dir_a", type=Path)
    parser.add_argument("dir_b", type=Path)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)

    root = Path.cwd() if args.root is None else args.root.resolve()

    try:
        dir_a = _check_within_root(args.dir_a, root, "dirA")
        dir_b = _check_within_root(args.dir_b, root, "dirB")

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

    except BenchInputError as exc:
        emit_error(exc.code, exc.hint)
        return 2

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
        compare_doc["truncated"] = True
        # Trim checks if needed
        compare_doc["checks"] = {
            k: v for k, v in list(checks_comparison.items())[:10]
        }
        formatted = json.dumps(compare_doc, indent=2)
        if len(formatted) > MAX_VIEW_CHARS:
            formatted = formatted[:MAX_VIEW_CHARS]

    print(formatted)
    return 0


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
