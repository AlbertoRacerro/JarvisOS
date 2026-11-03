from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.runner import service as _base
from app.modules.runner.input_contracts import (
    ModelInputContractV2,
    ModelInputContractV3,
    normalize_input_set_v2,
    parse_stored_input_contract,
)
from app.modules.runner.linked_parameters import require_linked_parameters_usable
from app.modules.runner.models import (
    BindingPreviewRequest,
    ModelImplementationCreate,
    ModelImplementationRead,
    RunnerJobCreate,
    RunnerJobCreateResponse,
    RunnerJobRunResponse,
)
from app.modules.runner.public_models import PublicModelImplementationCreate
from app.modules.runner.safety import RunnerSafetyError, sha256_file
from app.modules.studio_runtime import runtime as studio_runtime

RUNNER_SCRIPT_POLICY_VIOLATION = "RUNNER_SCRIPT_POLICY_VIOLATION"
STUDIO_CALC_KIND = "studio_calc_v1"
STUDIO_GEOMETRY_KIND = "studio_geometry_v1"
STUDIO_KINDS = frozenset({STUDIO_CALC_KIND, STUDIO_GEOMETRY_KIND})


def _job_timeout(payload: RunnerJobCreate, implementation_kind: str) -> int:
    # A geometry run always uses the fixed geometry profile wall time (spec 189).
    if implementation_kind == STUDIO_GEOMETRY_KIND:
        return studio_runtime.PROFILES["geometry"]["wall_seconds"]
    return min(payload.timeout_seconds or _base.DEFAULT_TIMEOUT_SECONDS, 60)
_LEGACY_SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
def create_model_implementation(
    workspace_id: str,
    payload: PublicModelImplementationCreate,
) -> ModelImplementationRead:
    """Register only the fixed bundled batch-growth implementation.

    Reviewed calc models use their dedicated bundled registration routes.
    BLUECAD L2 remains a historical profile but is not instantiable.
    """

    if payload.implementation_kind != _base.IMPLEMENTATION_KIND:
        raise RunnerSafetyError(
            "runner_implementation_kind_unsupported",
            "Normal registration supports only the bundled batch_growth_v0 implementation.",
        )
    return _base.create_model_implementation(
        workspace_id,
        ModelImplementationCreate(
            model_spec_id=payload.model_spec_id,
            version_label=payload.version_label,
            implementation_kind=_base.IMPLEMENTATION_KIND,
            notes=payload.notes,
            input_contract=payload.input_contract,
        ),
    )


def create_runner_job(
    workspace_id: str,
    payload: RunnerJobCreate,
) -> RunnerJobCreateResponse:
    replay = _replay_existing_runner_job(workspace_id, payload)
    if replay is not None:
        return replay

    model_version = _load_model_version(workspace_id, payload.model_version_id)
    _require_permitted_implementation(model_version)
    if model_version["implementation_kind"] in STUDIO_KINDS:
        preview = _base.preview_model_bindings(
            workspace_id, payload.model_version_id, BindingPreviewRequest(bindings=payload.input_set)
        )
        if preview.state != "ready" or preview.normalized_input_set is None:
            raise RunnerSafetyError("runner_input_invalid", "Studio inputs do not satisfy the published v2 contract.")
        payload = payload.model_copy(update={"input_set": preview.normalized_input_set})
    try:
        return _create_runner_job_idempotent(workspace_id, payload)
    except RunnerSafetyError as exc:
        translated = _translate_policy_error(exc)
        if translated is exc:
            raise
        raise translated from exc


def _replay_existing_runner_job(
    workspace_id: str,
    payload: RunnerJobCreate,
) -> RunnerJobCreateResponse | None:
    if payload.request_key is None:
        return None

    with open_sqlite_connection() as connection:
        existing = connection.execute(
            """
            SELECT
                rj.id AS runner_job_id,
                rj.simulation_run_id,
                rj.timeout_seconds,
                rj.implementation_kind,
                sr.model_version_id,
                sr.run_label,
                sr.input_payload,
                sr.parameter_payload
            FROM runner_jobs rj
            JOIN simulation_runs sr ON sr.id = rj.simulation_run_id
            WHERE rj.workspace_id = ? AND rj.request_key = ?
            """,
            (workspace_id, payload.request_key),
        ).fetchone()
    if existing is None:
        return None

    try:
        if existing["implementation_kind"] == _base.IMPLEMENTATION_KIND:
            input_payload, parameter_payload = _base.validate_batch_growth_input(payload.input_set)
        elif existing["implementation_kind"] == _base.BLUECAD_L2_IMPLEMENTATION_KIND:
            input_payload, parameter_payload = _base.validate_bluecad_l2_input(payload.input_set)
        elif existing["implementation_kind"] == _base.CALC_V0_IMPLEMENTATION_KIND:
            input_payload, parameter_payload = _base.validate_calc_v0_input(payload.input_set)
        elif existing["implementation_kind"] in STUDIO_KINDS:
            model_version = _load_model_version(workspace_id, payload.model_version_id)
            contract, _ = parse_stored_input_contract(
                model_version["input_contract_payload"], model_version["input_contract_sha256"]
            )
            if not isinstance(contract, ModelInputContractV2):
                raise RunnerSafetyError("runner_input_contract_invalid", "Studio input contract is not v2.")
            candidate = normalize_input_set_v2(contract, payload.input_set, studio_pint=True)
            stored = json.loads(existing["input_payload"])
            if set(candidate) != set(stored) or any(
                any(candidate[name].get(field) != stored[name].get(field)
                    for field in ("value", "unit", "source_parameter_id"))
                or ("source_parameter_updated_at" in payload.input_set[name]
                    and payload.input_set[name]["source_parameter_updated_at"]
                    != stored[name].get("source_parameter_updated_at"))
                for name in candidate
            ):
                raise RunnerSafetyError("runner_request_key_conflict", "Runner request key has different Studio inputs.")
            input_payload = existing["input_payload"]
            parameter_payload = existing["parameter_payload"]
        else:
            raise RunnerSafetyError("runner_implementation_kind_unsupported", "Unsupported implementation kind.")
    except RunnerSafetyError as exc:
        raise RunnerSafetyError(
            "runner_request_key_conflict",
            "Runner request key is already bound to a different create payload.",
        ) from exc

    same_payload = (
        existing["model_version_id"] == payload.model_version_id
        and existing["run_label"] == payload.run_label
        and existing["input_payload"] == input_payload
        and existing["parameter_payload"] == parameter_payload
        and int(existing["timeout_seconds"]) == _job_timeout(payload, existing["implementation_kind"])
    )
    if not same_payload:
        raise RunnerSafetyError(
            "runner_request_key_conflict",
            "Runner request key is already bound to a different create payload.",
        )
    return RunnerJobCreateResponse(
        runner_job=_base.get_runner_job(str(existing["runner_job_id"])),
        simulation_run=_base.get_simulation_run_detail(workspace_id, str(existing["simulation_run_id"])),
    )


def _create_runner_job_idempotent(
    workspace_id: str,
    payload: RunnerJobCreate,
) -> RunnerJobCreateResponse:
    now = _base.utc_now()

    with open_sqlite_connection() as connection:
        _base._require_workspace(connection, workspace_id)
        model_version = _base._load_model_version_with_artifact(
            connection,
            workspace_id,
            payload.model_version_id,
        )
        implementation_kind = model_version["implementation_kind"]
        timeout_seconds = _job_timeout(payload, implementation_kind)
        if implementation_kind == _base.IMPLEMENTATION_KIND:
            input_payload, parameter_payload = _base.validate_batch_growth_input(payload.input_set)
        elif implementation_kind == _base.BLUECAD_L2_IMPLEMENTATION_KIND:
            input_payload, parameter_payload = _base.validate_bluecad_l2_input(payload.input_set)
        elif implementation_kind == _base.CALC_V0_IMPLEMENTATION_KIND:
            input_payload, parameter_payload = _base.validate_calc_v0_input(payload.input_set)
        elif implementation_kind in STUDIO_KINDS:
            input_payload, parameter_payload = _base.validate_calc_v0_input(payload.input_set)
        else:
            raise RunnerSafetyError("runner_implementation_kind_unsupported", "Unsupported implementation kind.")

        script_path = _base.validate_script_path(workspace_id, model_version["script_path"])
        script_sha = _base.sha256_file(script_path)
        if script_sha != model_version["script_sha256"]:
            raise RunnerSafetyError("runner_script_hash_mismatch", "Script hash does not match registered artifact.")
        if implementation_kind == _base.BLUECAD_L2_IMPLEMENTATION_KIND:
            _base.preflight_script_policy(script_path, ast_import_allowlist=True)
        elif implementation_kind == _base.CALC_V0_IMPLEMENTATION_KIND:
            _base.preflight_script_policy(script_path, ast_policy=_base.CALC_V0_IMPLEMENTATION_KIND)

        # Every genuinely new create serializes its final linked-source decision
        # with run/job persistence. A same-key committed replay remains
        # reconciliation and is returned before current freshness is consulted.
        connection.execute("BEGIN IMMEDIATE")
        if payload.request_key is not None:
            existing = connection.execute(
                """
                SELECT
                    rj.id AS runner_job_id,
                    rj.simulation_run_id,
                    rj.timeout_seconds,
                    sr.model_version_id,
                    sr.run_label,
                    sr.input_payload,
                    sr.parameter_payload
                FROM runner_jobs rj
                JOIN simulation_runs sr ON sr.id = rj.simulation_run_id
                WHERE rj.workspace_id = ? AND rj.request_key = ?
                """,
                (workspace_id, payload.request_key),
            ).fetchone()
            if existing is not None:
                same_payload = (
                    existing["model_version_id"] == payload.model_version_id
                    and existing["run_label"] == payload.run_label
                    and existing["input_payload"] == input_payload
                    and existing["parameter_payload"] == parameter_payload
                    and int(existing["timeout_seconds"]) == timeout_seconds
                )
                runner_job_id = str(existing["runner_job_id"])
                simulation_run_id = str(existing["simulation_run_id"])
                connection.rollback()
                if not same_payload:
                    raise RunnerSafetyError(
                        "runner_request_key_conflict",
                        "Runner request key is already bound to a different create payload.",
                    )
                return RunnerJobCreateResponse(
                    runner_job=_base.get_runner_job(runner_job_id),
                    simulation_run=_base.get_simulation_run_detail(workspace_id, simulation_run_id),
                )

        if _uses_canonical_linked_parameter_contract(model_version):
            require_linked_parameters_usable(
                connection, workspace_id, input_payload,
                studio_pint=implementation_kind in STUDIO_KINDS,
            )

        simulation_run_id = str(uuid4())
        runner_job_id = str(uuid4())
        job_run_root = _base.run_root(workspace_id, simulation_run_id)
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
                max_artifact_bytes, request_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                runner_job_id,
                workspace_id,
                simulation_run_id,
                _base.RUNNER_TYPE,
                "queued",
                str(script_path),
                script_sha,
                implementation_kind,
                None,
                None,
                str(job_run_root),
                str(job_run_root / "input.json"),
                str(job_run_root),
                timeout_seconds,
                _base.MAX_STDOUT_BYTES,
                _base.MAX_STDERR_BYTES,
                _base.MAX_OUTPUT_JSON_BYTES,
                (studio_runtime.PROFILES["geometry"]["file_bytes"] if implementation_kind == STUDIO_GEOMETRY_KIND
                 else _base.MAX_ARTIFACT_BYTES),
                payload.request_key,
                now,
                now,
            ),
        )
        _base._log_event(
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
        _base._log_event(
            connection,
            event_type="SimulationRunCreated",
            target_type="SimulationRun",
            target_id=simulation_run_id,
            workspace_id=workspace_id,
            payload={"run_label": payload.run_label, "status": "queued"},
        )
        connection.commit()

    return RunnerJobCreateResponse(
        runner_job=_base.get_runner_job(runner_job_id),
        simulation_run=_base.get_simulation_run_detail(workspace_id, simulation_run_id),
    )


def run_runner_job(runner_job_id: str) -> RunnerJobRunResponse:
    model_version = _load_job_model_version(runner_job_id)
    _require_permitted_implementation(model_version)
    try:
        response = _base.run_runner_job(runner_job_id)
    except RunnerSafetyError as exc:
        translated = _translate_policy_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    if response.error and response.error.get("code") == _LEGACY_SANDBOX_VIOLATION:
        error = dict(response.error)
        error["code"] = RUNNER_SCRIPT_POLICY_VIOLATION
        return response.model_copy(update={"error": error})
    return response


def _uses_canonical_linked_parameter_contract(model_version: Any) -> bool:
    contract_payload = model_version["input_contract_payload"]
    contract_sha256 = model_version["input_contract_sha256"]
    if not contract_payload or not contract_sha256:
        return False
    contract, _ = parse_stored_input_contract(contract_payload, contract_sha256)
    return isinstance(contract, (ModelInputContractV2, ModelInputContractV3))


def _translate_policy_error(exc: RunnerSafetyError) -> RunnerSafetyError:
    if exc.code == _LEGACY_SANDBOX_VIOLATION:
        return RunnerSafetyError(RUNNER_SCRIPT_POLICY_VIOLATION, exc.message)
    return exc


def _require_exact_bundled(model_version: dict[str, Any]) -> None:
    if not _is_exact_bundled(model_version):
        raise RunnerSafetyError(
            RUNNER_SCRIPT_POLICY_VIOLATION,
            "Runner execution is limited to exact server-known bundled implementations.",
        )


def _require_permitted_implementation(model_version: dict[str, Any]) -> None:
    if model_version.get("implementation_kind") not in STUDIO_KINDS:
        _require_exact_bundled(model_version)
        return
    if model_version.get("status") != "published":
        raise RunnerSafetyError(RUNNER_SCRIPT_POLICY_VIOLATION, "Studio model is not published.")
    contract, _ = parse_stored_input_contract(
        model_version.get("input_contract_payload"), model_version.get("input_contract_sha256")
    )
    if not isinstance(contract, ModelInputContractV2):
        raise RunnerSafetyError(RUNNER_SCRIPT_POLICY_VIOLATION, "Studio model requires a v2 input contract.")
    script_path = _base.validate_script_path(model_version["workspace_id"], model_version["script_path"])
    if sha256_file(script_path) != model_version["script_sha256"]:
        raise RunnerSafetyError("runner_script_hash_mismatch", "Studio source hash does not match its artifact.")
    with open_sqlite_connection() as connection:
        published = connection.execute(
            """SELECT source_sha256 FROM studio_published_versions
               WHERE version_id = ? AND model_spec_id = ?""",
            (model_version["id"], model_version["model_spec_id"]),
        ).fetchone()
    if published is None or published["source_sha256"] != model_version["script_sha256"]:
        raise RunnerSafetyError(RUNNER_SCRIPT_POLICY_VIOLATION, "Studio publication evidence does not match.")
    state = studio_runtime.status()
    if not state["enabled"] or not state["available"]:
        raise RunnerSafetyError("studio_runtime_unavailable", f"Studio runtime is {state['reason']}.")


def _is_exact_bundled(model_version: dict[str, Any]) -> bool:
    stored_path = model_version.get("script_path")
    stored_sha = model_version.get("script_sha256")
    if not isinstance(stored_path, str) or not isinstance(stored_sha, str):
        return False
    try:
        if sha256_file(Path(stored_path)) != stored_sha:
            return False
    except (OSError, ValueError):
        return False

    # Only the fixed batch-growth example is a server-known bundled implementation;
    # every other kind is gated elsewhere (Studio) or not executable.
    if model_version.get("implementation_kind") == _base.IMPLEMENTATION_KIND:
        return stored_sha == sha256_file(_base._example_script_path())
    return False


def _load_model_version(workspace_id: str, model_version_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT mv.*, a.sha256 AS script_sha256, a.stored_path AS script_path
            FROM model_versions mv
            JOIN artifacts a ON a.id = mv.implementation_artifact_id
            WHERE mv.workspace_id = ? AND mv.id = ?
            """,
            (workspace_id, model_version_id),
        ).fetchone()
    if row is None:
        raise RunnerSafetyError("runner_model_version_not_found", "Model implementation not found.")
    return dict(row)


def _load_job_model_version(runner_job_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT mv.*, a.sha256 AS script_sha256, a.stored_path AS script_path
            FROM runner_jobs rj
            JOIN simulation_runs sr ON sr.id = rj.simulation_run_id
            JOIN model_versions mv ON mv.id = sr.model_version_id
            JOIN artifacts a ON a.id = mv.implementation_artifact_id
            WHERE rj.id = ?
            """,
            (runner_job_id,),
        ).fetchone()
    if row is None:
        raise RunnerSafetyError("runner_job_not_found", "Runner job not found.")
    return dict(row)


# Read-only paths remain owned by the reviewed base service.
list_model_implementations = _base.list_model_implementations
preview_model_bindings = _base.preview_model_bindings
get_simulation_run_detail = _base.get_simulation_run_detail
list_run_artifacts = _base.list_run_artifacts
list_run_logs = _base.list_run_logs
