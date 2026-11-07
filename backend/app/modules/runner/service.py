import hashlib
import json
import mimetypes
import shutil
from collections.abc import Sequence
from math import isfinite
from pathlib import Path
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.events.service import log_event, utc_now
from app.modules.memory.models import CalcParameterProposalCreate
from app.modules.memory.service import create_calc_parameter_proposals
from app.modules.project_knowledge.sim_results import ingest_validated_calc_v0_run
from app.modules.runner.input_contracts import (
    build_binding_preview,
    canonicalize_input_contract,
)
from app.modules.runner.linked_parameters import (
    LINKED_PARAMETER_UNUSABLE_CODE,
    LINKED_PARAMETER_UNUSABLE_MESSAGE,
    contract_requires_canonical_linked_parameters,
    load_usable_linked_parameter,
    require_linked_parameters_usable,
)
from app.modules.runner.local_python import LocalPythonResult, execute_python_script
from app.modules.runner.models import (
    BindingPreviewRequest,
    BindingPreviewResponse,
    ModelImplementationCreate,
    ModelImplementationRead,
    RunArtifactRead,
    RunLogRead,
    RunnerJobCreate,
    RunnerJobCreateResponse,
    RunnerJobRead,
    RunnerJobRunResponse,
    SimulationRunDetail,
)
from app.modules.runner.safety import (
    BLUECAD_L2_REQUIRED_ARTIFACTS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ARTIFACT_BYTES,
    MAX_OUTPUT_JSON_BYTES,
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    RunnerSafetyError,
    canonical_json,
    model_implementation_root,
    preflight_script_policy,
    run_root,
    safe_artifact_path,
    sha256_file,
    validate_batch_growth_input,
    validate_bluecad_l2_input,
    validate_calc_v0_input,
    validate_run_paths,
    validate_script_path,
)
from app.modules.studio_runtime import runtime as studio_runtime

RUNNER_TYPE = "python_local"
IMPLEMENTATION_KIND = "batch_growth_v0"
BLUECAD_L2_IMPLEMENTATION_KIND = "bluecad_l2_v0"
CALC_V0_IMPLEMENTATION_KIND = "calc_v0"
STUDIO_CALC_IMPLEMENTATION_KIND = "studio_calc_v1"
STUDIO_GEOMETRY_IMPLEMENTATION_KIND = "studio_geometry_v1"
STUDIO_IMPLEMENTATION_KINDS = frozenset({STUDIO_CALC_IMPLEMENTATION_KIND, STUDIO_GEOMETRY_IMPLEMENTATION_KIND})
SUPPORTED_IMPLEMENTATION_KINDS = frozenset(
    {IMPLEMENTATION_KIND, BLUECAD_L2_IMPLEMENTATION_KIND, CALC_V0_IMPLEMENTATION_KIND}
)


def create_model_implementation(workspace_id: str, payload: ModelImplementationCreate) -> ModelImplementationRead:
    if payload.implementation_kind not in SUPPORTED_IMPLEMENTATION_KINDS:
        raise RunnerSafetyError(
            "runner_implementation_kind_unsupported",
            "Only batch_growth_v0, bluecad_l2_v0, and calc_v0 are supported.",
        )
    if (
        payload.implementation_kind in {BLUECAD_L2_IMPLEMENTATION_KIND, CALC_V0_IMPLEMENTATION_KIND}
        and not payload.script_text
    ):
        raise RunnerSafetyError("runner_script_text_required", f"{payload.implementation_kind} requires script_text.")

    input_contract_payload: str | None = None
    input_contract_sha256: str | None = None
    if payload.input_contract is not None:
        input_contract_payload, input_contract_sha256, _ = canonicalize_input_contract(payload.input_contract)

    now = utc_now()
    model_version_id = str(uuid4())
    artifact_id = str(uuid4())
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        model_spec = connection.execute(
            "SELECT * FROM model_specs WHERE id = ? AND workspace_id = ?",
            (payload.model_spec_id, workspace_id),
        ).fetchone()
        if model_spec is None:
            raise RunnerSafetyError("runner_model_spec_not_found", "Model spec not found.")

    target_dir = model_implementation_root(workspace_id) / model_version_id
    target_dir.mkdir(parents=True, exist_ok=False)
    if payload.implementation_kind == IMPLEMENTATION_KIND:
        target_script = target_dir / "batch_growth.py"
        shutil.copy2(_example_script_path(), target_script)
        artifact_notes = "Reviewed deterministic batch growth V0 script."
        changelog = "Initial reviewed deterministic batch growth implementation."
    elif payload.implementation_kind == BLUECAD_L2_IMPLEMENTATION_KIND:
        target_script = target_dir / "bluecad_l2.py"
        target_script.write_text(payload.script_text or "", encoding="utf-8")
        artifact_notes = "Caller-supplied BLUECAD L2 V0 script."
        changelog = "Initial BLUECAD L2 V0 script implementation."
    else:
        target_script = target_dir / "calc_v0.py"
        target_script.write_text(payload.script_text or "", encoding="utf-8")
        artifact_notes = "Caller-supplied calc V0 script."
        changelog = "Initial calc V0 script implementation."
    script_sha = sha256_file(target_script)

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                workspace_id,
                target_script.name,
                str(target_script),
                "python_script",
                "text/x-python",
                script_sha,
                f"model_spec:{payload.model_spec_id}",
                "registered",
                now,
                artifact_notes,
            ),
        )
        connection.execute(
            """
            INSERT INTO model_versions (
                id, workspace_id, model_spec_id, version_label,
                implementation_artifact_id, implementation_kind, status, changelog,
                input_contract_payload, input_contract_sha256, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_version_id,
                workspace_id,
                payload.model_spec_id,
                payload.version_label,
                artifact_id,
                payload.implementation_kind,
                "ready",
                changelog,
                input_contract_payload,
                input_contract_sha256,
                now,
                payload.notes,
            ),
        )
        _log_event(
            connection,
            event_type="ModelImplementationCreated",
            target_type="ModelImplementation",
            target_id=model_version_id,
            workspace_id=workspace_id,
            payload={
                "model_spec_id": payload.model_spec_id,
                "implementation_kind": payload.implementation_kind,
                "script_sha256": script_sha,
            },
        )
        connection.commit()

    return get_model_implementation(workspace_id, model_version_id)


def list_model_implementations(workspace_id: str) -> list[ModelImplementationRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            """
            SELECT mv.*, a.sha256 AS script_sha256, a.stored_path AS script_path
            FROM model_versions mv
            JOIN artifacts a ON a.id = mv.implementation_artifact_id
            WHERE mv.workspace_id = ?
            ORDER BY mv.created_at DESC
            """,
            (workspace_id,),
        ).fetchall()
    return [_model_implementation_from_row(row) for row in rows]


def get_model_implementation(workspace_id: str, model_version_id: str) -> ModelImplementationRead:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT mv.*, a.sha256 AS script_sha256, a.stored_path AS script_path
            FROM model_versions mv
            JOIN artifacts a ON a.id = mv.implementation_artifact_id
            WHERE mv.id = ? AND mv.workspace_id = ?
            """,
            (model_version_id, workspace_id),
        ).fetchone()
    if row is None:
        raise RunnerSafetyError("runner_model_version_not_found", "Model implementation not found.")
    return _model_implementation_from_row(row)


def preview_model_bindings(
    workspace_id: str,
    model_version_id: str,
    payload: BindingPreviewRequest,
) -> BindingPreviewResponse:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        model_version = connection.execute(
            "SELECT * FROM model_versions WHERE id = ? AND workspace_id = ?",
            (model_version_id, workspace_id),
        ).fetchone()
        if model_version is None:
            raise RunnerSafetyError(
                "runner_model_version_not_found",
                "Model implementation not found.",
            )

        canonical_linked_parameters = contract_requires_canonical_linked_parameters(
            model_version["input_contract_payload"],
            model_version["input_contract_sha256"],
        )

        def load_parameter(parameter_id: str) -> dict[str, object] | None:
            if canonical_linked_parameters:
                return load_usable_linked_parameter(connection, workspace_id, parameter_id)
            row = connection.execute(
                "SELECT id, workspace_id, value, unit FROM parameters WHERE id = ? AND workspace_id = ?",
                (parameter_id, workspace_id),
            ).fetchone()
            return dict(row) if row is not None else None

        return build_binding_preview(
            model_version_id=model_version_id,
            contract_payload=model_version["input_contract_payload"],
            contract_sha256=model_version["input_contract_sha256"],
            bindings=payload.bindings,
            load_parameter=load_parameter,
            studio_pint=model_version["implementation_kind"] in STUDIO_IMPLEMENTATION_KINDS,
        )


def create_runner_job(workspace_id: str, payload: RunnerJobCreate) -> RunnerJobCreateResponse:
    now = utc_now()
    simulation_run_id = str(uuid4())
    runner_job_id = str(uuid4())

    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        model_version = _load_model_version_with_artifact(connection, workspace_id, payload.model_version_id)
        implementation_kind = model_version["implementation_kind"]
        if implementation_kind == IMPLEMENTATION_KIND:
            input_payload, parameter_payload = validate_batch_growth_input(payload.input_set)
        elif implementation_kind == BLUECAD_L2_IMPLEMENTATION_KIND:
            input_payload, parameter_payload = validate_bluecad_l2_input(payload.input_set)
        elif implementation_kind == CALC_V0_IMPLEMENTATION_KIND:
            input_payload, parameter_payload = validate_calc_v0_input(payload.input_set)
        else:
            raise RunnerSafetyError("runner_implementation_kind_unsupported", "Unsupported implementation kind.")
        script_path = validate_script_path(workspace_id, model_version["script_path"])
        script_sha = sha256_file(script_path)
        if script_sha != model_version["script_sha256"]:
            raise RunnerSafetyError("runner_script_hash_mismatch", "Script hash does not match registered artifact.")
        if implementation_kind == BLUECAD_L2_IMPLEMENTATION_KIND:
            preflight_script_policy(script_path, ast_import_allowlist=True)
        elif implementation_kind == CALC_V0_IMPLEMENTATION_KIND:
            preflight_script_policy(script_path, ast_policy=CALC_V0_IMPLEMENTATION_KIND)

        job_run_root = run_root(workspace_id, simulation_run_id)
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, model_version_id, run_label, status,
                input_payload, parameter_payload, output_payload, started_at,
                completed_at, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                simulation_run_id,
                workspace_id,
                payload.model_version_id,
                payload.run_label,
                "queued",
                input_payload,
                parameter_payload,
                None,
                None,
                None,
                now,
                "Created by Python Runner V0.",
            ),
        )
        connection.execute(
            """
            INSERT INTO runner_jobs (
                id, workspace_id, simulation_run_id, runner_type, status,
                script_path, script_sha256, implementation_kind, command_json, environment_json,
                working_dir, input_file, output_dir, timeout_seconds,
                max_stdout_bytes, max_stderr_bytes, max_output_json_bytes,
                max_artifact_bytes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                runner_job_id,
                workspace_id,
                simulation_run_id,
                RUNNER_TYPE,
                "queued",
                str(script_path),
                script_sha,
                implementation_kind,
                None,
                None,
                str(job_run_root),
                str(job_run_root / "input.json"),
                str(job_run_root),
                min(payload.timeout_seconds or DEFAULT_TIMEOUT_SECONDS, 60),
                MAX_STDOUT_BYTES,
                MAX_STDERR_BYTES,
                MAX_OUTPUT_JSON_BYTES,
                MAX_ARTIFACT_BYTES,
                now,
                now,
            ),
        )
        _log_event(
            connection,
            event_type="RunnerJobCreated",
            target_type="RunnerJob",
            target_id=runner_job_id,
            workspace_id=workspace_id,
            payload={
                "simulation_run_id": simulation_run_id,
                "model_version_id": payload.model_version_id,
                "status": "queued",
                "script_sha256": script_sha,
            },
        )
        _log_event(
            connection,
            event_type="SimulationRunCreated",
            target_type="SimulationRun",
            target_id=simulation_run_id,
            workspace_id=workspace_id,
            payload={"run_label": payload.run_label, "status": "queued"},
        )
        connection.commit()

    return RunnerJobCreateResponse(
        runner_job=get_runner_job(runner_job_id),
        simulation_run=get_simulation_run_detail(workspace_id, simulation_run_id),
    )


def run_runner_job(runner_job_id: str) -> RunnerJobRunResponse:
    with open_sqlite_connection() as connection:
        job = _load_runner_job(connection, runner_job_id)
        if job is None:
            raise RunnerSafetyError("runner_job_not_found", "Runner job not found.")
        workspace_id = job["workspace_id"]
        simulation_run_id = job["simulation_run_id"]
        if job["status"] != "queued":
            raise RunnerSafetyError("runner_job_not_queued", "Only queued jobs can be run in V0.")

    script_path = validate_script_path(workspace_id, job["script_path"])
    implementation_kind = job["implementation_kind"]
    simulation_run = get_simulation_run_detail(workspace_id, simulation_run_id)
    try:
        if implementation_kind not in STUDIO_IMPLEMENTATION_KINDS:
            preflight_script_policy(
                script_path,
                ast_policy=CALC_V0_IMPLEMENTATION_KIND if implementation_kind == CALC_V0_IMPLEMENTATION_KIND else None,
                ast_import_allowlist=implementation_kind == BLUECAD_L2_IMPLEMENTATION_KIND,
            )
    except RunnerSafetyError as exc:
        if (
            implementation_kind in {BLUECAD_L2_IMPLEMENTATION_KIND, CALC_V0_IMPLEMENTATION_KIND}
            and exc.code == "SANDBOX_VIOLATION"
        ):
            return _finish_failed(
                runner_job_id,
                workspace_id,
                simulation_run_id,
                status="failed",
                code=exc.code,
                message=exc.message,
            )
        raise
    script_sha = sha256_file(script_path)
    if script_sha != job["script_sha256"]:
        raise RunnerSafetyError("runner_script_hash_mismatch", "Script hash does not match runner job metadata.")

    working_dir, input_file, output_dir = validate_run_paths(
        workspace_id,
        simulation_run_id,
        working_dir=job["working_dir"],
        input_file=job["input_file"],
        output_dir=job["output_dir"],
    )
    if simulation_run.input_payload is None:
        raise RunnerSafetyError("runner_input_invalid", "Simulation run is missing input payload.")

    started_at = utc_now()
    try:
        claimed = _claim_and_mark_running(runner_job_id, workspace_id, simulation_run_id, started_at)
    except RunnerSafetyError as exc:
        if exc.code != LINKED_PARAMETER_UNUSABLE_CODE:
            raise
        return RunnerJobRunResponse(
            runner_job=get_runner_job(runner_job_id),
            simulation_run=get_simulation_run_detail(workspace_id, simulation_run_id),
            output=None,
            error={"code": exc.code, "message": exc.message},
        )
    if not claimed:
        # Another concurrent /run already claimed this queued job. Only one
        # caller may transition queued -> running, so we refuse here instead of
        # executing the script a second time.
        raise RunnerSafetyError("runner_job_not_queued", "Only queued jobs can be run in V0.")

    working_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_file.write_text(_pretty_json(simulation_run.input_payload), encoding="utf-8")

    if implementation_kind in STUDIO_IMPLEMENTATION_KINDS:
        studio = _run_studio_calc_job(
            runner_job_id, workspace_id, simulation_run_id, job, simulation_run, script_path, output_dir
        )
        if isinstance(studio, RunnerJobRunResponse):
            return studio
        output, output_payload, artifact_ids = studio
    else:
        result = execute_python_script(
            script_path=script_path,
            input_file=input_file,
            output_dir=output_dir,
            working_dir=working_dir,
            timeout_seconds=int(job["timeout_seconds"]),
            max_stdout_bytes=int(job["max_stdout_bytes"]),
            max_stderr_bytes=int(job["max_stderr_bytes"]),
        )
        _capture_logs(workspace_id, simulation_run_id, result)
        _store_execution_metadata(runner_job_id, result)

        if result.timed_out:
            return _finish_failed(
                runner_job_id,
                workspace_id,
                simulation_run_id,
                status="timed_out",
                code="runner_timeout",
                message="The runner timed out.",
            )
        if result.return_code != 0:
            return _finish_failed(
                runner_job_id,
                workspace_id,
                simulation_run_id,
                status="failed",
                code="runner_process_failed",
                message="The runner process exited with a nonzero code.",
            )

        try:
            output = _load_result_json(output_dir, int(job["max_output_json_bytes"]))
            declared_artifacts = output.get("artifacts") or []
            if not isinstance(declared_artifacts, list):
                raise RunnerSafetyError("runner_result_invalid_json", "Artifacts declaration must be a list.")
            if implementation_kind == BLUECAD_L2_IMPLEMENTATION_KIND:
                _validate_bluecad_l2_output(output_dir, declared_artifacts)
            calc_outputs: dict[str, dict[str, object]] | None = None
            if implementation_kind == CALC_V0_IMPLEMENTATION_KIND:
                calc_outputs = _validate_calc_v0_output(output)
                output_payload = _canonical_result_json(output)
                declared_artifacts = [
                    {
                        "path": "result.json",
                        "role": "calc_result_json",
                        "artifact_type": "json",
                        "mime_type": "application/json",
                    }
                ]
            artifact_ids = _register_declared_artifacts(
                workspace_id,
                simulation_run_id,
                output_dir,
                declared_artifacts,
                int(job["max_artifact_bytes"]),
            )
        except RunnerSafetyError as exc:
            return _finish_failed(
                runner_job_id,
                workspace_id,
                simulation_run_id,
                status="failed",
                code=exc.code,
                message=exc.message,
            )

        if implementation_kind == CALC_V0_IMPLEMENTATION_KIND:
            try:
                _create_calc_parameter_records(workspace_id, runner_job_id, calc_outputs or {})
            except Exception as exc:
                return _finish_failed(
                    runner_job_id,
                    workspace_id,
                    simulation_run_id,
                    status="failed",
                    code="runner_memory_facade_error",
                    message=f"Memory facade failed to create calc parameter proposals: {exc}",
                )

    completed_at = utc_now()
    if implementation_kind not in {CALC_V0_IMPLEMENTATION_KIND, *STUDIO_IMPLEMENTATION_KINDS}:
        output_payload = canonical_json(output)
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE simulation_runs
            SET status = ?, output_payload = ?, completed_at = ?
            WHERE id = ?
            """,
            ("succeeded", output_payload, completed_at, simulation_run_id),
        )
        connection.execute(
            "UPDATE runner_jobs SET status = ?, updated_at = ? WHERE id = ?",
            ("succeeded", completed_at, runner_job_id),
        )
        _log_event(
            connection,
            event_type="RunnerJobSucceeded",
            target_type="RunnerJob",
            target_id=runner_job_id,
            workspace_id=workspace_id,
            payload={
                "simulation_run_id": simulation_run_id,
                "status": "succeeded",
                "artifact_count": len(artifact_ids),
                "script_sha256": script_sha,
            },
        )
        connection.commit()

    if implementation_kind in {CALC_V0_IMPLEMENTATION_KIND, STUDIO_CALC_IMPLEMENTATION_KIND}:
        ingest_validated_calc_v0_run(workspace_id, simulation_run_id)

    return RunnerJobRunResponse(
        runner_job=get_runner_job(runner_job_id),
        simulation_run=get_simulation_run_detail(workspace_id, simulation_run_id),
        output=output,
        error=None,
    )


def _run_studio_calc_job(
    runner_job_id: str,
    workspace_id: str,
    simulation_run_id: str,
    job,
    simulation_run: SimulationRunDetail,
    script_path: Path,
    output_dir: Path,
) -> RunnerJobRunResponse | tuple[dict[str, object], str, list[str]]:
    """Execute published user code only inside the 187 isolated runtime.

    A geometry version (spec 189) runs under the fixed geometry profile; its verified STEP, STL
    and GLB are copied into the run directory after sandbox exit and registered with the facts.
    Returns the terminal failure response, or the output for the caller to record as succeeded.
    """
    geometry = job["implementation_kind"] == STUDIO_GEOMETRY_IMPLEMENTATION_KIND
    try:
        source_bytes = script_path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != job["script_sha256"]:
            raise RunnerSafetyError("runner_script_hash_mismatch", "Studio source hash changed.")
        source = source_bytes.decode("utf-8")
        with open_sqlite_connection() as connection:
            published = connection.execute(
                """SELECT contract_json, source_sha256
                   FROM studio_published_versions WHERE version_id = ?""",
                (simulation_run.model_version_id,),
            ).fetchone()
        if published is None or published["source_sha256"] != job["script_sha256"]:
            raise RunnerSafetyError("runner_studio_publication_mismatch", "Studio publication evidence changed.")
        contract = json.loads(published["contract_json"])
        units = {} if geometry else {item["name"]: item["unit"] for item in contract["outputs"]}
        inputs = {name: item["value"] for name, item in json.loads(simulation_run.input_payload or "{}").items()}
        if geometry:
            record = studio_runtime.run(studio_runtime.RunRequest(source=source, inputs=inputs),
                                        profile="geometry", artifact_dir=output_dir / "geometry")
        else:
            record = studio_runtime.run(studio_runtime.RunRequest(
                source=source,
                inputs=inputs,
                limits=studio_runtime.Limits(wall_seconds=min(int(job["timeout_seconds"]), 15)),
            ))
        if record.source_sha256 != job["script_sha256"]:
            raise RunnerSafetyError("runner_script_hash_mismatch", "Studio runtime source hash differs.")
        _capture_logs(workspace_id, simulation_run_id, LocalPythonResult(
            return_code=0 if record.classification == "ok" else 1,
            timed_out=record.classification == "timeout",
            stdout=record.stdout,
            stderr=record.stderr,
            stdout_truncated=record.logs_truncated,
            stderr_truncated=record.logs_truncated,
            command_metadata={},
            environment_metadata={},
        ))
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE runner_jobs SET command_json = ?, environment_json = ?, updated_at = ? WHERE id = ?",
                (canonical_json({"mechanism": record.mechanism, "source_sha256": record.source_sha256}),
                 canonical_json({"isolation": record.mechanism, "limits": record.limits.model_dump()}),
                 utc_now(), runner_job_id),
            )
            connection.commit()
        if record.classification != "ok":
            return _finish_failed(
                runner_job_id, workspace_id, simulation_run_id,
                status="timed_out" if record.classification == "timeout" else "failed",
                code=f"studio_runtime_{record.classification}",
                message=(f"Studio {record.profile} run stopped at the {record.limit} limit ({record.limit_value})."
                         if record.limit else f"Studio runtime returned {record.classification}"
                         + (f" ({record.artifact_error})." if record.artifact_error else ".")),
            )
        if geometry:
            return _studio_geometry_output(workspace_id, simulation_run_id, output_dir, job, record)
        if not isinstance(record.result, dict) or set(record.result) != set(units):
            raise RunnerSafetyError("runner_result_invalid_json", "Studio result does not match declared outputs.")
        output: dict[str, object] = {"outputs": {
            name: {"value": value, "unit": units[name]}
            for name, value in record.result.items()
        }}
        calc_outputs = _validate_calc_v0_output(output)
        output_payload = _canonical_result_json(output)
        result_path = output_dir / "result.json"
        result_path.write_text(output_payload, encoding="utf-8")
        artifact_ids = _register_declared_artifacts(
            workspace_id, simulation_run_id, output_dir,
            [{"path": "result.json", "role": "calc_result_json", "artifact_type": "json",
              "mime_type": "application/json"}],
            int(job["max_artifact_bytes"]),
        )
        _create_calc_parameter_records(workspace_id, runner_job_id, calc_outputs)
    except Exception as exc:  # noqa: BLE001 - claimed job must receive a terminal result
        code = exc.code if isinstance(exc, RunnerSafetyError) else "runner_studio_execution_error"
        message = exc.message if isinstance(exc, RunnerSafetyError) else "Studio run could not be completed."
        return _finish_failed(
            runner_job_id, workspace_id, simulation_run_id,
            status="failed", code=code, message=message,
        )

    return output, output_payload, artifact_ids


_GEOMETRY_MIME = {"model.step": "application/step", "model.stl": "model/stl", "model.glb": "model/gltf-binary"}


def _studio_geometry_output(
    workspace_id: str, simulation_run_id: str, output_dir: Path, job, record: studio_runtime.RunRecord,
) -> tuple[dict[str, object], str, list[str]]:
    """Register the admitted geometry artifacts and the shape facts as one run output."""
    facts = record.result
    if not isinstance(facts, dict) or set(facts) != {"valid", "solids", "volume", "bbox_min", "bbox_max"}:
        raise RunnerSafetyError("runner_result_invalid_json", "Studio geometry facts are invalid.")
    output: dict[str, object] = {"geometry": facts, "artifacts": [
        {"name": item.name, "sha256": item.sha256, "size_bytes": item.size_bytes} for item in record.artifacts]}
    output_payload = _canonical_result_json(output)
    (output_dir / "result.json").write_text(output_payload, encoding="utf-8")
    declared = [{"path": "result.json", "role": "studio_geometry_facts", "artifact_type": "json",
                 "mime_type": "application/json"}]
    declared += [{"path": f"geometry/{item.name}", "role": "studio_geometry", "artifact_type": "studio_geometry",
                  "mime_type": _GEOMETRY_MIME[item.name]} for item in record.artifacts]
    artifact_ids = _register_declared_artifacts(
        workspace_id, simulation_run_id, output_dir, declared, int(job["max_artifact_bytes"]))
    return output, output_payload, artifact_ids


def get_runner_job(runner_job_id: str) -> RunnerJobRead:
    with open_sqlite_connection() as connection:
        row = _load_runner_job(connection, runner_job_id)
    if row is None:
        raise RunnerSafetyError("runner_job_not_found", "Runner job not found.")
    return _runner_job_from_row(row)


def get_simulation_run_detail(workspace_id: str, simulation_run_id: str) -> SimulationRunDetail:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT * FROM simulation_runs WHERE id = ? AND workspace_id = ?",
            (simulation_run_id, workspace_id),
        ).fetchone()
        if row is not None:
            version = connection.execute(
                "SELECT implementation_kind FROM model_versions WHERE id = ?",
                (row["model_version_id"],),
            ).fetchone()
            stale = False
            superseded = False
            if version is not None and version["implementation_kind"] in STUDIO_IMPLEMENTATION_KINDS:
                try:
                    require_linked_parameters_usable(
                        connection, workspace_id, row["input_payload"] or "{}", studio_pint=True
                    )
                except RunnerSafetyError as exc:
                    if exc.code != LINKED_PARAMETER_UNUSABLE_CODE:
                        raise
                    stale = True
                superseded = connection.execute(
                    """SELECT 1 FROM studio_published_versions newer
                       JOIN studio_published_versions ran ON ran.model_spec_id = newer.model_spec_id
                       WHERE ran.version_id = ? AND newer.rowid > ran.rowid LIMIT 1""",
                    (row["model_version_id"],),
                ).fetchone() is not None
    if row is None:
        raise RunnerSafetyError("runner_simulation_run_not_found", "Simulation run not found.")
    return SimulationRunDetail(**dict(row), stale=stale, superseded=superseded)


def list_run_logs(workspace_id: str, simulation_run_id: str) -> list[RunLogRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            """
            SELECT * FROM run_logs
            WHERE workspace_id = ? AND simulation_run_id = ?
            ORDER BY created_at ASC
            """,
            (workspace_id, simulation_run_id),
        ).fetchall()
    return [
        RunLogRead(
            id=row["id"],
            workspace_id=row["workspace_id"],
            simulation_run_id=row["simulation_run_id"],
            stream=row["stream"],
            content=row["content"],
            truncated=bool(row["truncated"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def list_run_artifacts(workspace_id: str, simulation_run_id: str) -> list[RunArtifactRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        run = connection.execute(
            "SELECT id FROM simulation_runs WHERE id = ? AND workspace_id = ?",
            (simulation_run_id, workspace_id),
        ).fetchone()
        if run is None:
            raise RunnerSafetyError("runner_simulation_run_not_found", "Simulation run not found.")
        rows = connection.execute(
            """
            SELECT
                ra.simulation_run_id,
                ra.role,
                a.id AS artifact_id,
                a.workspace_id,
                a.filename,
                a.stored_path,
                a.artifact_type,
                a.mime_type,
                a.sha256,
                a.source_ref,
                a.status,
                a.created_at
            FROM run_artifacts ra
            JOIN artifacts a ON a.id = ra.artifact_id
            WHERE ra.workspace_id = ? AND ra.simulation_run_id = ?
            ORDER BY a.created_at ASC
            """,
            (workspace_id, simulation_run_id),
        ).fetchall()
    return [_run_artifact_from_row(row) for row in rows]


def _claim_and_mark_running(runner_job_id: str, workspace_id: str, simulation_run_id: str, started_at: str) -> bool:
    """Atomically transition a queued job to running after strict linked-source revalidation."""
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT status FROM runner_jobs WHERE id = ? AND workspace_id = ? AND simulation_run_id = ?",
            (runner_job_id, workspace_id, simulation_run_id),
        ).fetchone()
        if job is None or job["status"] != "queued":
            connection.rollback()
            return False

        run = connection.execute(
            """
            SELECT input_payload, model_version_id
            FROM simulation_runs
            WHERE id = ? AND workspace_id = ?
            """,
            (simulation_run_id, workspace_id),
        ).fetchone()
        if run is None or run["input_payload"] is None or run["model_version_id"] is None:
            connection.rollback()
            raise RunnerSafetyError("runner_input_invalid", "Simulation run is missing input payload.")

        model_version = connection.execute(
            """
            SELECT input_contract_payload, input_contract_sha256, implementation_kind
            FROM model_versions
            WHERE id = ? AND workspace_id = ?
            """,
            (run["model_version_id"], workspace_id),
        ).fetchone()
        if model_version is None:
            connection.rollback()
            raise RunnerSafetyError("runner_model_version_not_found", "Model implementation not found.")

        if contract_requires_canonical_linked_parameters(
            model_version["input_contract_payload"],
            model_version["input_contract_sha256"],
        ):
            try:
                require_linked_parameters_usable(
                    connection, workspace_id, run["input_payload"],
                    studio_pint=model_version["implementation_kind"] in STUDIO_IMPLEMENTATION_KINDS,
                )
            except RunnerSafetyError as exc:
                if exc.code != LINKED_PARAMETER_UNUSABLE_CODE:
                    connection.rollback()
                    raise
                error_payload = {
                    "status": "failed",
                    "error": {
                        "code": LINKED_PARAMETER_UNUSABLE_CODE,
                        "message": LINKED_PARAMETER_UNUSABLE_MESSAGE,
                    },
                }
                connection.execute(
                    """
                    UPDATE simulation_runs
                    SET status = ?, output_payload = ?, completed_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    ("failed", canonical_json(error_payload), started_at, simulation_run_id),
                )
                connection.execute(
                    """
                    UPDATE runner_jobs
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    ("failed", started_at, runner_job_id),
                )
                _log_event(
                    connection,
                    event_type="RunnerJobFailed",
                    target_type="RunnerJob",
                    target_id=runner_job_id,
                    workspace_id=workspace_id,
                    payload={
                        "simulation_run_id": simulation_run_id,
                        "status": "failed",
                        "error_code": LINKED_PARAMETER_UNUSABLE_CODE,
                    },
                )
                connection.commit()
                raise RunnerSafetyError(
                    LINKED_PARAMETER_UNUSABLE_CODE,
                    LINKED_PARAMETER_UNUSABLE_MESSAGE,
                ) from exc

        cursor = connection.execute(
            "UPDATE runner_jobs SET status = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
            ("running", started_at, runner_job_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.execute(
            "UPDATE simulation_runs SET status = ?, started_at = ? WHERE id = ?",
            ("running", started_at, simulation_run_id),
        )
        _log_event(
            connection,
            event_type="RunnerJobStarted",
            target_type="RunnerJob",
            target_id=runner_job_id,
            workspace_id=workspace_id,
            payload={"simulation_run_id": simulation_run_id, "status": "running"},
        )
        connection.commit()
    return True


def _finish_failed(
    runner_job_id: str,
    workspace_id: str,
    simulation_run_id: str,
    *,
    status: str,
    code: str,
    message: str,
) -> RunnerJobRunResponse:
    completed_at = utc_now()
    error_payload = {"status": status, "error": {"code": code, "message": message}}
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE simulation_runs
            SET status = ?, output_payload = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, canonical_json(error_payload), completed_at, simulation_run_id),
        )
        connection.execute(
            "UPDATE runner_jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, completed_at, runner_job_id),
        )
        _log_event(
            connection,
            event_type="RunnerJobTimedOut" if status == "timed_out" else "RunnerJobFailed",
            target_type="RunnerJob",
            target_id=runner_job_id,
            workspace_id=workspace_id,
            payload={"simulation_run_id": simulation_run_id, "status": status, "error_code": code},
        )
        connection.commit()
    return RunnerJobRunResponse(
        runner_job=get_runner_job(runner_job_id),
        simulation_run=get_simulation_run_detail(workspace_id, simulation_run_id),
        output=None,
        error={"code": code, "message": message},
    )


def _capture_logs(workspace_id: str, simulation_run_id: str, result: LocalPythonResult) -> None:
    with open_sqlite_connection() as connection:
        for stream, content, truncated in [
            ("stdout", result.stdout, result.stdout_truncated),
            ("stderr", result.stderr, result.stderr_truncated),
        ]:
            if not content:
                continue
            connection.execute(
                """
                INSERT INTO run_logs (
                    id, workspace_id, simulation_run_id, stream, content, truncated, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), workspace_id, simulation_run_id, stream, content, int(truncated), utc_now()),
            )
        connection.commit()


def _store_execution_metadata(runner_job_id: str, result: LocalPythonResult) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE runner_jobs
            SET command_json = ?, environment_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                canonical_json(result.command_metadata),
                canonical_json(result.environment_metadata),
                utc_now(),
                runner_job_id,
            ),
        )
        connection.commit()


def _load_result_json(working_dir: Path, max_bytes: int) -> dict[str, object]:
    result_path = working_dir / "result.json"
    if not result_path.exists():
        raise RunnerSafetyError("runner_result_missing", "The runner did not produce result.json.")
    if result_path.stat().st_size > max_bytes:
        raise RunnerSafetyError("runner_output_too_large", "The runner result JSON exceeds the V0 size limit.")
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunnerSafetyError("runner_result_invalid_json", "The runner did not produce valid result JSON.") from exc
    if not isinstance(data, dict):
        raise RunnerSafetyError("runner_result_invalid_json", "Runner result JSON must be an object.")
    return data


def _register_declared_artifacts(
    workspace_id: str,
    simulation_run_id: str,
    output_dir: Path,
    artifacts: Sequence[object],
    max_artifact_bytes: int,
) -> list[str]:
    artifact_ids: list[str] = []
    with open_sqlite_connection() as connection:
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise RunnerSafetyError("runner_result_invalid_json", "Artifact declarations must be objects.")
            path_value = str(artifact.get("path") or "")
            artifact_path = safe_artifact_path(output_dir, path_value)
            if not artifact_path.exists():
                raise RunnerSafetyError("runner_artifact_missing", "Declared artifact file is missing.")
            if artifact_path.stat().st_size > max_artifact_bytes:
                raise RunnerSafetyError("runner_artifact_too_large", "Declared artifact exceeds V0 size limit.")

            artifact_id = str(uuid4())
            role = str(artifact.get("role") or "other")
            mime_type = str(artifact.get("mime_type") or mimetypes.guess_type(artifact_path.name)[0] or "")
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, workspace_id, filename, stored_path, artifact_type, mime_type,
                    sha256, source_ref, status, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    workspace_id,
                    artifact_path.name,
                    str(artifact_path),
                    str(artifact.get("artifact_type") or role),
                    mime_type,
                    sha256_file(artifact_path),
                    f"simulation_run:{simulation_run_id}",
                    "registered",
                    utc_now(),
                    "Generated by Python Runner V0.",
                ),
            )
            connection.execute(
                """
                INSERT INTO run_artifacts (
                    id, workspace_id, simulation_run_id, artifact_id, role, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), workspace_id, simulation_run_id, artifact_id, role, utc_now(), None),
            )
            _log_event(
                connection,
                event_type="RunArtifactRegistered",
                target_type="Artifact",
                target_id=artifact_id,
                workspace_id=workspace_id,
                payload={"simulation_run_id": simulation_run_id, "role": role},
            )
            artifact_ids.append(artifact_id)
        connection.commit()
    return artifact_ids


def _canonical_result_json(output: dict[str, object]) -> str:
    try:
        return canonical_json(output)
    except RunnerSafetyError as exc:
        raise RunnerSafetyError(
            "runner_result_invalid_json", "Runner result JSON must be finite canonical JSON."
        ) from exc


def _validate_calc_v0_output(output: dict[str, object]) -> dict[str, dict[str, object]]:
    outputs = output.get("outputs")
    if not isinstance(outputs, dict):
        raise RunnerSafetyError("runner_result_invalid_json", "calc_v0 result must include an outputs object.")
    normalized: dict[str, dict[str, object]] = {}
    for name, item in outputs.items():
        if not isinstance(name, str) or not name.strip():
            raise RunnerSafetyError("runner_result_invalid_json", "calc_v0 output names must be non-empty strings.")
        if not isinstance(item, dict):
            raise RunnerSafetyError("runner_result_invalid_json", f"calc_v0 output {name} must be an object.")
        value = item.get("value")
        if isinstance(value, bool):
            raise RunnerSafetyError("runner_result_invalid_json", f"calc_v0 output {name} value must be numeric.")
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise RunnerSafetyError(
                "runner_result_invalid_json", f"calc_v0 output {name} value must be numeric."
            ) from exc
        if not isfinite(number):
            raise RunnerSafetyError("runner_result_invalid_json", f"calc_v0 output {name} value must be finite.")
        if not isinstance(item.get("unit"), str) or not str(item.get("unit")).strip():
            raise RunnerSafetyError("runner_output_unit_missing", f"calc_v0 output {name} unit is required.")
        normalized[name] = {"value": number, "unit": str(item["unit"])}
    return normalized


def _create_calc_parameter_records(
    workspace_id: str, runner_job_id: str, outputs: dict[str, dict[str, object]]
) -> None:
    payloads = [
        CalcParameterProposalCreate(
            workspace_id=workspace_id,
            runner_job_id=runner_job_id,
            name=name,
            value=str(output["value"]),
            unit=str(output["unit"]),
            notes="Created from calc_v0 runner output.",
        )
        for name, output in outputs.items()
    ]
    records = create_calc_parameter_proposals(payloads)
    if len(records) != len(payloads):
        raise RuntimeError("Memory facade did not return one record per calc output.")


def _validate_bluecad_l2_output(output_dir: Path, artifacts: list[object]) -> None:
    roles: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RunnerSafetyError("runner_result_invalid_json", "Artifact declarations must be objects.")
        role = str(artifact.get("role") or "")
        path_value = str(artifact.get("path") or "")
        if role in BLUECAD_L2_REQUIRED_ARTIFACTS:
            roles[role] = path_value

    missing_roles = sorted(set(BLUECAD_L2_REQUIRED_ARTIFACTS) - set(roles))
    if missing_roles:
        raise RunnerSafetyError(
            "runner_bluecad_output_invalid",
            f"Missing required BLUECAD artifact roles: {', '.join(missing_roles)}.",
        )
    for role, required_filename in BLUECAD_L2_REQUIRED_ARTIFACTS.items():
        relative_path = roles[role]
        if Path(relative_path).name != required_filename:
            raise RunnerSafetyError(
                "runner_bluecad_output_invalid",
                f"{role} must declare required filename {required_filename}.",
            )
        safe_artifact_path(output_dir, relative_path)



def _load_runner_job(connection, runner_job_id: str):
    return connection.execute("SELECT * FROM runner_jobs WHERE id = ?", (runner_job_id,)).fetchone()


def _load_model_version_with_artifact(connection, workspace_id: str, model_version_id: str):
    row = connection.execute(
        """
        SELECT mv.*, a.sha256 AS script_sha256, a.stored_path AS script_path
        FROM model_versions mv
        JOIN artifacts a ON a.id = mv.implementation_artifact_id
        WHERE mv.id = ? AND mv.workspace_id = ?
        """,
        (model_version_id, workspace_id),
    ).fetchone()
    if row is None:
        raise RunnerSafetyError("runner_model_version_not_found", "Model implementation not found.")
    return row


def _runner_job_from_row(row) -> RunnerJobRead:
    data = dict(row)
    return RunnerJobRead(
        id=data["id"],
        workspace_id=data["workspace_id"],
        simulation_run_id=data["simulation_run_id"],
        runner_type=data["runner_type"],
        status=data["status"],
        script_path=data["script_path"],
        script_sha256=data["script_sha256"],
        command_metadata=_json_or_none(data["command_json"]),
        environment_metadata=_json_or_none(data["environment_json"]),
        working_dir=data["working_dir"],
        input_file=data["input_file"],
        output_dir=data["output_dir"],
        timeout_seconds=int(data["timeout_seconds"]),
        max_stdout_bytes=int(data["max_stdout_bytes"]),
        max_stderr_bytes=int(data["max_stderr_bytes"]),
        max_output_json_bytes=int(data["max_output_json_bytes"]),
        max_artifact_bytes=int(data["max_artifact_bytes"]),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _model_implementation_from_row(row) -> ModelImplementationRead:
    data = dict(row)
    return ModelImplementationRead(
        id=data["id"],
        workspace_id=data["workspace_id"],
        model_spec_id=data["model_spec_id"],
        version_label=data["version_label"],
        implementation_artifact_id=data["implementation_artifact_id"],
        status=data["status"],
        implementation_kind=data["implementation_kind"],
        script_sha256=data["script_sha256"],
        script_path=data["script_path"],
        created_at=data["created_at"],
        notes=data["notes"],
        input_contract=_json_or_none(data.get("input_contract_payload")),
        input_contract_sha256=data.get("input_contract_sha256"),
    )


def _run_artifact_from_row(row) -> RunArtifactRead:
    data = dict(row)
    data_root = build_paths().data_root.resolve()
    stored_path_value = str(data["stored_path"])
    stored_path = Path(stored_path_value).resolve()
    under_data_root = False
    relative_path: str | None = None
    safe_stored_path: str | None = None
    size_bytes: int | None = None
    try:
        relative_path = str(stored_path.relative_to(data_root))
        under_data_root = True
    except ValueError:
        relative_path = None

    if under_data_root:
        safe_stored_path = str(stored_path)
        if stored_path.exists() and stored_path.is_file():
            size_bytes = stored_path.stat().st_size

    source_ref = data["source_ref"]
    source_module = "python_runner_v0" if source_ref == f"simulation_run:{data['simulation_run_id']}" else None
    return RunArtifactRead(
        artifact_id=data["artifact_id"],
        workspace_id=data["workspace_id"],
        simulation_run_id=data["simulation_run_id"],
        role=data["role"],
        artifact_type=data["artifact_type"],
        filename=data["filename"],
        relative_path=relative_path,
        stored_path=safe_stored_path,
        size_bytes=size_bytes,
        created_at=data["created_at"],
        source_ref=source_ref,
        source_module=source_module,
        mime_type=data["mime_type"],
        sha256=data["sha256"],
        status=data["status"],
        under_data_root=under_data_root,
    )


def _require_workspace(connection, workspace_id: str) -> None:
    row = connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise RunnerSafetyError("runner_workspace_not_found", "Workspace not found.")


def _log_event(
    connection, *, event_type: str, target_type: str, target_id: str, workspace_id: str, payload: dict[str, object]
) -> None:
    log_event(
        connection,
        event_type=event_type,
        actor="local-user",
        target_type=target_type,
        target_id=target_id,
        workspace_id=workspace_id,
        payload=payload,
    )


def _example_script_path() -> Path:
    return Path(__file__).resolve().parent / "examples" / "batch_growth.py"


def _pretty_json(canonical_payload: str) -> str:
    return json.dumps(json.loads(canonical_payload), indent=2)


def _json_or_none(value: str | None):
    return json.loads(value) if value else None
