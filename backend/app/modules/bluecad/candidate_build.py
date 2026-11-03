"""Shared canonicalize/build/validate/evidence/export path for GeometrySpec candidates."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.modules.bluecad.evidence import record_validation_evidence
from app.modules.bluecad.ledger import (
    candidate_work_dir,
    finish_attempt,
    mark_candidate_valid,
    park_candidate,
    register_artifact,
    register_export_artifacts,
    update_candidate_artifacts,
)
from app.modules.bluecad.service import DEFAULT_TIMEOUT_SECONDS, build_geometry_spec
from app.modules.bluecad.spec import canonical_json


def build_geometry_candidate(
    workspace_id: str,
    candidate_id: str,
    attempt_id: str,
    spec: dict[str, Any],
    *,
    producer_notes: str,
    failure_reason: str,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Build a stored candidate through the canonical deterministic BLUECAD pipeline."""
    out_dir = candidate_work_dir(workspace_id, candidate_id, 1)
    registered: list[str] = []
    attempt_finished = False
    try:
        out_dir.mkdir(parents=True, exist_ok=False)
        spec_path = out_dir / "geometry_spec.json"
        spec_path.write_text(canonical_json(spec) + "\n", encoding="utf-8")
        result = build_geometry_spec(spec, out_dir, timeout_s=timeout_s)
        source_ref = f"bluecad_candidate:{candidate_id}:attempt:1"

        def register(path: Path, role: str) -> str:
            artifact_id = register_artifact(
                workspace_id, path, role=role, source_ref=source_ref, producer_notes=producer_notes
            )
            registered.append(artifact_id)
            return artifact_id

        spec_id = register(spec_path, "bluecad_spec")
        report_path = result.report_path or out_dir / "validation_report.json"
        if not report_path.is_file():
            raise RuntimeError("expected deterministic BLUECAD validation report is missing")
        report_id = register(report_path, "bluecad_report")
        manifest_id = (
            register(result.manifest_path, "bluecad_manifest")
            if result.manifest_path and result.manifest_path.is_file()
            else None
        )
        glb_path = out_dir / "model.glb"
        glb_id = register(glb_path, "bluecad_glb") if glb_path.is_file() else None
        if glb_id:
            registered.extend(
                register_export_artifacts(
                    workspace_id, out_dir, source_ref=source_ref, producer_notes=producer_notes
                ).values()
            )
        verdict = "pass" if result.report.get("verdict") == "pass" else "fail"
        finish_attempt(
            attempt_id,
            proposal_outcome="not_applicable",
            build_outcome="ok" if result.verdict != "error" else _build_error_code(result.errors),
            validation_verdict=verdict,
            spec_artifact_id=spec_id,
            report_artifact_id=report_id,
            manifest_artifact_id=manifest_id,
        )
        attempt_finished = True
        update_candidate_artifacts(
            candidate_id, spec_artifact_id=spec_id, glb_artifact_id=glb_id, report_artifact_id=report_id
        )
        record_validation_evidence(workspace_id, candidate_id, attempt_id, result.report, report_artifact_id=report_id)
        if verdict == "pass":
            mark_candidate_valid(candidate_id)
            return True
        park_candidate(candidate_id, failure_reason, notes="Deterministic GeometrySpec validation failed.")
        return False
    except Exception as exc:
        if not attempt_finished:
            try:
                finish_attempt(
                    attempt_id,
                    proposal_outcome="not_applicable",
                    build_outcome="candidate_build_error",
                    validation_verdict="fail",
                    error_detail={"error_type": type(exc).__name__},
                )
            except Exception:
                pass
        park_candidate(candidate_id, failure_reason, notes=f"candidate_build_error={type(exc).__name__}")
        if not registered and out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        raise


def _build_error_code(errors: list[dict[str, Any]]) -> str:
    if not errors:
        return "error"
    code = errors[0].get("code")
    return str(code).lower() if code else "error"
