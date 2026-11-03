"""Synchronous BLUECAD AI generate/build/validate/repair loop."""

from __future__ import annotations

import json
import mimetypes
import shutil
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai.budget import evaluate_ai_status
from app.modules.ai.contracts import AIProviderAdapter
from app.modules.ai.execution import ProviderBinding, resolve_binding, run_ai_task
from app.modules.ai.settings import get_ai_settings
from app.modules.bluecad.evidence import (
    map_fem_static_evidence,
    map_mesh_quality_evidence,
    record_fem_static_evidence,
    record_mesh_quality_evidence,
    record_validation_evidence,
)
from app.modules.bluecad.evidence_egress import (
    EXTERNAL_STRUCTURAL_PROMPT_VERSION,
    bind_evidence_lineage,
    prepare_external_structural_repair,
)
from app.modules.bluecad.evidence_sight import render_evidence_sight
from app.modules.bluecad.export import sha256_file
from app.modules.bluecad.fem_adapter import append_tier3_checks, solve_static_analysis
from app.modules.bluecad.ledger import (
    _artifact_storage_path,
    candidate_work_dir,
    create_candidate_record,
    finish_attempt,
    get_candidate,
    mark_candidate_valid,
    park_candidate,
    register_artifact,
    register_export_artifacts,
    start_attempt,
    update_candidate_artifacts,
)
from app.modules.bluecad.mesh_adapter import mesh_analysis_spec
from app.modules.bluecad.models import BluecadCandidateCreate, BluecadCandidateRead, BluecadLoopConfig
from app.modules.bluecad.prompts import (
    PROMPT_VERSION,
    STRUCTURAL_REPAIR_PROMPT_VERSION,
    generate_prompt,
    repair_prompt,
    structural_repair_prompt,
)
from app.modules.bluecad.service import build_geometry_spec
from app.modules.bluecad.spec import SpecValidationError, canonical_json, canonicalize_geometry_spec
from app.modules.bluecad.structural_ledger import (
    commit_structural_candidate_artifacts,
    start_structural_attempt,
)
from app.modules.events.service import utc_now

_EXTERNAL_ROUTES = {"external:cheap", "external:reasoning"}
_MAX_PROPOSAL_TEXT_CHARS = 262_144


@dataclass(frozen=True)
class SimulationStageOutcome:
    status: str
    source_run_id: str | None = None


def create_bluecad_candidate(
    workspace_id: str,
    payload: BluecadCandidateCreate,
    *,
    adapters: dict[str, AIProviderAdapter] | None = None,
    bindings: dict[str, ProviderBinding] | None = None,
    force_external_allowed: bool = False,
) -> BluecadCandidateRead:
    loop_config = payload.loop_config or BluecadLoopConfig()
    _validate_loop_config(loop_config)
    candidate = create_candidate_record(workspace_id, payload.brief_text, loop_config)

    blocked_reason = None if force_external_allowed else _external_blocked_reason()
    if blocked_reason is not None:
        park_candidate(candidate.id, "budget_blocked", notes=f"external_blocked_reason={blocked_reason}")
        return _require_candidate(workspace_id, candidate.id)

    attempt_no = 0
    last_spec: dict[str, Any] | None = None
    last_report: dict[str, Any] | None = None
    malformed_on_tier = 0
    saw_malformed = False
    saw_build_or_validation_failure = False

    for tier in loop_config.tier_ladder:
        malformed_on_tier = 0
        for _ in range(loop_config.max_attempts_per_tier):
            attempt_no += 1
            prompt = _prompt_for_attempt(payload.brief_text, last_spec, last_report)
            task_kind = "bluecad_cad_generate" if last_spec is None else "bluecad_cad_repair"
            attempt = start_attempt(candidate.id, attempt_no, tier, prompt_version=PROMPT_VERSION)
            outcome = run_ai_task(
                user_prompt=prompt,
                task_kind=task_kind,
                route_class=tier,
                max_output_tokens=loop_config.max_output_tokens,
                adapters=adapters,
                bindings=bindings,
            )
            if outcome.status == "config_error":
                finish_attempt(
                    attempt.id,
                    proposal_ai_job_id=outcome.ledger_id,
                    proposal_outcome="blocked",
                    error_detail={"error_type": outcome.error_type or outcome.status},
                )
                park_candidate(
                    candidate.id,
                    "budget_blocked",
                    notes=f"external_blocked_reason={outcome.error_type or outcome.status}",
                )
                return _require_candidate(workspace_id, candidate.id)
            if outcome.status != "success" or outcome.response is None or outcome.response.text is None:
                finish_attempt(
                    attempt.id,
                    proposal_ai_job_id=outcome.ledger_id,
                    proposal_outcome="provider_error",
                    error_detail={"error_type": outcome.error_type or outcome.status},
                )
                continue

            try:
                spec = parse_geometry_spec_response(outcome.response.text)
            except SpecValidationError as exc:
                saw_malformed = True
                malformed_on_tier += 1
                finish_attempt(
                    attempt.id,
                    proposal_ai_job_id=outcome.ledger_id,
                    proposal_outcome="malformed",
                    error_detail={"parse_error": exc.detail},
                )
                if malformed_on_tier >= loop_config.max_attempts_per_tier:
                    break
                continue

            malformed_on_tier = 0
            last_spec = spec
            build = _build_and_register(workspace_id, candidate.id, attempt_no, spec)
            verdict = "pass" if build["report"].get("verdict") == "pass" else "fail"
            build_outcome = (
                "ok"
                if build["result"].verdict != "error"
                else _build_error_code(build["result"].errors)
            )
            finish_attempt(
                attempt.id,
                proposal_ai_job_id=outcome.ledger_id,
                proposal_outcome="ok",
                build_outcome=build_outcome,
                validation_verdict=verdict,
                spec_artifact_id=build["spec_artifact_id"],
                report_artifact_id=build["report_artifact_id"],
                manifest_artifact_id=build["manifest_artifact_id"],
                error_detail={"prompt_version": PROMPT_VERSION},
            )
            record_validation_evidence(
                workspace_id,
                candidate.id,
                attempt.id,
                build["report"],
                report_artifact_id=build["report_artifact_id"],
            )
            update_candidate_artifacts(
                candidate.id,
                spec_artifact_id=build["spec_artifact_id"],
                glb_artifact_id=build["glb_artifact_id"],
                report_artifact_id=build["report_artifact_id"],
            )
            last_report = build["report"]
            if verdict == "pass":
                mark_candidate_valid(candidate.id)
                simulation = _run_simulation_stage(
                    workspace_id,
                    candidate.id,
                    attempt.id,
                    attempt_no,
                    loop_config.analysis_spec,
                    build,
                )
                if loop_config.structural_repair and simulation.status == "criteria_failed":
                    _run_structural_repair_cycle(
                        workspace_id=workspace_id,
                        candidate_id=candidate.id,
                        initial_attempt_id=attempt.id,
                        initial_attempt_no=attempt_no,
                        initial_spec=spec,
                        route_class=tier,
                        loop_config=loop_config,
                        adapters=adapters,
                        bindings=bindings,
                    )
                return _require_candidate(workspace_id, candidate.id)
            saw_build_or_validation_failure = True

    reason = (
        "malformed_repeated"
        if saw_malformed and not saw_build_or_validation_failure
        else "attempts_exhausted"
    )
    park_candidate(candidate.id, reason)
    return _require_candidate(workspace_id, candidate.id)


def _run_structural_repair_cycle(
    *,
    workspace_id: str,
    candidate_id: str,
    initial_attempt_id: str,
    initial_attempt_no: int,
    initial_spec: dict[str, Any],
    route_class: str,
    loop_config: BluecadLoopConfig,
    adapters: dict[str, AIProviderAdapter] | None,
    bindings: dict[str, ProviderBinding] | None,
) -> None:
    sight = render_evidence_sight(workspace_id, candidate_id, initial_attempt_id)
    if sight is None:
        return
    current_spec = initial_spec
    source_attempt_id = initial_attempt_id
    attempt_no = initial_attempt_no
    binding, _decision = resolve_binding(route_class, bindings)
    if binding is None:
        return
    network_bound = binding.requires_network

    for _ in range(loop_config.max_structural_repairs):
        attempt_no += 1
        external_preparation = None
        if network_bound:
            try:
                external_preparation = prepare_external_structural_repair(
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                    source_attempt_id=source_attempt_id,
                    valid_spec=current_spec,
                    expected_sight=sight,
                    adapters=adapters,
                )
            except Exception:  # noqa: BLE001 - preparation is fail-closed before attempt insert.
                return
            prompt = external_preparation.raw_prompt
            prompt_version = EXTERNAL_STRUCTURAL_PROMPT_VERSION
        else:
            prompt = structural_repair_prompt(current_spec, sight.text)
            prompt_version = STRUCTURAL_REPAIR_PROMPT_VERSION
        try:
            attempt = start_structural_attempt(
                candidate_id,
                attempt_no,
                route_class,
                prompt_version=prompt_version,
                evidence_digest=sight.digest,
            )
        except ValueError:
            return
        if external_preparation is None:
            outcome = run_ai_task(
                user_prompt=prompt,
                task_kind="bluecad_cad_repair",
                route_class=route_class,
                max_output_tokens=loop_config.max_output_tokens,
                adapters=adapters,
                bindings=bindings,
            )
        else:
            lineage = external_preparation.lineage_for(attempt.id)
            with bind_evidence_lineage(lineage):
                outcome = run_ai_task(
                    user_prompt=prompt,
                    task_kind="bluecad_cad_repair",
                    route_class=route_class,
                    context_blocks=[dict(external_preparation.context_block)],
                    max_output_tokens=loop_config.max_output_tokens,
                    adapters=adapters,
                    bindings=bindings,
                    workspace_id=workspace_id,
                )
        if outcome.status == "config_error":
            finish_attempt(
                attempt.id,
                proposal_ai_job_id=outcome.ledger_id,
                proposal_outcome="blocked",
                error_detail={"error_type": outcome.error_type or outcome.status},
            )
            return
        if outcome.status != "success" or outcome.response is None or outcome.response.text is None:
            finish_attempt(
                attempt.id,
                proposal_ai_job_id=outcome.ledger_id,
                proposal_outcome="provider_error",
                error_detail={"error_type": outcome.error_type or outcome.status},
            )
            return
        try:
            spec = parse_geometry_spec_response(outcome.response.text)
        except SpecValidationError as exc:
            finish_attempt(
                attempt.id,
                proposal_ai_job_id=outcome.ledger_id,
                proposal_outcome="malformed",
                error_detail={"parse_error": exc.detail},
            )
            return
        try:
            build = _build_and_register(workspace_id, candidate_id, attempt_no, spec)
        except Exception as exc:  # noqa: BLE001 - speculative build must preserve valid owner.
            finish_attempt(
                attempt.id,
                proposal_ai_job_id=outcome.ledger_id,
                proposal_outcome="ok",
                build_outcome="error",
                error_detail={"build_error": type(exc).__name__},
            )
            return
        verdict = "pass" if build["report"].get("verdict") == "pass" else "fail"
        build_outcome = (
            "ok"
            if build["result"].verdict != "error"
            else _build_error_code(build["result"].errors)
        )
        finish_attempt(
            attempt.id,
            proposal_ai_job_id=outcome.ledger_id,
            proposal_outcome="ok",
            build_outcome=build_outcome,
            validation_verdict=verdict,
            spec_artifact_id=build["spec_artifact_id"],
            report_artifact_id=build["report_artifact_id"],
            manifest_artifact_id=build["manifest_artifact_id"],
        )
        try:
            record_validation_evidence(
                workspace_id,
                candidate_id,
                attempt.id,
                build["report"],
                report_artifact_id=build["report_artifact_id"],
            )
        except Exception:  # noqa: BLE001 - evidence failure is non-triggering.
            return
        if verdict != "pass" or build_outcome != "ok":
            return
        simulation = _run_simulation_stage(
            workspace_id,
            candidate_id,
            attempt.id,
            attempt_no,
            loop_config.analysis_spec,
            build,
            producer_notes="Generated by EVIDENCE-SIGHT-0 structural repair.",
            required_candidate_status="valid",
        )
        if simulation.status == "criteria_passed":
            glb_artifact_id = build.get("glb_artifact_id")
            if not isinstance(glb_artifact_id, str) or not glb_artifact_id:
                return
            try:
                commit_structural_candidate_artifacts(
                    candidate_id,
                    spec_artifact_id=build["spec_artifact_id"],
                    glb_artifact_id=glb_artifact_id,
                    report_artifact_id=build["report_artifact_id"],
                )
            except ValueError:
                return
            return
        if simulation.status != "criteria_failed":
            return
        next_sight = render_evidence_sight(workspace_id, candidate_id, attempt.id)
        if next_sight is None:
            return
        current_spec = spec
        source_attempt_id = attempt.id
        sight = next_sight


def _run_simulation_stage(
    workspace_id: str,
    candidate_id: str,
    attempt_id: str,
    attempt_no: int,
    analysis_spec_without_geometry: dict[str, Any] | None,
    build: dict[str, Any],
    *,
    producer_notes: str | None = None,
    required_candidate_status: str | None = None,
) -> SimulationStageOutcome:
    if analysis_spec_without_geometry is None:
        return SimulationStageOutcome("skipped")
    try:
        out_dir = candidate_work_dir(workspace_id, candidate_id, attempt_no) / "simulation"
        out_dir.mkdir(parents=True, exist_ok=True)
        analysis_spec = _analysis_spec_with_geometry(analysis_spec_without_geometry, build)
        source_run_id = _create_simulation_run(
            workspace_id,
            candidate_id,
            attempt_id,
            analysis_spec,
            required_candidate_status=required_candidate_status,
        )
    except Exception:
        return SimulationStageOutcome("setup_error")
    source_ref = f"bluecad_candidate:{candidate_id}:attempt:{attempt_no}:sim:{source_run_id}"
    try:
        mesh_result = mesh_analysis_spec(analysis_spec, out_dir / "mesh")
    except Exception as exc:  # noqa: BLE001 - sim failures are advisory evidence only.
        mesh_result = _mesh_error_result(exc, analysis_spec)
    try:
        if required_candidate_status is None:
            mesh_report_artifact_id = _register_sim_report(
                workspace_id,
                out_dir / "mesh_result.json",
                mesh_result,
                source_ref,
                producer_notes=producer_notes,
            )
            mesh_evidence_id = record_mesh_quality_evidence(
                workspace_id,
                mesh_result,
                source_run_id=source_run_id,
                report_artifact_id=mesh_report_artifact_id,
            )
            _link_sim_evidence_context(mesh_evidence_id, candidate_id, attempt_id)
        else:
            _persist_sim_evidence(
                workspace_id,
                candidate_id,
                attempt_id,
                source_run_id,
                out_dir / "mesh_result.json",
                mesh_result,
                source_ref,
                evidence_builder=lambda artifact_id: map_mesh_quality_evidence(
                    workspace_id,
                    mesh_result,
                    source_run_id=source_run_id,
                    report_artifact_id=artifact_id,
                ),
                producer_notes=producer_notes,
                required_candidate_status=required_candidate_status,
            )
    except Exception:
        _best_effort_fail_simulation_run(
            source_run_id,
            "mesh_evidence_persistence_failed",
        )
        return SimulationStageOutcome("mesh_evidence_error", source_run_id)
    mesh_verdict = str(mesh_result.get("verdict") or "error")
    if mesh_verdict != "pass" or "mesh_inp" not in mesh_result.get("artifacts", {}):
        _complete_simulation_run(source_run_id, mesh_result, None)
        status = "mesh_failed" if mesh_verdict == "fail" else "mesh_error"
        return SimulationStageOutcome(status, source_run_id)
    try:
        fem_summary = solve_static_analysis(analysis_spec, mesh_result, out_dir / "fem")
    except Exception as exc:  # noqa: BLE001 - sim failures are advisory evidence only.
        fem_summary = _fem_error_result(exc)
    fem_report = None
    if fem_summary.get("verdict") == "pass" and analysis_spec.get("pass_criteria"):
        try:
            fem_report = append_tier3_checks(
                deepcopy(build["report"]),
                fem_summary,
                analysis_spec["pass_criteria"],
            )
        except Exception as exc:  # noqa: BLE001 - Tier 3 failures are advisory evidence only.
            fem_report = {
                "verdict": "error",
                "checks": [],
                "errors": [
                    {
                        "code": "TIER3_ERROR",
                        "detail": {"message": str(exc), "type": type(exc).__name__},
                    }
                ],
            }
    fem_payload = {"result_summary": fem_summary, "report": fem_report}
    try:
        if required_candidate_status is None:
            fem_report_artifact_id = _register_sim_report(
                workspace_id,
                out_dir / "fem_result.json",
                fem_payload,
                source_ref,
                producer_notes=producer_notes,
            )
            fem_evidence_id = record_fem_static_evidence(
                workspace_id,
                fem_summary,
                fem_report,
                source_run_id=source_run_id,
                report_artifact_id=fem_report_artifact_id,
            )
            _link_sim_evidence_context(fem_evidence_id, candidate_id, attempt_id)
        else:
            _persist_sim_evidence(
                workspace_id,
                candidate_id,
                attempt_id,
                source_run_id,
                out_dir / "fem_result.json",
                fem_payload,
                source_ref,
                evidence_builder=lambda artifact_id: map_fem_static_evidence(
                    workspace_id,
                    fem_summary,
                    fem_report,
                    source_run_id=source_run_id,
                    report_artifact_id=artifact_id,
                ),
                producer_notes=producer_notes,
                required_candidate_status=required_candidate_status,
            )
    except Exception:
        _best_effort_fail_simulation_run(
            source_run_id,
            "fem_evidence_persistence_failed",
        )
        return SimulationStageOutcome("fem_evidence_error", source_run_id)
    _complete_simulation_run(source_run_id, mesh_result, fem_summary, fem_report)
    if fem_summary.get("verdict") != "pass":
        return SimulationStageOutcome("solve_error", source_run_id)
    if not analysis_spec.get("pass_criteria"):
        return SimulationStageOutcome("no_criteria", source_run_id)
    return SimulationStageOutcome(_criteria_outcome(fem_report), source_run_id)


def _criteria_outcome(fem_report: dict[str, Any] | None) -> str:
    if fem_report is None:
        return "criteria_error"
    checks = fem_report.get("checks")
    errors = fem_report.get("errors")
    if not isinstance(checks, list) or not isinstance(errors, list):
        return "criteria_error"
    statuses = [check.get("status") for check in checks if isinstance(check, dict)]
    if fem_report.get("verdict") == "error" or errors or "error" in statuses:
        return "criteria_error"
    if fem_report.get("verdict") == "fail" and "fail" in statuses:
        return "criteria_failed"
    if fem_report.get("verdict") == "pass" and all(status == "pass" for status in statuses):
        return "criteria_passed"
    return "criteria_error"


def _link_sim_evidence_context(
    record_id: str,
    candidate_id: str,
    attempt_id: str,
) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE evidence_records SET candidate_id = ?, attempt_id = ? WHERE id = ?",
            (candidate_id, attempt_id, record_id),
        )
        connection.commit()


def _register_sim_report(
    workspace_id: str,
    path: Path,
    payload: dict[str, Any],
    source_ref: str,
    *,
    producer_notes: str | None = None,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if producer_notes is None:
        return register_artifact(
            workspace_id,
            path,
            role="bluecad_sim_report",
            source_ref=source_ref,
        )
    return register_artifact(
        workspace_id,
        path,
        role="bluecad_sim_report",
        source_ref=source_ref,
        producer_notes=producer_notes,
    )


def _analysis_spec_with_geometry(
    analysis_spec_without_geometry: dict[str, Any],
    build: dict[str, Any],
) -> dict[str, Any]:
    result = build["result"]
    step_path = result.out_dir / "model.step"
    manifest_path = result.manifest_path
    if manifest_path is None:
        raise RuntimeError("validated BLUECAD build missing manifest for simulation")
    return {
        **analysis_spec_without_geometry,
        "geometry": {
            "step_path": str(step_path),
            "manifest_path": str(manifest_path),
        },
    }


def _persist_sim_evidence(
    workspace_id: str,
    candidate_id: str,
    attempt_id: str,
    source_run_id: str,
    path: Path,
    payload: dict[str, Any],
    source_ref: str,
    *,
    evidence_builder: Callable[[str], Any],
    producer_notes: str | None = None,
    required_candidate_status: str | None = None,
) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_id = str(uuid4())
    stored_path = _artifact_storage_path(
        workspace_id,
        artifact_id,
        path.name,
    )
    try:
        stored_path.parent.mkdir(parents=True, exist_ok=False)
        shutil.copy2(path, stored_path)
        evidence = evidence_builder(artifact_id)
        evidence_id = str(uuid4())
        notes = producer_notes or "Generated by BLUECAD AI loop v0."
        now = utc_now()
        with open_sqlite_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                """
                SELECT input_payload
                FROM simulation_runs
                WHERE id = ? AND workspace_id = ? AND status = 'running'
                """,
                (source_run_id, workspace_id),
            ).fetchone()
            if run_row is None:
                connection.rollback()
                raise RuntimeError(
                    "BLUECAD analysis run no longer owns evidence persistence"
                )
            run_context = json.loads(str(run_row["input_payload"]))
            if (
                run_context.get("candidate_id") != candidate_id
                or run_context.get("attempt_id") != attempt_id
            ):
                connection.rollback()
                raise RuntimeError("BLUECAD analysis run context is inconsistent")
            if required_candidate_status is not None:
                owner = connection.execute(
                    """
                    SELECT 1
                    FROM bluecad_candidates
                    WHERE workspace_id = ? AND id = ? AND status = ?
                    """,
                    (workspace_id, candidate_id, required_candidate_status),
                ).fetchone()
                if owner is None:
                    connection.rollback()
                    raise RuntimeError(
                        "BLUECAD candidate no longer owns evidence persistence"
                    )
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, workspace_id, filename, stored_path, artifact_type,
                    mime_type, sha256, source_ref, status, created_at, notes
                ) VALUES (?, ?, ?, ?, 'bluecad_sim_report', ?, ?, ?,
                    'registered', ?, ?)
                """,
                (
                    artifact_id,
                    workspace_id,
                    stored_path.name,
                    str(stored_path),
                    mimetypes.guess_type(stored_path.name)[0]
                    or "application/octet-stream",
                    sha256_file(stored_path),
                    source_ref,
                    now,
                    notes,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_records (
                    id, workspace_id, kind, verdict, metrics_json, source_run_id,
                    candidate_id, attempt_id, report_artifact_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    evidence.workspace_id,
                    evidence.kind,
                    evidence.verdict,
                    evidence.metrics_json,
                    source_run_id,
                    candidate_id,
                    attempt_id,
                    artifact_id,
                    now,
                ),
            )
            connection.commit()
    except Exception:
        shutil.rmtree(stored_path.parent, ignore_errors=True)
        raise
    return artifact_id, evidence_id


def _create_simulation_run(
    workspace_id: str,
    candidate_id: str,
    attempt_id: str,
    analysis_spec: dict[str, Any],
    *,
    required_candidate_status: str | None = None,
) -> str:
    run_id = str(uuid4())
    now = utc_now()
    values = (
        run_id,
        workspace_id,
        f"bluecad_attempt_{attempt_id}",
        json.dumps(
            {"candidate_id": candidate_id, "attempt_id": attempt_id},
            sort_keys=True,
        ),
        json.dumps(analysis_spec, sort_keys=True),
        now,
        now,
        "BLUECAD advisory synchronous mesh/FEM stage.",
    )
    with open_sqlite_connection() as connection:
        if required_candidate_status is None:
            cursor = connection.execute(
                """
                INSERT INTO simulation_runs (
                    id, workspace_id, model_version_id, run_label, status,
                    input_payload, parameter_payload, output_payload, started_at,
                    completed_at, created_at, notes
                ) VALUES (?, ?, NULL, ?, 'running', ?, ?, NULL, ?, NULL, ?, ?)
                """,
                values,
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO simulation_runs (
                    id, workspace_id, model_version_id, run_label, status,
                    input_payload, parameter_payload, output_payload, started_at,
                    completed_at, created_at, notes
                )
                SELECT ?, ?, NULL, ?, 'running', ?, ?, NULL, ?, NULL, ?, ?
                FROM bluecad_candidates
                WHERE workspace_id = ? AND id = ? AND status = ?
                """,
                (
                    *values,
                    workspace_id,
                    candidate_id,
                    required_candidate_status,
                ),
            )
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError("BLUECAD candidate no longer owns the analysis stage")
        connection.commit()
    return run_id


def _complete_simulation_run(
    source_run_id: str,
    mesh_result: dict[str, Any],
    fem_summary: dict[str, Any] | None,
    fem_report: dict[str, Any] | None = None,
) -> None:
    try:
        completed_at = utc_now()
        output_payload = _simulation_run_output_payload(
            mesh_result,
            fem_summary,
            fem_report,
        )
        with open_sqlite_connection() as connection:
            connection.execute(
                """
                UPDATE simulation_runs
                SET status = 'completed', output_payload = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (canonical_json(output_payload), completed_at, source_run_id),
            )
            connection.commit()
    except Exception:
        return


def _best_effort_fail_simulation_run(source_run_id: str, error_code: str) -> None:
    try:
        completed_at = utc_now()
        output_payload = {
            "status": "failed",
            "error": {"code": error_code},
            "mesh_verdict": None,
            "fem_verdict": None,
        }
        with open_sqlite_connection() as connection:
            connection.execute(
                """
                UPDATE simulation_runs
                SET status = 'failed', output_payload = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (canonical_json(output_payload), completed_at, source_run_id),
            )
            connection.commit()
    except Exception:
        return


def _simulation_run_output_payload(
    mesh_result: dict[str, Any],
    fem_summary: dict[str, Any] | None,
    fem_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "completed",
        "mesh_verdict": mesh_result.get("verdict"),
        "fem_verdict": (
            fem_report.get("verdict")
            if fem_report is not None
            else (fem_summary.get("verdict") if fem_summary is not None else None)
        ),
    }


def _mesh_error_result(exc: Exception, analysis_spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_mesh_result_v0_1",
        "verdict": "error",
        "errors": [
            {
                "code": "MESH_ERROR",
                "detail": {"message": str(exc), "type": type(exc).__name__},
            }
        ],
        "attempts": [
            {
                "attempt_no": 1,
                "target_size": analysis_spec.get("mesh", {}).get("target_size"),
                "counts": {},
                "errors": [],
            }
        ],
        "artifacts": {},
    }


def _fem_error_result(exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_result_summary_v0_1",
        "verdict": "error",
        "errors": [
            {
                "code": "SOLVE_ERROR",
                "detail": {"message": str(exc), "type": type(exc).__name__},
            }
        ],
        "solver": {"tool_id": "calculix", "version": None, "returncode": None},
        "artifacts": {},
    }


def parse_geometry_spec_response(text: str) -> dict[str, Any]:
    if len(text) > _MAX_PROPOSAL_TEXT_CHARS:
        raise SpecValidationError({"message": "LLM response exceeds the proposal text limit."})
    payload = _extract_single_json_object(text)
    try:
        loaded = json.loads(payload)
    except RecursionError as exc:
        raise SpecValidationError(
            {"message": "LLM response exceeded the JSON nesting limit."}
        ) from exc
    except json.JSONDecodeError as exc:
        raise SpecValidationError(
            {
                "message": "LLM response did not contain valid JSON.",
                "position": exc.pos,
            }
        ) from exc
    try:
        return canonicalize_geometry_spec(loaded)
    except SpecValidationError:
        raise
    except Exception as exc:
        raise SpecValidationError(
            {"message": str(exc), "type": type(exc).__name__}
        ) from exc


def _extract_single_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if (
            len(lines) >= 3
            and lines[0].startswith("```")
            and lines[-1].strip() == "```"
        ):
            stripped = "\n".join(lines[1:-1]).strip()
    start = stripped.find("{")
    if start < 0:
        raise SpecValidationError(
            {"message": "LLM response must contain one JSON object."}
        )
    depth = 0
    in_string = False
    escape = False
    end = None
    for index, char in enumerate(stripped[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
            if depth < 0:
                break
    if end is None:
        raise SpecValidationError(
            {"message": "LLM response JSON object was incomplete."}
        )
    before = stripped[:start].strip()
    after = stripped[end:].strip()
    if before or after:
        raise SpecValidationError(
            {"message": "LLM response must contain exactly one JSON object."}
        )
    return stripped[start:end]


def _prompt_for_attempt(
    brief_text: str,
    last_spec: dict[str, Any] | None,
    last_report: dict[str, Any] | None,
) -> str:
    if last_spec is None or last_report is None:
        return generate_prompt(brief_text)
    return repair_prompt(last_spec, last_report)


def _build_and_register(
    workspace_id: str,
    candidate_id: str,
    attempt_no: int,
    spec: dict[str, Any],
) -> dict[str, Any]:
    out_dir = candidate_work_dir(workspace_id, candidate_id, attempt_no)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / "geometry_spec.json"
    spec_path.write_text(canonical_json(spec) + "\n", encoding="utf-8")
    result = build_geometry_spec(spec, out_dir)
    source_ref = f"bluecad_candidate:{candidate_id}:attempt:{attempt_no}"
    spec_artifact_id = register_artifact(
        workspace_id,
        spec_path,
        role="bluecad_spec",
        source_ref=source_ref,
    )
    report_artifact_id = register_artifact(
        workspace_id,
        _require_path(result.report_path, out_dir / "validation_report.json"),
        role="bluecad_report",
        source_ref=source_ref,
    )
    manifest_artifact_id = None
    glb_artifact_id = None
    if result.manifest_path is not None and result.manifest_path.exists():
        manifest_artifact_id = register_artifact(
            workspace_id,
            result.manifest_path,
            role="bluecad_manifest",
            source_ref=source_ref,
        )
    glb_path = out_dir / "model.glb"
    if glb_path.exists():
        glb_artifact_id = register_artifact(
            workspace_id,
            glb_path,
            role="bluecad_glb",
            source_ref=source_ref,
        )
        register_export_artifacts(workspace_id, out_dir, source_ref=source_ref)
    return {
        "result": result,
        "report": result.report,
        "spec_artifact_id": spec_artifact_id,
        "report_artifact_id": report_artifact_id,
        "manifest_artifact_id": manifest_artifact_id,
        "glb_artifact_id": glb_artifact_id,
    }


def _require_path(path: Path | None, fallback: Path) -> Path:
    resolved = path or fallback
    if not resolved.exists():
        raise RuntimeError(f"expected BLUECAD artifact missing: {resolved}")
    return resolved


def _build_error_code(errors: list[dict[str, Any]]) -> str:
    if not errors:
        return "error"
    code = errors[0].get("code")
    return str(code or "error")


def _external_blocked_reason(route_class: str = "external:cheap") -> str | None:
    binding, _decision = resolve_binding(route_class)
    provider_mode = binding.provider_id if binding is not None else route_class
    status = evaluate_ai_status(get_ai_settings(), provider_mode)
    if status.external_calls_allowed:
        return None
    return status.blocking_reason or "external_calls_blocked"


def _validate_loop_config(loop_config: BluecadLoopConfig) -> None:
    if not loop_config.tier_ladder:
        raise ValueError("tier_ladder must not be empty")
    for route_class in loop_config.tier_ladder:
        if route_class not in _EXTERNAL_ROUTES:
            raise ValueError(
                "BLUECAD loop route classes must be explicit external tiers"
            )


def _require_candidate(workspace_id: str, candidate_id: str) -> BluecadCandidateRead:
    candidate = get_candidate(workspace_id, candidate_id)
    if candidate is None:  # pragma: no cover - defensive persistence guard
        raise RuntimeError("BLUECAD candidate disappeared during loop")
    return candidate
