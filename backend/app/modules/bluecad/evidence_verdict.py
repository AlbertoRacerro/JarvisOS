"""Compute shared evaluation verdicts and checks for BLUECAD evidence records (spec 301).

Derives an honest verdict ('usable', 'usable_with_warnings', 'unchecked', 'not_usable')
on read from stored native reports and simulation specifications using the existing
engineering evaluator result adapters. Never persists verdicts; safe and path-free.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.paths import build_paths
from app.modules.bluecad.models import BuildResult
from app.modules.bluecad.result_adapters import (
    fem_evaluator_descriptor,
    fem_result_to_evaluation_result,
    mesh_evaluator_descriptor,
    mesh_result_to_evaluation_result,
    solid_build_result_to_evaluation_result,
    solid_evaluator_descriptor,
)
from app.modules.engineering.evaluator_contracts import (
    EvaluationResult,
    ResultCheck,
    Verdict,
    result_verdict,
)

if TYPE_CHECKING:
    from app.modules.bluecad.evidence import EvidenceRecord
    from app.modules.bluecad.models import BluecadCandidateRead
    from app.modules.bluecad.read_model import BluecadEvidenceCheckRead


PATH_PATTERN = re.compile(r"(?<![\w.)\]])(?:\.\.[/\\]|/|[A-Za-z]:\\)(?:[^\s\"'/\\]+[/\\])*([^\s\"'/\\]+)")


def _clean_detail(hint: str) -> str:
    cleaned = PATH_PATTERN.sub(r"\1", str(hint))
    cleaned = cleaned.replace("\\", "/")
    cleaned = re.sub(r"/(?:[^\s\"']+/)+([^\s\"']*)", r"\1", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned


def _clean_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    cleaned = _clean_detail(reason)
    return cleaned if cleaned else None


def _resolve_stored_path(stored_path_value: object) -> Path | None:
    try:
        raw_path = Path(str(stored_path_value))
        data_root = build_paths().data_root.resolve()
        resolved = (data_root / raw_path if not raw_path.is_absolute() else raw_path).resolve()
        resolved.relative_to(data_root)
        if resolved.exists() and resolved.is_file():
            return resolved
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _load_artifact_json(stored_path: Path) -> dict[str, Any] | None:
    try:
        content = stored_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _extract_checks(
    checks: tuple[ResultCheck, ...],
    required_checks: tuple[str, ...],
) -> list[BluecadEvidenceCheckRead]:
    from app.modules.bluecad.read_model import BluecadEvidenceCheckRead

    req_set = set(required_checks)
    result: list[BluecadEvidenceCheckRead] = []
    for c in checks[:30]:
        detail = _clean_detail(c.hint)
        result.append(
            BluecadEvidenceCheckRead(
                name=c.id,
                family=c.family,
                status=c.status,
                required=c.id in req_set,
                detail=detail,
            )
        )
    return result


def _verdict_reason_for(verdict: Verdict, eval_res: EvaluationResult) -> str | None:
    if verdict == "usable":
        return None
    if verdict == "usable_with_warnings":
        return "Advisory checks warned or not evaluated"
    if verdict == "not_usable":
        if eval_res.status != "succeeded":
            return f"Evaluation status {eval_res.status}"
        return "One or more required checks failed"
    if verdict == "unchecked":
        return "Required checks not evaluated"
    return None


def compute_evidence_verdicts(
    connection: sqlite3.Connection,
    workspace_id: str,
    candidate: BluecadCandidateRead,
    records: list[EvidenceRecord],
) -> dict[str, tuple[Verdict, str | None, list[BluecadEvidenceCheckRead]]]:
    """Compute shared verdicts, short reasons and checks for evidence records."""
    if not records:
        return {}

    attempts_by_id = {attempt.id: attempt for attempt in candidate.attempts}
    needed_artifact_ids: set[str] = set()
    needed_run_ids: set[str] = set()

    for r in records:
        if r.kind == "validation_v0":
            if r.report_artifact_id:
                needed_artifact_ids.add(r.report_artifact_id)
            attempt = attempts_by_id.get(r.attempt_id) if r.attempt_id else None
            if attempt and attempt.manifest_artifact_id:
                needed_artifact_ids.add(attempt.manifest_artifact_id)
        elif r.kind in {"mesh_quality_v0", "fem_static_v0"}:
            if r.report_artifact_id:
                needed_artifact_ids.add(r.report_artifact_id)
            if r.source_run_id:
                needed_run_ids.add(r.source_run_id)

    artifacts_data: dict[str, tuple[dict[str, Any] | None, Path | None]] = {}
    if needed_artifact_ids:
        placeholders = ", ".join("?" for _ in needed_artifact_ids)
        rows = connection.execute(
            f"SELECT id, stored_path FROM artifacts WHERE workspace_id = ? AND id IN ({placeholders})",
            (workspace_id, *needed_artifact_ids),
        ).fetchall()
        for row in rows:
            art_id = str(row["id"])
            path = _resolve_stored_path(row["stored_path"])
            if path is not None:
                data = _load_artifact_json(path)
                artifacts_data[art_id] = (data, path)

    runs_spec: dict[str, dict[str, Any] | None] = {}
    if needed_run_ids:
        placeholders = ", ".join("?" for _ in needed_run_ids)
        rows = connection.execute(
            f"SELECT id, parameter_payload FROM simulation_runs WHERE workspace_id = ? AND id IN ({placeholders})",
            (workspace_id, *needed_run_ids),
        ).fetchall()
        for row in rows:
            run_id = str(row["id"])
            raw_param = row["parameter_payload"]
            if raw_param is not None:
                try:
                    spec = json.loads(str(raw_param))
                    if isinstance(spec, dict):
                        runs_spec[run_id] = spec
                except Exception:
                    pass

    verdicts: dict[str, tuple[Verdict, str | None, list[BluecadEvidenceCheckRead]]] = {}

    for r in records:
        if r.kind == "validation_v0":
            attempt = attempts_by_id.get(r.attempt_id) if r.attempt_id else None
            if attempt is None or not attempt.manifest_artifact_id:
                verdicts[r.id] = ("unchecked", "Manifest artifact missing", [])
                continue
            report_entry = artifacts_data.get(r.report_artifact_id)
            if not report_entry or report_entry[0] is None:
                verdicts[r.id] = ("unchecked", "Validation report artifact unreadable or missing", [])
                continue
            manifest_entry = artifacts_data.get(attempt.manifest_artifact_id)
            if not manifest_entry or manifest_entry[0] is None:
                verdicts[r.id] = ("unchecked", "Manifest artifact unreadable or missing", [])
                continue
            report_dict, report_path = report_entry
            manifest_dict, manifest_path = manifest_entry

            try:
                spec_id = str(report_dict.get("spec_id") or manifest_dict.get("spec_id") or candidate.id)
                out_dir = manifest_path.parent if manifest_path else (report_path.parent if report_path else Path("."))
                verdict_val = report_dict.get("verdict", "error")
                errors_val = report_dict.get("errors", [])
                build_res = BuildResult(
                    spec_id=spec_id,
                    out_dir=out_dir,
                    manifest_path=manifest_path,
                    report_path=report_path,
                    manifest=manifest_dict,
                    report=report_dict,
                    verdict=verdict_val,
                    errors=errors_val if isinstance(errors_val, list) else [],
                )
                eval_res = solid_build_result_to_evaluation_result(build_res, workspace_id=workspace_id)
                desc = solid_evaluator_descriptor()
                verd = result_verdict(desc, eval_res)
                checks = _extract_checks(eval_res.checks, desc.required_checks)
                reason = _verdict_reason_for(verd, eval_res)
                verdicts[r.id] = (verd, reason, checks)
            except Exception as exc:
                verdicts[r.id] = ("unchecked", f"Evaluation adapter error: {type(exc).__name__}", [])

        elif r.kind == "mesh_quality_v0":
            report_entry = artifacts_data.get(r.report_artifact_id)
            if not report_entry or report_entry[0] is None:
                verdicts[r.id] = ("unchecked", "Mesh report artifact unreadable or missing", [])
                continue
            mesh_result, _ = report_entry
            if not r.source_run_id or r.source_run_id not in runs_spec:
                verdicts[r.id] = ("unchecked", "Source simulation run or analysis spec missing", [])
                continue
            analysis_spec = runs_spec[r.source_run_id]
            if analysis_spec is None:
                verdicts[r.id] = ("unchecked", "Analysis spec payload unreadable or missing", [])
                continue

            try:
                eval_res = mesh_result_to_evaluation_result(mesh_result, analysis_spec, workspace_id=workspace_id)
                desc = mesh_evaluator_descriptor()
                verd = result_verdict(desc, eval_res)
                checks = _extract_checks(eval_res.checks, desc.required_checks)
                reason = _verdict_reason_for(verd, eval_res)
                verdicts[r.id] = (verd, reason, checks)
            except Exception as exc:
                verdicts[r.id] = ("unchecked", f"Evaluation adapter error: {type(exc).__name__}", [])

        elif r.kind == "fem_static_v0":
            report_entry = artifacts_data.get(r.report_artifact_id)
            if not report_entry or report_entry[0] is None:
                verdicts[r.id] = ("unchecked", "FEM report artifact unreadable or missing", [])
                continue
            report_payload, _ = report_entry
            summary = report_payload.get("result_summary")
            if not isinstance(summary, dict):
                verdicts[r.id] = ("unchecked", "FEM result summary missing in report payload", [])
                continue
            if not r.source_run_id or r.source_run_id not in runs_spec:
                verdicts[r.id] = ("unchecked", "Source simulation run or analysis spec missing", [])
                continue
            analysis_spec = runs_spec[r.source_run_id]
            if analysis_spec is None:
                verdicts[r.id] = ("unchecked", "Analysis spec payload unreadable or missing", [])
                continue

            try:
                eval_res = fem_result_to_evaluation_result(summary, analysis_spec, workspace_id=workspace_id)
                desc = fem_evaluator_descriptor()
                verd = result_verdict(desc, eval_res)
                checks = _extract_checks(eval_res.checks, desc.required_checks)
                reason = _verdict_reason_for(verd, eval_res)
                verdicts[r.id] = (verd, reason, checks)
            except Exception as exc:
                verdicts[r.id] = ("unchecked", f"Evaluation adapter error: {type(exc).__name__}", [])

        else:
            verdicts[r.id] = ("unchecked", f"Unsupported evidence kind: {r.kind}", [])

    return verdicts
