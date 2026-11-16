"""Deterministic EvaluationResult adapters for BLUECAD solids, mesh and static FEM (spec 281).

Adapts native BuildResult, Gmsh mesh dict, and CalculiX static FEM summary into
serializable EvaluationResult contracts carrying ResultCheck values. Derives
usability through result_verdict without losing native reports or altering outputs.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.bluecad.export import sha256_file
from app.modules.bluecad.models import BuildResult
from app.modules.bluecad.registry import resolve_tool
from app.modules.engineering.evaluator_contracts import (
    REFUSAL_CATEGORIES,
    CheckStatus,
    EvaluationFailure,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
    ResultCheck,
    failure_category_for_code,
)
from app.modules.engineering.refs import (
    EvaluationRequestRef,
    EvaluationResultRef,
    Quantity,
)

SOLID_EVALUATOR_ID: Final = "bluecad.solid"
MESH_EVALUATOR_ID: Final = "bluecad.mesh"
FEM_EVALUATOR_ID: Final = "bluecad.fem"

CHECK_BREP_VALID: Final = "BREP_VALID"
CHECK_WATERTIGHT: Final = "WATERTIGHT"
CHECK_VOLUME_CONSERVATION: Final = "VOLUME_CONSERVATION"

CHECK_VOLUME_ELEMENTS: Final = "VOLUME_ELEMENTS"
CHECK_ELEMENT_QUALITY: Final = "ELEMENT_QUALITY"

CHECK_SOLVER_COMPLETION: Final = "SOLVER_COMPLETION"
CHECK_FORCE_EQUILIBRIUM: Final = "FORCE_EQUILIBRIUM"

# Policy limit for tetrahedral element quality (gamma measure from Gmsh tetrahedral optimizer).
# Elements with quality below 0.10 are considered poor/degraded for static FEA.
MESH_QUALITY_POLICY_LIMIT: Final = 0.10
MESH_QUALITY_POLICY_SOURCE: Final = "policy:gmsh_gamma_min"

SOLID_TOPOLOGY_SOURCE: Final = "physical_limit"
SOLID_VOLUME_SOURCE: Final = "policy:declared_volume"

FEM_SOLVER_SOURCE: Final = "policy:solver_completion"
FEM_EQUILIBRIUM_SOURCE: Final = "policy:force_equilibrium"
FEM_FORCE_EQUILIBRIUM_TOL_N: Final = 1e-3
FEM_FORCE_EQUILIBRIUM_REL_TOL: Final = 0.01

WORST_AVG_RE = re.compile(
    r"worst\s*=\s*(?P<worst>[0-9.eE+-]+)\s*/\s*average\s*=\s*(?P<average>[0-9.eE+-]+)",
    re.IGNORECASE,
)
HIST_BIN_RE = re.compile(
    r"(?P<low>[0-9.]+)\s*<\s*quality\s*<\s*(?P<high>[0-9.]+)\s*:\s*(?P<count>\d+)\s+elements?",
    re.IGNORECASE,
)


def solid_evaluator_descriptor(registry_path: Path | None = None) -> EvaluatorDescriptor:
    version = "0.11.1"
    try:
        import build123d

        version = getattr(build123d, "__version__", version)
    except ImportError:
        pass
    return EvaluatorDescriptor(
        evaluator_id=SOLID_EVALUATOR_ID,
        backend_kind="cad_kernel",
        backend_name="build123d",
        backend_version=version,
        tool_registry_id="build123d",
        fidelity="screening",
        capabilities=("geometry_build", "brep_export"),
        required_checks=(CHECK_BREP_VALID, CHECK_WATERTIGHT),
        advisory_checks=(CHECK_VOLUME_CONSERVATION,),
    )


def mesh_evaluator_descriptor(registry_path: Path | None = None) -> EvaluatorDescriptor:
    version = "4.15.2"
    try:
        tool = resolve_tool("gmsh", registry_path)
        version = str(tool.get("version_pin") or version)
    except Exception:
        pass
    return EvaluatorDescriptor(
        evaluator_id=MESH_EVALUATOR_ID,
        backend_kind="mesher",
        backend_name="Gmsh",
        backend_version=version,
        tool_registry_id="gmsh",
        fidelity="screening",
        capabilities=("tet_mesh", "physical_groups"),
        required_checks=(CHECK_VOLUME_ELEMENTS, CHECK_ELEMENT_QUALITY),
        advisory_checks=(),
    )


def fem_evaluator_descriptor(registry_path: Path | None = None) -> EvaluatorDescriptor:
    version = "2.22"
    try:
        tool = resolve_tool("calculix", registry_path)
        version = str(tool.get("version_pin") or version)
    except Exception:
        pass
    return EvaluatorDescriptor(
        evaluator_id=FEM_EVALUATOR_ID,
        backend_kind="fem_solver",
        backend_name="CalculiX",
        backend_version=version,
        tool_registry_id="calculix",
        fidelity="field_resolved",
        capabilities=("static",),
        required_checks=(CHECK_SOLVER_COMPLETION, CHECK_FORCE_EQUILIBRIUM),
        advisory_checks=(),
    )


def _synthesize_refs(
    object_id: str | None,
    authority_owner: str,
    workspace_id: str | None = None,
) -> tuple[EvaluationRequestRef, EvaluationResultRef]:
    ws_id = workspace_id or "default"
    obj_id = object_id or "default"
    req_ref = EvaluationRequestRef(
        authority_owner=authority_owner,
        object_type="evaluation_request",
        object_id=obj_id,
        workspace_id=ws_id,
        revision="1",
    )
    res_ref = EvaluationResultRef(
        authority_owner=authority_owner,
        object_type="evaluation_result",
        object_id=f"res-{obj_id}",
        workspace_id=ws_id,
        revision="1",
    )
    return req_ref, res_ref


def _classify_native_failure(
    errors: list[dict[str, Any]],
) -> tuple[EvaluationStatus, EvaluationFailure]:
    if not errors:
        return "failed", EvaluationFailure(category="solver_crash", backend_code="SOLVE_ERROR")
    first = errors[0]
    code = str(first.get("code", "SOLVE_ERROR"))
    detail = first.get("detail")
    message = detail.get("message") if isinstance(detail, dict) else str(detail) if detail else None

    if code == "TIMEOUT":
        return "deadline_exceeded", EvaluationFailure(category="timeout", backend_code=code, message=message)

    category = failure_category_for_code(code)
    if category in REFUSAL_CATEGORIES:
        return "refused", EvaluationFailure(category=category, backend_code=code, message=message)
    if category == "timeout":
        return "deadline_exceeded", EvaluationFailure(category=category, backend_code=code, message=message)
    if category == "cancelled":
        return "cancelled", EvaluationFailure(category=category, backend_code=code, message=message)
    return "failed", EvaluationFailure(category=category, backend_code=code, message=message)


def _solid_artifacts(build_result: BuildResult, workspace_id: str) -> list[SourceRef]:
    artifacts: list[SourceRef] = []
    seen_paths: set[Path] = set()

    for path in (build_result.manifest_path, build_result.report_path):
        if path is not None and path.is_file():
            seen_paths.add(path.resolve())
            digest = f"sha256:{sha256_file(path)}"
            artifacts.append(
                SourceRef(
                    authority_owner="bluecad",
                    object_type="artifact",
                    object_id=path.name,
                    workspace_id=workspace_id,
                    content_digest=digest,
                )
            )

    out_dir = build_result.out_dir
    if out_dir.is_dir():
        for item in sorted(out_dir.iterdir()):
            if item.is_file() and item.resolve() not in seen_paths:
                if item.suffix.lower() in {".step", ".stl", ".glb", ".json"}:
                    seen_paths.add(item.resolve())
                    digest = f"sha256:{sha256_file(item)}"
                    artifacts.append(
                        SourceRef(
                            authority_owner="bluecad",
                            object_type="artifact",
                            object_id=item.name,
                            workspace_id=workspace_id,
                            content_digest=digest,
                        )
                    )
    return artifacts


def solid_build_result_to_evaluation_result(
    build_result: BuildResult,
    *,
    request: EvaluationRequest | None = None,
    workspace_id: str | None = None,
    started_at: datetime | None = None,
) -> EvaluationResult:
    """Convert a native BLUECAD solid BuildResult into a checked EvaluationResult."""
    completed = datetime.now(UTC)
    descriptor = solid_evaluator_descriptor()
    backend_version = descriptor.backend_version

    ws_id = request.request_ref.workspace_id if request else (workspace_id or "default")
    if request:
        req_ref = request.request_ref
        res_ref = EvaluationResultRef(
            authority_owner="bluecad",
            object_type="evaluation_result",
            object_id=f"res-{request.request_ref.object_id}",
            workspace_id=ws_id,
            revision="1",
        )
    else:
        req_ref, res_ref = _synthesize_refs(build_result.spec_id, "bluecad", ws_id)

    # Classify run outcome
    is_error = build_result.verdict == "error" or (build_result.errors and not build_result.manifest)
    if is_error:
        status, failure = _classify_native_failure(build_result.errors)
        checks: list[ResultCheck] = [
            ResultCheck(
                id=CHECK_BREP_VALID,
                family="input",
                status="not_evaluated",
                hint="B-rep validity check not evaluated: build failed",
                source=SOLID_TOPOLOGY_SOURCE,
            ),
            ResultCheck(
                id=CHECK_WATERTIGHT,
                family="input",
                status="not_evaluated",
                hint="watertight check not evaluated: build failed",
                source=SOLID_TOPOLOGY_SOURCE,
            ),
            ResultCheck(
                id=CHECK_VOLUME_CONSERVATION,
                family="conservation",
                status="not_evaluated",
                hint="volume conservation check not evaluated: build failed",
                source=SOLID_VOLUME_SOURCE,
            ),
        ]
        return EvaluationResult(
            result_ref=res_ref,
            request_ref=req_ref,
            evaluator_id=SOLID_EVALUATOR_ID,
            backend_version=backend_version,
            status=status,
            failure=failure,
            fidelity="screening",
            outputs=(),
            output_artifacts=(),
            checks=tuple(checks),
            numerical=NumericalDiagnostics(),
            started_at=started_at,
            completed_at=completed,
        )

    # Built geometry exists
    manifest = build_result.manifest or {}
    assembly = manifest.get("assembly", {})
    outputs_list: list[NamedQuantity] = []
    if "total_volume_mm3" in assembly and assembly["total_volume_mm3"] is not None:
        try:
            total_volume = float(assembly["total_volume_mm3"])
            if math.isfinite(total_volume):
                outputs_list.append(
                    NamedQuantity(name="total_volume", value=Quantity(value=total_volume, unit="mm**3"))
                )
        except (ValueError, TypeError):
            pass

    if "bbox_mm" in assembly and isinstance(assembly["bbox_mm"], dict):
        bbox = assembly["bbox_mm"]
        min_coords = bbox.get("min")
        max_coords = bbox.get("max")
        if (
            min_coords is not None
            and max_coords is not None
            and len(min_coords) == 3
            and len(max_coords) == 3
        ):
            try:
                min_f = [float(x) for x in min_coords]
                max_f = [float(x) for x in max_coords]
                if all(math.isfinite(x) for x in min_f) and all(math.isfinite(x) for x in max_f):
                    outputs_list.extend(
                        [
                            NamedQuantity(name="bbox_min_x", value=Quantity(value=min_f[0], unit="mm")),
                            NamedQuantity(name="bbox_min_y", value=Quantity(value=min_f[1], unit="mm")),
                            NamedQuantity(name="bbox_min_z", value=Quantity(value=min_f[2], unit="mm")),
                            NamedQuantity(name="bbox_max_x", value=Quantity(value=max_f[0], unit="mm")),
                            NamedQuantity(name="bbox_max_y", value=Quantity(value=max_f[1], unit="mm")),
                            NamedQuantity(name="bbox_max_z", value=Quantity(value=max_f[2], unit="mm")),
                            NamedQuantity(name="bbox_dx", value=Quantity(value=max_f[0] - min_f[0], unit="mm")),
                            NamedQuantity(name="bbox_dy", value=Quantity(value=max_f[1] - min_f[1], unit="mm")),
                            NamedQuantity(name="bbox_dz", value=Quantity(value=max_f[2] - min_f[2], unit="mm")),
                        ]
                    )
            except (ValueError, TypeError):
                pass

    outputs = tuple(outputs_list)

    # Topology checks
    report_checks = {c["id"]: c for c in build_result.report.get("checks", [])}
    kernel_checks = assembly.get("kernel_checks", {})

    brep_status: CheckStatus
    if "T1_BREP_VALID" in report_checks:
        brep_ok = report_checks["T1_BREP_VALID"]["status"] == "pass"
        brep_status = "pass" if brep_ok else "fail"
        brep_hint = "solid B-rep topology is valid" if brep_ok else "solid B-rep topology contains invalid parts"
    elif kernel_checks:
        brep_ok = all(check.get("brep_valid") for check in kernel_checks.values())
        brep_status = "pass" if brep_ok else "fail"
        brep_hint = "solid B-rep topology is valid" if brep_ok else "solid B-rep topology contains invalid parts"
    else:
        brep_status = "not_evaluated"
        brep_hint = "B-rep validity check not evaluated: no validation report or kernel checks"

    watertight_status: CheckStatus
    if "T1_WATERTIGHT" in report_checks:
        watertight_ok = report_checks["T1_WATERTIGHT"]["status"] == "pass"
        watertight_status = "pass" if watertight_ok else "fail"
        watertight_hint = "solid is manifold and watertight" if watertight_ok else "solid is open or non-manifold"
    elif kernel_checks:
        watertight_ok = all(check.get("manifold") for check in kernel_checks.values())
        watertight_status = "pass" if watertight_ok else "fail"
        watertight_hint = "solid is manifold and watertight" if watertight_ok else "solid is open or non-manifold"
    else:
        watertight_status = "not_evaluated"
        watertight_hint = "watertight check not evaluated: no validation report or kernel checks"

    checks_list: list[ResultCheck] = [
        ResultCheck(
            id=CHECK_BREP_VALID,
            family="input",
            status=brep_status,
            hint=brep_hint,
            source=SOLID_TOPOLOGY_SOURCE,
        ),
        ResultCheck(
            id=CHECK_WATERTIGHT,
            family="input",
            status=watertight_status,
            hint=watertight_hint,
            source=SOLID_TOPOLOGY_SOURCE,
        ),
    ]

    # Declared volume check: map only native T1_VOLUME_DECL check
    if "T1_VOLUME_DECL" in report_checks:
        t1_vol = report_checks["T1_VOLUME_DECL"]
        detail = t1_vol.get("detail")
        rel_error = None
        rel_tol = None
        if isinstance(detail, dict):
            raw_err = detail.get("rel_error")
            raw_tol = detail.get("rel_tol")
            try:
                if raw_err is not None:
                    err_val = float(raw_err)
                    if math.isfinite(err_val):
                        rel_error = err_val
                if raw_tol is not None:
                    tol_val = float(raw_tol)
                    if math.isfinite(tol_val):
                        rel_tol = tol_val
            except (ValueError, TypeError):
                pass

        if rel_error is not None and rel_tol is not None:
            is_pass = t1_vol.get("status") == "pass"
            checks_list.append(
                ResultCheck(
                    id=CHECK_VOLUME_CONSERVATION,
                    family="conservation",
                    status="pass" if is_pass else "fail",
                    actual=rel_error,
                    limit=rel_tol,
                    unit="1",
                    hint=(
                        f"declared volume relative error {rel_error:.6g} within tolerance {rel_tol:.6g}"
                        if is_pass
                        else f"declared volume relative error {rel_error:.6g} exceeds tolerance {rel_tol:.6g}"
                    ),
                    source=SOLID_VOLUME_SOURCE,
                )
            )
        else:
            checks_list.append(
                ResultCheck(
                    id=CHECK_VOLUME_CONSERVATION,
                    family="conservation",
                    status="not_evaluated",
                    hint="declared volume check lacks finite relative error or tolerance",
                    source=SOLID_VOLUME_SOURCE,
                )
            )
    else:
        checks_list.append(
            ResultCheck(
                id=CHECK_VOLUME_CONSERVATION,
                family="conservation",
                status="not_evaluated",
                hint="no volume was declared in specification",
                source=SOLID_VOLUME_SOURCE,
            )
        )

    artifacts = tuple(_solid_artifacts(build_result, ws_id))

    return EvaluationResult(
        result_ref=res_ref,
        request_ref=req_ref,
        evaluator_id=SOLID_EVALUATOR_ID,
        backend_version=backend_version,
        status="succeeded",
        fidelity="screening",
        outputs=outputs,
        output_artifacts=artifacts,
        checks=tuple(checks_list),
        numerical=NumericalDiagnostics(),
        started_at=started_at,
        completed_at=completed,
    )


def _parse_gmsh_quality(
    log_text: str,
) -> tuple[float | None, float | None, int | None]:
    matches = list(WORST_AVG_RE.finditer(log_text))
    if not matches:
        return None, None, None
    last_match = matches[-1]
    worst = float(last_match.group("worst"))
    average = float(last_match.group("average"))
    if not (math.isfinite(worst) and math.isfinite(average)):
        return None, None, None

    tail_text = log_text[last_match.end() :]
    below_limit = 0
    histogram_found = False
    for bin_match in HIST_BIN_RE.finditer(tail_text):
        histogram_found = True
        high = float(bin_match.group("high"))
        count = int(bin_match.group("count"))
        if high <= MESH_QUALITY_POLICY_LIMIT + 1e-9:
            below_limit += count

    below_limit_count = below_limit if histogram_found else None
    return worst, average, below_limit_count


def _resolve_mesh_quality(
    mesh_result: dict[str, Any],
) -> tuple[float | None, float | None, int | None]:
    attempts = mesh_result.get("attempts", [])
    candidate_artifacts = [
        mesh_result.get("artifacts", {}),
        *(att.get("artifacts", {}) for att in reversed(attempts)),
    ]

    for arts in candidate_artifacts:
        log_meta = arts.get("gmsh_log")
        if isinstance(log_meta, dict) and "path" in log_meta:
            log_path = Path(log_meta["path"])
            if log_path.is_file():
                try:
                    q_min, q_avg, q_below = _parse_gmsh_quality(
                        log_path.read_text(encoding="utf-8", errors="replace")
                    )
                    if q_min is not None:
                        return q_min, q_avg, q_below
                except OSError:
                    pass

    return None, None, None


def _normalize_digest(raw_digest: Any, file_path: Path | None = None) -> str:
    if file_path is not None and file_path.is_file():
        return f"sha256:{sha256_file(file_path)}"
    val = str(raw_digest or "")
    hex_part = val[7:] if val.startswith("sha256:") else val
    if len(hex_part) == 64 and all(c in "0123456789abcdefABCDEF" for c in hex_part):
        return f"sha256:{hex_part.lower()}"
    return f"sha256:{hashlib.sha256(val.encode('utf-8')).hexdigest()}"


def _mesh_artifacts(mesh_result: dict[str, Any], workspace_id: str) -> list[SourceRef]:
    artifacts: list[SourceRef] = []
    raw_artifacts = mesh_result.get("artifacts", {})
    for role, item in sorted(raw_artifacts.items()):
        if isinstance(item, dict):
            path_str = item.get("path", role)
            path = Path(path_str) if path_str else None
            digest = _normalize_digest(item.get("sha256"), path)
            filename = (path.name if path else None) or role
            artifacts.append(
                SourceRef(
                    authority_owner="bluecad",
                    object_type="artifact",
                    object_id=filename,
                    workspace_id=workspace_id,
                    content_digest=digest,
                )
            )
    return artifacts


def mesh_result_to_evaluation_result(
    mesh_result: dict[str, Any],
    analysis_spec: dict[str, Any] | None = None,
    *,
    request: EvaluationRequest | None = None,
    workspace_id: str | None = None,
    started_at: datetime | None = None,
) -> EvaluationResult:
    """Convert a native BLUECAD Gmsh mesh_analysis_spec dict into a checked EvaluationResult."""
    completed = datetime.now(UTC)
    descriptor = mesh_evaluator_descriptor()
    backend_version = descriptor.backend_version

    ws_id = request.request_ref.workspace_id if request else (workspace_id or "default")
    object_id = (analysis_spec or {}).get("analysis_id") or "mesh"
    if request:
        req_ref = request.request_ref
        res_ref = EvaluationResultRef(
            authority_owner="bluecad",
            object_type="evaluation_result",
            object_id=f"res-{request.request_ref.object_id}",
            workspace_id=ws_id,
            revision="1",
        )
    else:
        req_ref, res_ref = _synthesize_refs(object_id, "bluecad", ws_id)

    errors = mesh_result.get("errors", [])
    verdict = mesh_result.get("verdict", "error")

    # Handle native mesh failure paths
    if verdict in {"fail", "error"} or errors:
        status, failure = _classify_native_failure(errors)
        checks: list[ResultCheck] = [
            ResultCheck(
                id=CHECK_VOLUME_ELEMENTS,
                family="discretisation",
                status="not_evaluated",
                hint=f"volume elements not evaluated: mesh generation failed ({failure.backend_code})",
                source="policy:mesh_volume_elements",
            ),
            ResultCheck(
                id=CHECK_ELEMENT_QUALITY,
                family="discretisation",
                status="not_evaluated",
                hint="element quality not evaluated: mesh generation failed",
                source=MESH_QUALITY_POLICY_SOURCE,
            ),
        ]
        return EvaluationResult(
            result_ref=res_ref,
            request_ref=req_ref,
            evaluator_id=MESH_EVALUATOR_ID,
            backend_version=backend_version,
            status=status,
            failure=failure,
            fidelity="screening",
            outputs=(),
            output_artifacts=(),
            checks=tuple(checks),
            numerical=NumericalDiagnostics(),
            started_at=started_at,
            completed_at=completed,
        )

    attempts = mesh_result.get("attempts", [])
    latest_attempt = attempts[-1] if attempts else {}
    counts = latest_attempt.get("counts", {})
    elements_total = int(counts.get("elements_total", 0))
    nodes_total = int(counts.get("nodes_total", 0))

    q_min, q_avg, q_below = _resolve_mesh_quality(mesh_result)

    outputs_list: list[NamedQuantity] = [
        NamedQuantity(name="elements_total", value=Quantity(value=float(elements_total), unit="1")),
        NamedQuantity(name="nodes_total", value=Quantity(value=float(nodes_total), unit="1")),
    ]
    if q_min is not None and q_avg is not None:
        outputs_list.extend(
            [
                NamedQuantity(name="mesh_quality_min", value=Quantity(value=q_min, unit="1")),
                NamedQuantity(name="mesh_quality_mean", value=Quantity(value=q_avg, unit="1")),
            ]
        )
        if q_below is not None:
            outputs_list.append(
                NamedQuantity(name="mesh_quality_below_limit", value=Quantity(value=float(q_below), unit="1")),
            )

    # Checks
    checks_list: list[ResultCheck] = []
    has_vol_elements = elements_total > 0
    checks_list.append(
        ResultCheck(
            id=CHECK_VOLUME_ELEMENTS,
            family="discretisation",
            status="pass" if has_vol_elements else "fail",
            actual=float(elements_total),
            limit=1.0,
            unit="1",
            hint=f"{elements_total} volume elements in mesh" if has_vol_elements else "mesh contains zero volume elements",
            source="policy:mesh_volume_elements",
        )
    )

    if q_min is not None and q_avg is not None:
        q_pass = q_min >= MESH_QUALITY_POLICY_LIMIT and (q_below is None or q_below == 0)
        if q_below is not None:
            hint = (
                f"minimum Gmsh gamma quality {q_min:.4f} >= limit {MESH_QUALITY_POLICY_LIMIT:.2f} (mean {q_avg:.4f}, {q_below} below limit)"
                if q_pass
                else f"minimum Gmsh gamma quality {q_min:.4f} < limit {MESH_QUALITY_POLICY_LIMIT:.2f} (mean {q_avg:.4f}, {q_below} below limit)"
            )
        else:
            hint = (
                f"minimum Gmsh gamma quality {q_min:.4f} >= limit {MESH_QUALITY_POLICY_LIMIT:.2f} (mean {q_avg:.4f})"
                if q_pass
                else f"minimum Gmsh gamma quality {q_min:.4f} < limit {MESH_QUALITY_POLICY_LIMIT:.2f} (mean {q_avg:.4f})"
            )
        checks_list.append(
            ResultCheck(
                id=CHECK_ELEMENT_QUALITY,
                family="discretisation",
                status="pass" if q_pass else "fail",
                actual=q_min,
                limit=MESH_QUALITY_POLICY_LIMIT,
                unit="1",
                hint=hint,
                source=MESH_QUALITY_POLICY_SOURCE,
            )
        )
    else:
        # Quality could not be parsed; do not invent a value.
        checks_list.append(
            ResultCheck(
                id=CHECK_ELEMENT_QUALITY,
                family="discretisation",
                status="not_evaluated",
                hint="mesh element quality not reported by mesher",
                source=MESH_QUALITY_POLICY_SOURCE,
            )
        )

    artifacts = tuple(_mesh_artifacts(mesh_result, ws_id))

    return EvaluationResult(
        result_ref=res_ref,
        request_ref=req_ref,
        evaluator_id=MESH_EVALUATOR_ID,
        backend_version=backend_version,
        status="succeeded",
        fidelity="screening",
        outputs=tuple(outputs_list),
        output_artifacts=artifacts,
        checks=tuple(checks_list),
        numerical=NumericalDiagnostics(),
        started_at=started_at,
        completed_at=completed,
    )


def _evaluate_force_equilibrium(
    fem_result: dict[str, Any],
    analysis_spec: dict[str, Any] | None,
) -> ResultCheck:
    """Evaluate static force equilibrium if applied load resultant and all support reactions are available."""
    hint_incomplete = "force equilibrium not evaluated: incomplete load resultant or multiple support reaction sets"
    if analysis_spec is None:
        return ResultCheck(
            id=CHECK_FORCE_EQUILIBRIUM,
            family="conservation",
            status="not_evaluated",
            hint="force equilibrium not evaluated: no analysis specification provided",
            source=FEM_EQUILIBRIUM_SOURCE,
        )

    bcs = analysis_spec.get("bcs", [])
    if len(bcs) != 1 or bcs[0].get("kind", "fixed") != "fixed":
        return ResultCheck(
            id=CHECK_FORCE_EQUILIBRIUM,
            family="conservation",
            status="not_evaluated",
            hint=hint_incomplete,
            source=FEM_EQUILIBRIUM_SOURCE,
        )

    loads = analysis_spec.get("loads", [])
    if not loads:
        return ResultCheck(
            id=CHECK_FORCE_EQUILIBRIUM,
            family="conservation",
            status="not_evaluated",
            hint=hint_incomplete,
            source=FEM_EQUILIBRIUM_SOURCE,
        )

    total_applied = [0.0, 0.0, 0.0]
    for load in loads:
        load_type = load.get("type", "force_total")
        if load_type == "force_total":
            f_vec = load.get("force", load.get("vector_n"))
            if not f_vec or len(f_vec) != 3 or not all(math.isfinite(v) for v in f_vec):
                return ResultCheck(
                    id=CHECK_FORCE_EQUILIBRIUM,
                    family="conservation",
                    status="not_evaluated",
                    hint=hint_incomplete,
                    source=FEM_EQUILIBRIUM_SOURCE,
                )
            for i in range(3):
                total_applied[i] += float(f_vec[i])
        elif load_type == "pressure":
            return ResultCheck(
                id=CHECK_FORCE_EQUILIBRIUM,
                family="conservation",
                status="not_evaluated",
                hint="force equilibrium not evaluated: pressure load lacks integrated resultant",
                source=FEM_EQUILIBRIUM_SOURCE,
            )
        else:
            return ResultCheck(
                id=CHECK_FORCE_EQUILIBRIUM,
                family="conservation",
                status="not_evaluated",
                hint=hint_incomplete,
                source=FEM_EQUILIBRIUM_SOURCE,
            )

    reactions = fem_result.get("reactions")
    reaction_resultant = fem_result.get("reaction_resultant")
    if reaction_resultant is None and reactions:
        reaction_resultant = [sum(item["force"][i] for item in reactions) for i in range(3)]

    if reaction_resultant is None or len(reaction_resultant) != 3 or not reactions:
        return ResultCheck(
            id=CHECK_FORCE_EQUILIBRIUM,
            family="conservation",
            status="not_evaluated",
            hint="force equilibrium not evaluated: support reactions missing or unparsed",
            source=FEM_EQUILIBRIUM_SOURCE,
        )

    if not all(math.isfinite(v) for v in reaction_resultant):
        return ResultCheck(
            id=CHECK_FORCE_EQUILIBRIUM,
            family="conservation",
            status="not_evaluated",
            hint="force equilibrium not evaluated: non-finite support reactions",
            source=FEM_EQUILIBRIUM_SOURCE,
        )

    res_vec = [total_applied[i] + reaction_resultant[i] for i in range(3)]
    res_mag = math.sqrt(sum(v * v for v in res_vec))
    f_mag = math.sqrt(sum(v * v for v in total_applied))
    norm_res = res_mag / max(f_mag, 1.0)

    tolerance_n = max(FEM_FORCE_EQUILIBRIUM_REL_TOL * f_mag, FEM_FORCE_EQUILIBRIUM_TOL_N)

    if res_mag <= tolerance_n:
        return ResultCheck(
            id=CHECK_FORCE_EQUILIBRIUM,
            family="conservation",
            status="pass",
            actual=res_mag,
            limit=tolerance_n,
            unit="N",
            hint=f"force equilibrium residual {res_mag:.4e} N within tolerance {tolerance_n:.4e} N (norm {norm_res:.4e})",
            source=FEM_EQUILIBRIUM_SOURCE,
        )
    return ResultCheck(
        id=CHECK_FORCE_EQUILIBRIUM,
        family="conservation",
        status="fail",
        actual=res_mag,
        limit=tolerance_n,
        unit="N",
        hint=f"force equilibrium residual {res_mag:.4e} N exceeds tolerance {tolerance_n:.4e} N (norm {norm_res:.4e})",
        source=FEM_EQUILIBRIUM_SOURCE,
    )


def _fem_artifacts(fem_result: dict[str, Any], workspace_id: str) -> list[SourceRef]:
    artifacts: list[SourceRef] = []
    raw_artifacts = fem_result.get("artifacts", {})
    for role, item in sorted(raw_artifacts.items()):
        if isinstance(item, dict):
            path_str = item.get("path", role)
            path = Path(path_str) if path_str else None
            digest = _normalize_digest(item.get("sha256"), path)
            filename = (path.name if path else None) or role
            artifacts.append(
                SourceRef(
                    authority_owner="bluecad",
                    object_type="artifact",
                    object_id=filename,
                    workspace_id=workspace_id,
                    content_digest=digest,
                )
            )
    return artifacts


def fem_result_to_evaluation_result(
    fem_result: dict[str, Any],
    analysis_spec: dict[str, Any] | None = None,
    *,
    request: EvaluationRequest | None = None,
    workspace_id: str | None = None,
    started_at: datetime | None = None,
) -> EvaluationResult:
    """Convert a native BLUECAD CalculiX solve_static_analysis dict into a checked EvaluationResult."""
    completed = datetime.now(UTC)
    descriptor = fem_evaluator_descriptor()
    backend_version = descriptor.backend_version

    ws_id = request.request_ref.workspace_id if request else (workspace_id or "default")
    object_id = (analysis_spec or {}).get("analysis_id") or "fem"
    if request:
        req_ref = request.request_ref
        res_ref = EvaluationResultRef(
            authority_owner="bluecad",
            object_type="evaluation_result",
            object_id=f"res-{request.request_ref.object_id}",
            workspace_id=ws_id,
            revision="1",
        )
    else:
        req_ref, res_ref = _synthesize_refs(object_id, "bluecad", ws_id)

    errors = fem_result.get("errors", [])
    verdict = fem_result.get("verdict", "error")

    # Handle native solver failure paths
    if verdict in {"fail", "error"} or errors:
        status, failure = _classify_native_failure(errors)
        checks: list[ResultCheck] = [
            ResultCheck(
                id=CHECK_SOLVER_COMPLETION,
                family="numerical",
                status="fail",
                hint=f"FEM solver did not complete successfully: {failure.backend_code}",
                source=FEM_SOLVER_SOURCE,
            ),
            ResultCheck(
                id=CHECK_FORCE_EQUILIBRIUM,
                family="conservation",
                status="not_evaluated",
                hint="force equilibrium not evaluated: solver did not complete",
                source=FEM_EQUILIBRIUM_SOURCE,
            ),
        ]
        return EvaluationResult(
            result_ref=res_ref,
            request_ref=req_ref,
            evaluator_id=FEM_EVALUATOR_ID,
            backend_version=backend_version,
            status=status,
            failure=failure,
            fidelity="field_resolved",
            outputs=(),
            output_artifacts=(),
            checks=tuple(checks),
            numerical=NumericalDiagnostics(converged=False if failure.category == "did_not_converge" else None),
            started_at=started_at,
            completed_at=completed,
        )

    # Solver completed successfully
    outputs_list: list[NamedQuantity] = []
    if "max_displacement" in fem_result:
        u_val = float(fem_result["max_displacement"]["value"])
        outputs_list.append(NamedQuantity(name="max_displacement", value=Quantity(value=u_val, unit="mm")))

    if "max_von_mises" in fem_result:
        vm_val = float(fem_result["max_von_mises"]["value"])
        outputs_list.append(NamedQuantity(name="max_von_mises", value=Quantity(value=vm_val, unit="MPa")))

    reactions = fem_result.get("reactions", [])
    reaction_resultant = fem_result.get("reaction_resultant")
    if reaction_resultant is None and reactions:
        reaction_resultant = [sum(item["force"][i] for item in reactions) for i in range(3)]
    if reaction_resultant and len(reaction_resultant) == 3:
        outputs_list.extend(
            [
                NamedQuantity(name="reaction_resultant_x", value=Quantity(value=float(reaction_resultant[0]), unit="N")),
                NamedQuantity(name="reaction_resultant_y", value=Quantity(value=float(reaction_resultant[1]), unit="N")),
                NamedQuantity(name="reaction_resultant_z", value=Quantity(value=float(reaction_resultant[2]), unit="N")),
            ]
        )

    checks_list = [
        ResultCheck(
            id=CHECK_SOLVER_COMPLETION,
            family="numerical",
            status="pass",
            hint="CalculiX static solver completed successfully",
            source=FEM_SOLVER_SOURCE,
        ),
        _evaluate_force_equilibrium(fem_result, analysis_spec),
    ]

    artifacts = tuple(_fem_artifacts(fem_result, ws_id))

    return EvaluationResult(
        result_ref=res_ref,
        request_ref=req_ref,
        evaluator_id=FEM_EVALUATOR_ID,
        backend_version=backend_version,
        status="succeeded",
        fidelity="field_resolved",
        outputs=tuple(outputs_list),
        output_artifacts=artifacts,
        checks=tuple(checks_list),
        numerical=NumericalDiagnostics(converged=True),
        started_at=started_at,
        completed_at=completed,
    )
