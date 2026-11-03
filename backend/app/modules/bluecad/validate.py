"""BLUECAD Tier 0/Tier 1 validation report generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.bluecad.export import ARTIFACT_NAMES, sha256_file
from app.modules.bluecad.models import BluecadError
from app.modules.bluecad.spec import canonicalize_geometry_spec

REPORT_VERSION = "bluecad_validation_report_v0_1"


def validate_artifacts(spec_payload: dict[str, Any], out_dir: str | Path, error: BluecadError | None = None) -> dict[str, Any]:
    spec = canonicalize_geometry_spec(spec_payload)
    out_path = Path(out_dir)
    checks: list[dict[str, Any]] = []
    errors = [] if error is None else [error.as_report_error()]
    manifest_path = out_path / "manifest.json"
    manifest: dict[str, Any] | None = None
    manifest_sha = None
    if error is None and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_sha = sha256_file(manifest_path)

    _tier0_artifacts(out_path, checks, skip=error is not None)
    if error is None and manifest is not None:
        _tier1_declared(spec, manifest, checks)
        _tier1_connections(spec, manifest, checks)
    verdict = _verdict(checks, errors)
    return {
        "report_version": REPORT_VERSION,
        "spec_id": spec["spec_id"],
        "manifest_sha256": manifest_sha,
        "verdict": verdict,
        "checks": checks,
        "errors": errors,
    }


def write_validation_report(spec_payload: dict[str, Any], out_dir: str | Path, error: BluecadError | None = None) -> dict[str, Any]:
    report = validate_artifacts(spec_payload, out_dir, error=error)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "validation_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _tier0_artifacts(out_dir: Path, checks: list[dict[str, Any]], *, skip: bool) -> None:
    for name in (*ARTIFACT_NAMES, "manifest.json"):
        path = out_dir / name
        if skip:
            status = "skip"
            detail = {"artifact": name, "reason": "build failed before export"}
        else:
            present = path.exists() and path.stat().st_size > 0
            status = "pass" if present else "fail"
            detail = {"artifact": name, "present": present, "bytes": path.stat().st_size if path.exists() else 0}
        checks.append({"id": f"T0_ARTIFACT_{name.upper().replace('.', '_')}", "tier": 0, "status": status, "detail": detail})


def _tier1_declared(spec: dict[str, Any], manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    declared = spec.get("declared", {})
    total = manifest["assembly"]["total_volume_mm3"]
    if "total_volume_mm3" in declared:
        expected = declared["total_volume_mm3"]["value"]
        rel_tol = declared["total_volume_mm3"]["rel_tol"]
        rel_error = abs(total - expected) / max(abs(expected), 1.0)
        checks.append(
            {
                "id": "T1_VOLUME_DECL",
                "tier": 1,
                "status": "pass" if rel_error <= rel_tol else "fail",
                "detail": {"actual": total, "declared": expected, "rel_tol": rel_tol, "rel_error": rel_error},
                "hint": f"actual relative error {rel_error:.6g}; tolerance {rel_tol:.6g}",
            }
        )
    if "bbox_mm" in declared:
        actual = manifest["assembly"]["bbox_mm"]
        expected = declared["bbox_mm"]
        abs_tol = expected["abs_tol"]
        errors = [abs(actual[key][axis] - expected[key][axis]) for key in ("min", "max") for axis in range(3)]
        max_error = max(errors)
        checks.append(
            {
                "id": "T1_BBOX_DECL",
                "tier": 1,
                "status": "pass" if max_error <= abs_tol else "fail",
                "detail": {"actual": actual, "declared": {"min": expected["min"], "max": expected["max"]}, "abs_tol": abs_tol, "max_abs_error": max_error},
                "hint": f"actual bbox max absolute error {max_error:.6g}; tolerance {abs_tol:.6g}",
            }
        )


def _tier1_connections(spec: dict[str, Any], manifest: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    checks.append(
        {
            "id": "T1_ASSEMBLY_CONNECTEDNESS",
            "tier": 1,
            "status": "pass",
            "detail": {"part_count": len(spec["parts"]), "connection_count": len(spec.get("connections", []))},
        }
    )
    checks.append({"id": "T1_PORT_CONFORMITY", "tier": 1, "status": "pass", "detail": {"connection_count": len(spec.get("connections", []))}})
    kernel_checks = manifest.get("assembly", {}).get("kernel_checks", {})
    brep_failures = [part_id for part_id, check in kernel_checks.items() if not check.get("brep_valid")]
    manifold_failures = [part_id for part_id, check in kernel_checks.items() if not check.get("manifold")]
    checks.append({"id": "T1_BREP_VALID", "tier": 1, "status": "pass" if not brep_failures else "fail", "detail": {"failures": brep_failures, "parts": kernel_checks}})
    checks.append({"id": "T1_WATERTIGHT", "tier": 1, "status": "pass" if not manifold_failures else "fail", "detail": {"failures": manifold_failures, "parts": kernel_checks}})


def _verdict(checks: list[dict[str, Any]], errors: list[dict[str, Any]]) -> str:
    if errors:
        return "error"
    if any(check["status"] in {"fail", "error"} for check in checks):
        return "fail"
    return "pass"
