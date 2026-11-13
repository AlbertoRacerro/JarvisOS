from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.runner.linked_parameters import (
    contract_requires_canonical_linked_parameters,
    linked_parameter_items,
)


class ValueHistoryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RunLinkedParameterItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    variable_name: str
    id: str
    name: str
    value: Any
    unit: str
    status: str
    lifecycle_state: str | None = None
    position: int
    newer_version_exists: bool
    source_parameter_updated_at: str | None = None


class RunInputsManifestRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    simulation_run_id: str
    workspace_id: str
    status: str
    history_not_recorded: bool
    history_recorded: bool
    reason: str | None = None
    items: list[RunLinkedParameterItem] = Field(default_factory=list)


class ValueVersionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version_index: int
    id: str
    value: Any
    unit: str
    conditions: dict[str, Any] = Field(default_factory=dict)
    status: str
    validation_state: str
    action: str | None = None
    source: str
    actor: str | None = None
    reason: str | None = None
    created_at: str
    origin: str | None = None


class ValueVersionChainRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value_kind: str
    value_id: str
    workspace_id: str
    intermediate_versions_unknown: bool = False
    versions: list[ValueVersionItem] = Field(default_factory=list)


class BoundRunRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    run_label: str | None = None
    status: str
    created_at: str | None = None


class BoundRequirementCheckRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    requirement_id: str
    outcome: str
    reason_code: str
    created_at: str | None = None


class ValueComparisonSide(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version_index: int | None = None
    id: str | None = None
    value: Any
    unit: str
    conditions: dict[str, Any] = Field(default_factory=dict)
    source: str
    validation_state: str
    status: str | None = None
    actor: str | None = None
    reason: str | None = None
    created_at: str | None = None
    runs: list[BoundRunRef] = Field(default_factory=list)
    requirement_checks: list[BoundRequirementCheckRef] = Field(default_factory=list)


class ValueComparisonRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value_kind: str
    value_id: str
    version_a: ValueComparisonSide
    version_b: ValueComparisonSide


def get_run_inputs_manifest(
    connection: sqlite3.Connection,
    workspace_id: str,
    simulation_run_id: str,
) -> RunInputsManifestRead:
    run = connection.execute(
        """
        SELECT id, workspace_id, model_version_id, input_payload, created_at
        FROM simulation_runs
        WHERE id = ? AND workspace_id = ?
        """,
        (simulation_run_id, workspace_id),
    ).fetchone()
    if run is None:
        raise ValueHistoryError("runner_simulation_run_not_found", f"Simulation run '{simulation_run_id}' not found.")

    model_version_id = run["model_version_id"]
    if not model_version_id:
        return RunInputsManifestRead(
            simulation_run_id=simulation_run_id,
            workspace_id=workspace_id,
            status="history_not_recorded",
            history_not_recorded=True,
            history_recorded=False,
            reason="Run has no model version or input contract.",
            items=[],
        )

    mv = connection.execute(
        """
        SELECT input_contract_payload, input_contract_sha256
        FROM model_versions
        WHERE id = ? AND workspace_id = ?
        """,
        (model_version_id, workspace_id),
    ).fetchone()
    if mv is None:
        return RunInputsManifestRead(
            simulation_run_id=simulation_run_id,
            workspace_id=workspace_id,
            status="history_not_recorded",
            history_not_recorded=True,
            history_recorded=False,
            reason="Model version not found.",
            items=[],
        )

    try:
        is_canonical = contract_requires_canonical_linked_parameters(
            mv["input_contract_payload"],
            mv["input_contract_sha256"],
        )
    except Exception:
        is_canonical = False

    if not is_canonical:
        return RunInputsManifestRead(
            simulation_run_id=simulation_run_id,
            workspace_id=workspace_id,
            status="history_not_recorded",
            history_not_recorded=True,
            history_recorded=False,
            reason="Stored input contract has no canonical linked parameters (historical or schema v1).",
            items=[],
        )

    input_payload_raw = run["input_payload"]
    if not input_payload_raw:
        return RunInputsManifestRead(
            simulation_run_id=simulation_run_id,
            workspace_id=workspace_id,
            status="recorded",
            history_not_recorded=False,
            history_recorded=True,
            reason=None,
            items=[],
        )

    try:
        payload_dict = (
            json.loads(input_payload_raw)
            if isinstance(input_payload_raw, str)
            else input_payload_raw
        )
    except (TypeError, json.JSONDecodeError):
        payload_dict = {}

    if not isinstance(payload_dict, dict):
        payload_dict = {}

    items: list[RunLinkedParameterItem] = []
    for var_name, var_data in sorted(payload_dict.items()):
        if not isinstance(var_data, dict):
            continue
        param_id = var_data.get("source_parameter_id")
        if not param_id or not isinstance(param_id, str) or not param_id.strip():
            continue
        param_id = param_id.strip()

        stored_value = var_data.get("value")
        stored_unit = str(var_data.get("unit") or "")
        source_parameter_updated_at = var_data.get("source_parameter_updated_at")

        p_row = connection.execute(
            """
            SELECT id, workspace_id, name, value, unit, status, lifecycle_state, supersedes_parameter_id
            FROM parameters
            WHERE id = ? AND workspace_id = ?
            """,
            (param_id, workspace_id),
        ).fetchone()

        if p_row is not None:
            name = str(p_row["name"])
            status = str(p_row["status"])
            lifecycle_state = str(p_row["lifecycle_state"])

            # Compute position in supersedes chain
            position = 0
            curr_supersedes = p_row["supersedes_parameter_id"]
            visited_up = {param_id}
            while curr_supersedes:
                if curr_supersedes in visited_up:
                    break
                visited_up.add(curr_supersedes)
                parent = connection.execute(
                    "SELECT id, supersedes_parameter_id FROM parameters WHERE id = ? AND workspace_id = ?",
                    (curr_supersedes, workspace_id),
                ).fetchone()
                if not parent:
                    break
                position += 1
                curr_supersedes = parent["supersedes_parameter_id"]

            # Compute newer_version_exists
            newer_version_exists = False
            curr_id = param_id
            visited_down = {param_id}
            while True:
                child = connection.execute(
                    "SELECT id, status, lifecycle_state FROM parameters WHERE supersedes_parameter_id = ? AND workspace_id = ?",
                    (curr_id, workspace_id),
                ).fetchone()
                if not child or child["id"] in visited_down:
                    break
                visited_down.add(child["id"])
                if child["lifecycle_state"] == "active" or child["status"] == "accepted":
                    newer_version_exists = True
                    break
                curr_id = child["id"]
        else:
            name = var_name
            status = "missing"
            lifecycle_state = None
            position = 0
            newer_version_exists = False

        items.append(
            RunLinkedParameterItem(
                variable_name=var_name,
                id=param_id,
                name=name,
                value=stored_value,
                unit=stored_unit,
                status=status,
                lifecycle_state=lifecycle_state,
                position=position,
                newer_version_exists=newer_version_exists,
                source_parameter_updated_at=source_parameter_updated_at,
            )
        )

    return RunInputsManifestRead(
        simulation_run_id=simulation_run_id,
        workspace_id=workspace_id,
        status="recorded",
        history_not_recorded=False,
        history_recorded=True,
        reason=None,
        items=items,
    )


def get_parameter_version_chain(
    connection: sqlite3.Connection,
    workspace_id: str,
    parameter_id: str,
) -> ValueVersionChainRead:
    start_param = connection.execute(
        """
        SELECT id, workspace_id, name, value, unit, status, lifecycle_state,
               supersedes_parameter_id, origin, notes, source_ref, created_at, updated_at
        FROM parameters
        WHERE id = ? AND workspace_id = ?
        """,
        (parameter_id, workspace_id),
    ).fetchone()
    if start_param is None:
        raise ValueHistoryError("parameter_not_found", f"Parameter '{parameter_id}' not found.")

    # Trace backward to root
    curr = start_param
    ancestors: list[sqlite3.Row] = []
    visited = {start_param["id"]}
    while curr["supersedes_parameter_id"]:
        parent_id = curr["supersedes_parameter_id"]
        if parent_id in visited:
            break
        visited.add(parent_id)
        parent = connection.execute(
            """
            SELECT id, workspace_id, name, value, unit, status, lifecycle_state,
                   supersedes_parameter_id, origin, notes, source_ref, created_at, updated_at
            FROM parameters
            WHERE id = ? AND workspace_id = ?
            """,
            (parent_id, workspace_id),
        ).fetchone()
        if not parent:
            break
        ancestors.append(parent)
        curr = parent

    ancestors.reverse()

    # Trace forward from start_param
    curr = start_param
    successors: list[sqlite3.Row] = []
    while True:
        child = connection.execute(
            """
            SELECT id, workspace_id, name, value, unit, status, lifecycle_state,
                   supersedes_parameter_id, origin, notes, source_ref, created_at, updated_at
            FROM parameters
            WHERE supersedes_parameter_id = ? AND workspace_id = ?
            """,
            (curr["id"], workspace_id),
        ).fetchone()
        if not child or child["id"] in visited:
            break
        visited.add(child["id"])
        successors.append(child)
        curr = child

    full_chain = ancestors + [start_param] + successors

    versions: list[ValueVersionItem] = []
    for idx, p in enumerate(full_chain):
        status = str(p["status"])
        val_state = status
        action = "create" if idx == 0 else "supersede"
        source = str(p["source_ref"] or f"parameter:{p['id']}")
        actor = str(p["origin"] or "user")
        reason = str(p["notes"]) if p["notes"] else None
        val = p["value"]
        unit = str(p["unit"] or "")

        versions.append(
            ValueVersionItem(
                version_index=idx,
                id=str(p["id"]),
                value=val,
                unit=unit,
                conditions={},
                status=status,
                validation_state=val_state,
                action=action,
                source=source,
                actor=actor,
                reason=reason,
                created_at=str(p["created_at"]),
                origin=str(p["origin"] or "user"),
            )
        )

    return ValueVersionChainRead(
        value_kind="parameter",
        value_id=parameter_id,
        workspace_id=workspace_id,
        intermediate_versions_unknown=False,
        versions=versions,
    )


def get_sim_result_value_version_chain(
    connection: sqlite3.Connection,
    workspace_id: str,
    value_id: str,
) -> ValueVersionChainRead:
    v_row = connection.execute(
        """
        SELECT v.*, b.workspace_id, b.run_id
        FROM simulation_result_values v
        JOIN simulation_result_batches b ON b.id = v.batch_id
        WHERE v.id = ? AND b.workspace_id = ?
        """,
        (value_id, workspace_id),
    ).fetchone()
    if v_row is None:
        raise ValueHistoryError("result_value_not_found", f"Simulation result value '{value_id}' not found.")

    version_rows = connection.execute(
        """
        SELECT * FROM simulation_result_value_versions
        WHERE value_id = ?
        ORDER BY version_index ASC
        """,
        (value_id,),
    ).fetchall()

    if not version_rows:
        try:
            conds = json.loads(str(v_row["conditions_json"]))
        except (TypeError, json.JSONDecodeError):
            conds = {}
        versions = [
            ValueVersionItem(
                version_index=0,
                id=str(v_row["id"]),
                value=str(v_row["value_text"]),
                unit=str(v_row["unit"]),
                conditions=conds if isinstance(conds, dict) else {},
                status=str(v_row["status"]),
                validation_state=str(v_row["status"]),
                action="ingest",
                source=f"simulation_run:{v_row['run_id']}",
                actor=None,
                reason=None,
                created_at=str(v_row["created_at"]),
                origin="recorded",
            )
        ]
        return ValueVersionChainRead(
            value_kind="simulation_result_value",
            value_id=value_id,
            workspace_id=workspace_id,
            intermediate_versions_unknown=False,
            versions=versions,
        )

    has_backfilled_resolution = any(
        row["origin"] == "backfilled" and row["version_index"] > 0
        for row in version_rows
    )

    versions = []
    for row in version_rows:
        try:
            conds = json.loads(str(row["conditions_json"]))
        except (TypeError, json.JSONDecodeError):
            conds = {}

        st = str(row["status"])
        versions.append(
            ValueVersionItem(
                version_index=int(row["version_index"]),
                id=str(row["id"]),
                value=str(row["value_text"]),
                unit=str(row["unit"]),
                conditions=conds if isinstance(conds, dict) else {},
                status=st,
                validation_state=st,
                action=str(row["action"]) if row["action"] is not None else None,
                source=f"simulation_run:{v_row['run_id']}",
                actor=str(row["actor"]) if row["actor"] is not None else None,
                reason=str(row["reason"]) if row["reason"] is not None else None,
                created_at=str(row["created_at"]),
                origin=str(row["origin"]),
            )
        )

    return ValueVersionChainRead(
        value_kind="simulation_result_value",
        value_id=value_id,
        workspace_id=workspace_id,
        intermediate_versions_unknown=bool(has_backfilled_resolution),
        versions=versions,
    )


def _find_runs_using_parameter(
    connection: sqlite3.Connection,
    workspace_id: str,
    parameter_id: str,
) -> list[BoundRunRef]:
    runs = connection.execute(
        """
        SELECT id, run_label, status, input_payload, created_at
        FROM simulation_runs
        WHERE workspace_id = ?
        ORDER BY created_at DESC
        """,
        (workspace_id,),
    ).fetchall()

    bound_runs: list[BoundRunRef] = []
    for r in runs:
        payload_raw = r["input_payload"]
        if not payload_raw:
            continue
        try:
            items = linked_parameter_items(payload_raw)
        except Exception:
            continue
        if any(str(item.get("source_parameter_id")) == parameter_id for item in items):
            bound_runs.append(
                BoundRunRef(
                    id=str(r["id"]),
                    run_label=str(r["run_label"]) if r["run_label"] else None,
                    status=str(r["status"]),
                    created_at=str(r["created_at"]) if r["created_at"] else None,
                )
            )
    return bound_runs


def _find_requirement_checks_for_runs(
    connection: sqlite3.Connection,
    workspace_id: str,
    run_ids: list[str],
) -> list[BoundRequirementCheckRef]:
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        f"""
        SELECT id, requirement_id, outcome, reason_code, created_at
        FROM project_knowledge_validation
        WHERE workspace_id = ? AND source_run_id IN ({placeholders})
        ORDER BY created_at DESC
        """,
        [workspace_id, *run_ids],
    ).fetchall()
    return [
        BoundRequirementCheckRef(
            id=str(row["id"]),
            requirement_id=str(row["requirement_id"]),
            outcome=str(row["outcome"]),
            reason_code=str(row["reason_code"]),
            created_at=str(row["created_at"]) if row["created_at"] else None,
        )
        for row in rows
    ]


def _find_requirement_checks_for_result_version(
    connection: sqlite3.Connection,
    workspace_id: str,
    scalar_id: str | None,
    run_id: str,
    version_row: sqlite3.Row,
) -> list[BoundRequirementCheckRef]:
    query = """
        SELECT id, requirement_id, outcome, reason_code, observed_json, created_at
        FROM project_knowledge_validation
        WHERE workspace_id = ? AND (source_run_id = ? OR source_scalar_id = ?)
        ORDER BY created_at DESC
    """
    rows = connection.execute(query, (workspace_id, run_id, scalar_id or "")).fetchall()
    matched: list[BoundRequirementCheckRef] = []
    val_text = str(version_row["value_text"])
    unit_text = str(version_row["unit"])
    for row in rows:
        obs_raw = row["observed_json"]
        if not obs_raw:
            continue
        try:
            obs = json.loads(str(obs_raw))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(obs, dict):
            obs_val = str(obs.get("value"))
            obs_unit = str(obs.get("unit") or "")
            if obs_val == val_text and (not unit_text or obs_unit == unit_text):
                matched.append(
                    BoundRequirementCheckRef(
                        id=str(row["id"]),
                        requirement_id=str(row["requirement_id"]),
                        outcome=str(row["outcome"]),
                        reason_code=str(row["reason_code"]),
                        created_at=str(row["created_at"]) if row["created_at"] else None,
                    )
                )
    return matched


def compare_parameters(
    connection: sqlite3.Connection,
    workspace_id: str,
    param_a_id: str,
    param_b_id: str,
) -> ValueComparisonRead:
    row_a = connection.execute(
        "SELECT * FROM parameters WHERE id = ? AND workspace_id = ?",
        (param_a_id, workspace_id),
    ).fetchone()
    if row_a is None:
        raise ValueHistoryError("parameter_not_found", f"Parameter '{param_a_id}' not found.")

    row_b = connection.execute(
        "SELECT * FROM parameters WHERE id = ? AND workspace_id = ?",
        (param_b_id, workspace_id),
    ).fetchone()
    if row_b is None:
        raise ValueHistoryError("parameter_not_found", f"Parameter '{param_b_id}' not found.")

    def find_root(start_row: sqlite3.Row) -> tuple[sqlite3.Row, int]:
        curr = start_row
        pos = 0
        visited = {curr["id"]}
        while curr["supersedes_parameter_id"]:
            parent = connection.execute(
                "SELECT * FROM parameters WHERE id = ? AND workspace_id = ?",
                (curr["supersedes_parameter_id"], workspace_id),
            ).fetchone()
            if not parent or parent["id"] in visited:
                break
            visited.add(parent["id"])
            curr = parent
            pos += 1
        return curr, pos

    root_a, pos_a = find_root(row_a)
    root_b, pos_b = find_root(row_b)

    if root_a["id"] != root_b["id"]:
        raise ValueHistoryError("not_same_value", "Parameters do not belong to the same value version chain.")

    runs_a = _find_runs_using_parameter(connection, workspace_id, param_a_id)
    checks_a = _find_requirement_checks_for_runs(connection, workspace_id, [r.id for r in runs_a])

    runs_b = _find_runs_using_parameter(connection, workspace_id, param_b_id)
    checks_b = _find_requirement_checks_for_runs(connection, workspace_id, [r.id for r in runs_b])

    side_a = ValueComparisonSide(
        version_index=pos_a,
        id=str(row_a["id"]),
        value=row_a["value"],
        unit=str(row_a["unit"] or ""),
        conditions={},
        source=str(row_a["source_ref"] or f"parameter:{row_a['id']}"),
        validation_state=str(row_a["status"]),
        status=str(row_a["status"]),
        actor=str(row_a["origin"] or "user"),
        reason=str(row_a["notes"]) if row_a["notes"] else None,
        created_at=str(row_a["created_at"]),
        runs=runs_a,
        requirement_checks=checks_a,
    )

    side_b = ValueComparisonSide(
        version_index=pos_b,
        id=str(row_b["id"]),
        value=row_b["value"],
        unit=str(row_b["unit"] or ""),
        conditions={},
        source=str(row_b["source_ref"] or f"parameter:{row_b['id']}"),
        validation_state=str(row_b["status"]),
        status=str(row_b["status"]),
        actor=str(row_b["origin"] or "user"),
        reason=str(row_b["notes"]) if row_b["notes"] else None,
        created_at=str(row_b["created_at"]),
        runs=runs_b,
        requirement_checks=checks_b,
    )

    return ValueComparisonRead(
        value_kind="parameter",
        value_id=str(root_a["id"]),
        version_a=side_a,
        version_b=side_b,
    )


def compare_sim_result_values(
    connection: sqlite3.Connection,
    workspace_id: str,
    value_id: str,
    version_a: str | int,
    version_b: str | int,
    value_b_id: str | None = None,
) -> ValueComparisonRead:
    if value_b_id is not None and value_b_id != value_id:
        raise ValueHistoryError("not_same_value", "Cannot compare versions from different simulation result values.")

    v_row = connection.execute(
        """
        SELECT v.*, b.workspace_id, b.run_id
        FROM simulation_result_values v
        JOIN simulation_result_batches b ON b.id = v.batch_id
        WHERE v.id = ? AND b.workspace_id = ?
        """,
        (value_id, workspace_id),
    ).fetchone()
    if v_row is None:
        raise ValueHistoryError("result_value_not_found", f"Simulation result value '{value_id}' not found.")

    def load_version_row(v_target: str | int) -> sqlite3.Row:
        row = None
        try:
            v_idx = int(v_target)
            row = connection.execute(
                "SELECT * FROM simulation_result_value_versions WHERE value_id = ? AND version_index = ?",
                (value_id, v_idx),
            ).fetchone()
        except (ValueError, TypeError):
            pass
        if row is None:
            row = connection.execute(
                "SELECT * FROM simulation_result_value_versions WHERE value_id = ? AND id = ?",
                (value_id, str(v_target)),
            ).fetchone()
        if row is None:
            diff_val = connection.execute(
                "SELECT value_id FROM simulation_result_value_versions WHERE id = ?",
                (str(v_target),),
            ).fetchone()
            if diff_val and str(diff_val["value_id"]) != value_id:
                raise ValueHistoryError("not_same_value", "Version belongs to a different simulation result value.")
            raise ValueHistoryError("result_value_version_not_found", f"Result value version '{v_target}' not found.")
        return row

    row_a = load_version_row(version_a)
    row_b = load_version_row(version_b)

    origin_run = connection.execute(
        "SELECT id, run_label, status, created_at FROM simulation_runs WHERE id = ? AND workspace_id = ?",
        (v_row["run_id"], workspace_id),
    ).fetchone()
    origin_ref = (
        [
            BoundRunRef(
                id=str(origin_run["id"]),
                run_label=str(origin_run["run_label"]) if origin_run["run_label"] else None,
                status=str(origin_run["status"]),
                created_at=str(origin_run["created_at"]) if origin_run["created_at"] else None,
            )
        ]
        if origin_run is not None
        else []
    )

    def find_downstream_runs_for_version(v_row_target: sqlite3.Row) -> list[BoundRunRef]:
        v_idx = int(v_row_target["version_index"])
        runs: list[BoundRunRef] = []
        if v_idx == 0 and origin_ref:
            runs.extend(origin_ref)

        all_runs = connection.execute(
            "SELECT id, run_label, status, input_payload, created_at FROM simulation_runs WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        for r in all_runs:
            if str(r["id"]) == str(v_row["run_id"]):
                continue
            payload_raw = r["input_payload"]
            if not payload_raw:
                continue
            try:
                p_dict = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            except Exception:
                continue
            if isinstance(p_dict, dict):
                matched_run = False
                for item in p_dict.values():
                    if isinstance(item, dict) and str(item.get("source_result_value_id")) == value_id:
                        matched_run = True
                        break
                if matched_run:
                    runs.append(
                        BoundRunRef(
                            id=str(r["id"]),
                            run_label=str(r["run_label"]) if r["run_label"] else None,
                            status=str(r["status"]),
                            created_at=str(r["created_at"]) if r["created_at"] else None,
                        )
                    )
        return runs

    runs_a = find_downstream_runs_for_version(row_a)
    checks_a = _find_requirement_checks_for_result_version(
        connection, workspace_id, v_row["admitted_scalar_id"], str(v_row["run_id"]), row_a
    )

    runs_b = find_downstream_runs_for_version(row_b)
    checks_b = _find_requirement_checks_for_result_version(
        connection, workspace_id, v_row["admitted_scalar_id"], str(v_row["run_id"]), row_b
    )

    def row_to_conds(r: sqlite3.Row) -> dict[str, Any]:
        try:
            c = json.loads(str(r["conditions_json"]))
            return c if isinstance(c, dict) else {}
        except Exception:
            return {}

    side_a = ValueComparisonSide(
        version_index=int(row_a["version_index"]),
        id=str(row_a["id"]),
        value=str(row_a["value_text"]),
        unit=str(row_a["unit"]),
        conditions=row_to_conds(row_a),
        source=f"simulation_run:{v_row['run_id']}",
        validation_state=str(row_a["status"]),
        status=str(row_a["status"]),
        actor=str(row_a["actor"]) if row_a["actor"] is not None else None,
        reason=str(row_a["reason"]) if row_a["reason"] is not None else None,
        created_at=str(row_a["created_at"]),
        runs=runs_a,
        requirement_checks=checks_a,
    )

    side_b = ValueComparisonSide(
        version_index=int(row_b["version_index"]),
        id=str(row_b["id"]),
        value=str(row_b["value_text"]),
        unit=str(row_b["unit"]),
        conditions=row_to_conds(row_b),
        source=f"simulation_run:{v_row['run_id']}",
        validation_state=str(row_b["status"]),
        status=str(row_b["status"]),
        actor=str(row_b["actor"]) if row_b["actor"] is not None else None,
        reason=str(row_b["reason"]) if row_b["reason"] is not None else None,
        created_at=str(row_b["created_at"]),
        runs=runs_b,
        requirement_checks=checks_b,
    )

    return ValueComparisonRead(
        value_kind="simulation_result_value",
        value_id=value_id,
        version_a=side_a,
        version_b=side_b,
    )


def compare_value_versions(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    value_kind: str | None = None,
    value_id: str | None = None,
    version_a: str | None = None,
    version_b: str | None = None,
    parameter_a_id: str | None = None,
    parameter_b_id: str | None = None,
) -> ValueComparisonRead:
    if parameter_a_id and parameter_b_id:
        return compare_parameters(connection, workspace_id, parameter_a_id, parameter_b_id)

    if value_kind in {"parameter", "parameters"}:
        p_a = parameter_a_id or version_a
        p_b = parameter_b_id or version_b
        if not p_a or not p_b:
            raise ValueHistoryError("comparison_target_missing", "Two parameter versions must be provided to compare.")
        return compare_parameters(connection, workspace_id, p_a, p_b)

    if value_kind in {"simulation_result_value", "result_value", "simulation-results", "result-values"}:
        if not value_id:
            raise ValueHistoryError("comparison_target_missing", "value_id must be provided for simulation result values.")
        if version_a is None or version_b is None:
            raise ValueHistoryError("comparison_target_missing", "version_a and version_b must be provided.")
        return compare_sim_result_values(connection, workspace_id, value_id, version_a, version_b)

    candidate_a = parameter_a_id or version_a
    candidate_b = parameter_b_id or version_b
    if candidate_a and candidate_b:
        p_a = connection.execute("SELECT id FROM parameters WHERE id = ? AND workspace_id = ?", (candidate_a, workspace_id)).fetchone()
        p_b = connection.execute("SELECT id FROM parameters WHERE id = ? AND workspace_id = ?", (candidate_b, workspace_id)).fetchone()
        if p_a and p_b:
            return compare_parameters(connection, workspace_id, candidate_a, candidate_b)
        if (p_a and not p_b) or (not p_a and p_b):
            raise ValueHistoryError("not_same_value", "Cannot compare a parameter with a different value type.")

    if value_id and version_a is not None and version_b is not None:
        v_res = connection.execute(
            """
            SELECT v.id FROM simulation_result_values v
            JOIN simulation_result_batches b ON b.id = v.batch_id
            WHERE v.id = ? AND b.workspace_id = ?
            """,
            (value_id, workspace_id),
        ).fetchone()
        if v_res:
            return compare_sim_result_values(connection, workspace_id, value_id, version_a, version_b)

    raise ValueHistoryError("comparison_target_missing", "Could not resolve comparison targets.")
