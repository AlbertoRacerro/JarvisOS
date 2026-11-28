"""Deterministic semantic diff engine between Design versions (spec 392)."""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.modules.process_stack import draft


class ParameterDiffItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    symbol: str | None = None
    base_value: str | None = None
    target_value: str | None = None
    base_unit: str | None = None
    target_unit: str | None = None
    numeric_delta: float | None = None
    relative_delta_pct: float | None = None
    base_status: str | None = None
    target_status: str | None = None


class ComponentDiffItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    geometry_candidate_changed: bool = False
    base_candidate_id: str | None = None
    target_candidate_id: str | None = None
    models_added: list[str] = Field(default_factory=list)
    models_removed: list[str] = Field(default_factory=list)
    models_changed: list[str] = Field(default_factory=list)


class UnitParamDiffItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    draft_name: str
    unit_tag: str
    unit_type: str
    param_key: str
    base_value: Any = None
    target_value: Any = None
    numeric_delta: float | None = None


class ResultDiffItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    output_name: str
    quantity: str | None = None
    base_value: str | None = None
    target_value: str | None = None
    base_unit: str | None = None
    target_unit: str | None = None
    numeric_delta: float | None = None
    relative_delta_pct: float | None = None
    base_status: str | None = None
    target_status: str | None = None


class ValidationDiffItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str
    base_outcome: str | None = None
    target_outcome: str | None = None
    base_reason_code: str | None = None
    target_reason_code: str | None = None


class DesignVersionDiff(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    base_version_id: str
    target_version_id: str
    has_changes: bool
    summary: str
    counts: dict[str, int] = Field(default_factory=dict)
    parameters_added: list[ParameterDiffItem] = Field(default_factory=list)
    parameters_removed: list[ParameterDiffItem] = Field(default_factory=list)
    parameters_changed: list[ParameterDiffItem] = Field(default_factory=list)
    components_added: list[ComponentDiffItem] = Field(default_factory=list)
    components_removed: list[ComponentDiffItem] = Field(default_factory=list)
    components_changed: list[ComponentDiffItem] = Field(default_factory=list)
    units_added: list[str] = Field(default_factory=list)
    units_removed: list[str] = Field(default_factory=list)
    unit_params_changed: list[UnitParamDiffItem] = Field(default_factory=list)
    results_added: list[ResultDiffItem] = Field(default_factory=list)
    results_removed: list[ResultDiffItem] = Field(default_factory=list)
    results_changed: list[ResultDiffItem] = Field(default_factory=list)
    validations_added: list[ValidationDiffItem] = Field(default_factory=list)
    validations_removed: list[ValidationDiffItem] = Field(default_factory=list)
    validations_changed: list[ValidationDiffItem] = Field(default_factory=list)


def _parse_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(str(val).strip())
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _compute_numeric_deltas(
    base_val: Any,
    target_val: Any,
    base_unit: str | None,
    target_unit: str | None,
) -> tuple[float | None, float | None]:
    b_u = (base_unit or "").strip().lower()
    t_u = (target_unit or "").strip().lower()
    b_unspec = b_u in ("", "unspecified")
    t_unspec = t_u in ("", "unspecified")
    if not ((b_unspec and t_unspec) or (b_u == t_u)):
        return None, None

    b_num = _parse_float(base_val)
    t_num = _parse_float(target_val)
    if b_num is None or t_num is None:
        return None, None

    num_delta = t_num - b_num
    rel_pct = ((t_num - b_num) / b_num) * 100.0 if b_num != 0 else None
    return num_delta, rel_pct


def _extract_unit_param_value(param_obj: Any) -> Any:
    if isinstance(param_obj, dict):
        if "value" in param_obj:
            return param_obj["value"]
        if "si" in param_obj:
            return param_obj["si"]
    return param_obj


def _collect_parameters(connection: sqlite3.Connection, workspace_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, name, symbol, value, unit, status, supersedes_parameter_id, created_at, updated_at
        FROM parameters
        WHERE workspace_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (workspace_id,),
    ).fetchall()

    superseded_ids = set()
    for r in rows:
        if r["status"] == "superseded":
            superseded_ids.add(r["id"])
        if r["supersedes_parameter_id"]:
            superseded_ids.add(r["supersedes_parameter_id"])

    latest_params: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["id"] in superseded_ids or r["status"] == "superseded":
            continue
        canonical = r["name"].strip().lower()
        if canonical not in latest_params:
            latest_params[canonical] = dict(r)
    return latest_params


def _collect_components(
    connection: sqlite3.Connection, version_id: str
) -> dict[str, dict[str, Any]]:
    comp_rows = connection.execute(
        """
        SELECT id, name, created_at, updated_at
        FROM design_components
        WHERE version_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (version_id,),
    ).fetchall()

    components: dict[str, dict[str, Any]] = {}
    for r in comp_rows:
        canonical = r["name"].strip().lower()
        if canonical in components:
            continue
        comp_id = r["id"]
        geom_row = connection.execute(
            "SELECT candidate_id FROM design_component_geometry WHERE component_id = ?",
            (comp_id,),
        ).fetchone()
        candidate_id = geom_row["candidate_id"] if geom_row else None

        model_rows = connection.execute(
            """
            SELECT model_version_id, node, category, source_sha256, contract_sha256
            FROM design_component_models
            WHERE component_id = ?
            ORDER BY model_version_id
            """,
            (comp_id,),
        ).fetchall()

        components[canonical] = {
            "id": comp_id,
            "name": r["name"],
            "candidate_id": candidate_id,
            "models": [dict(m) for m in model_rows],
        }
    return components


def _collect_draft_units(workspace_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    units: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        draft_list = draft.list_drafts(workspace_id)
    except Exception:
        return {}

    for d_item in draft_list:
        d_id = d_item.get("draft_id")
        d_name = (d_item.get("name") or "draft").strip()
        d_rev = d_item.get("revision")
        if not d_id or not d_rev:
            continue
        try:
            d_dir = draft.draft_dir(workspace_id, d_id)
            record = draft.load_revision(d_dir, d_rev)
        except Exception:
            continue

        doc = record.get("document", {})
        objects = doc.get("objects", {})
        for obj in objects.values():
            if not isinstance(obj, dict):
                continue
            if obj.get("kind") != "unit":
                continue
            unit_tag = obj.get("tag", "").strip()
            unit_type = obj.get("type", "").strip()
            params = obj.get("params", {})
            canonical_key = (d_name.lower(), unit_tag.lower())
            units[canonical_key] = {
                "draft_name": d_name,
                "unit_tag": unit_tag,
                "unit_type": unit_type,
                "params": params if isinstance(params, dict) else {},
            }
    return units


def _collect_simulation_results(
    connection: sqlite3.Connection, workspace_id: str
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            v.output_name,
            v.quantity,
            v.value_text,
            v.unit,
            v.status,
            v.created_at
        FROM simulation_result_values v
        JOIN simulation_result_batches b ON v.batch_id = b.id
        WHERE b.workspace_id = ?
        ORDER BY v.created_at DESC, v.id DESC
        """,
        (workspace_id,),
    ).fetchall()

    latest_results: dict[str, dict[str, Any]] = {}
    for r in rows:
        canonical = r["output_name"].strip().lower()
        if canonical not in latest_results:
            latest_results[canonical] = dict(r)
    return latest_results


def _collect_validations(
    connection: sqlite3.Connection, workspace_id: str
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            r.statement,
            v.outcome,
            v.reason_code,
            v.created_at
        FROM project_knowledge_validation v
        JOIN requirements r ON v.requirement_id = r.id
        WHERE v.workspace_id = ?
        ORDER BY v.created_at DESC, v.id DESC
        """,
        (workspace_id,),
    ).fetchall()

    latest_validations: dict[str, dict[str, Any]] = {}
    for r in rows:
        canonical = r["statement"].strip().lower()
        if canonical not in latest_validations:
            latest_validations[canonical] = dict(r)
    return latest_validations


def diff_design_versions(
    connection: sqlite3.Connection,
    project_id: str,
    base_version_id: str,
    target_version_id: str,
) -> DesignVersionDiff:
    """Compute deterministic semantic diff between two Design versions of a project."""
    # 1. Validate project exists
    project = connection.execute(
        "SELECT id FROM workspaces WHERE id = ? AND status != 'design_version'",
        (project_id,),
    ).fetchone()
    if project is None:
        raise HTTPException(404, detail="Project not found.")

    # 2. Validate both versions belong to project
    base_ver = connection.execute(
        "SELECT id, data_workspace_id FROM design_project_versions WHERE id = ? AND project_id = ?",
        (base_version_id, project_id),
    ).fetchone()
    if base_ver is None:
        raise HTTPException(404, detail="Design version not found in this project.")

    target_ver = connection.execute(
        "SELECT id, data_workspace_id FROM design_project_versions WHERE id = ? AND project_id = ?",
        (target_version_id, project_id),
    ).fetchone()
    if target_ver is None:
        raise HTTPException(404, detail="Design version not found in this project.")

    zero_counts = {
        "parameters_added": 0,
        "parameters_removed": 0,
        "parameters_changed": 0,
        "components_added": 0,
        "components_removed": 0,
        "components_changed": 0,
        "units_added": 0,
        "units_removed": 0,
        "unit_params_changed": 0,
        "results_added": 0,
        "results_removed": 0,
        "results_changed": 0,
        "validations_added": 0,
        "validations_removed": 0,
        "validations_changed": 0,
    }

    if base_version_id == target_version_id:
        return DesignVersionDiff(
            project_id=project_id,
            base_version_id=base_version_id,
            target_version_id=target_version_id,
            has_changes=False,
            summary="No differences between versions.",
            counts=zero_counts,
        )

    base_ws_id = str(base_ver["data_workspace_id"])
    target_ws_id = str(target_ver["data_workspace_id"])

    # ---------------- 1. Parameters ----------------
    base_params = _collect_parameters(connection, base_ws_id)
    target_params = _collect_parameters(connection, target_ws_id)
    all_param_keys = sorted(set(base_params.keys()) | set(target_params.keys()))

    parameters_added: list[ParameterDiffItem] = []
    parameters_removed: list[ParameterDiffItem] = []
    parameters_changed: list[ParameterDiffItem] = []

    for k in all_param_keys:
        in_base = k in base_params
        in_target = k in target_params
        if in_target and not in_base:
            t = target_params[k]
            parameters_added.append(
                ParameterDiffItem(
                    name=t["name"],
                    symbol=t["symbol"],
                    base_value=None,
                    target_value=t["value"],
                    base_unit=None,
                    target_unit=t["unit"],
                    numeric_delta=None,
                    relative_delta_pct=None,
                    base_status=None,
                    target_status=t["status"],
                )
            )
        elif in_base and not in_target:
            b = base_params[k]
            parameters_removed.append(
                ParameterDiffItem(
                    name=b["name"],
                    symbol=b["symbol"],
                    base_value=b["value"],
                    target_value=None,
                    base_unit=b["unit"],
                    target_unit=None,
                    numeric_delta=None,
                    relative_delta_pct=None,
                    base_status=b["status"],
                    target_status=None,
                )
            )
        else:
            b = base_params[k]
            t = target_params[k]
            changed = (
                b["value"] != t["value"]
                or b["unit"] != t["unit"]
                or b["status"] != t["status"]
                or b["symbol"] != t["symbol"]
            )
            if changed:
                num_delta, rel_pct = _compute_numeric_deltas(
                    b["value"], t["value"], b["unit"], t["unit"]
                )
                parameters_changed.append(
                    ParameterDiffItem(
                        name=t["name"],
                        symbol=t["symbol"] or b["symbol"],
                        base_value=b["value"],
                        target_value=t["value"],
                        base_unit=b["unit"],
                        target_unit=t["unit"],
                        numeric_delta=num_delta,
                        relative_delta_pct=rel_pct,
                        base_status=b["status"],
                        target_status=t["status"],
                    )
                )

    parameters_added.sort(key=lambda x: x.name.strip().lower())
    parameters_removed.sort(key=lambda x: x.name.strip().lower())
    parameters_changed.sort(key=lambda x: x.name.strip().lower())

    # ---------------- 2. Components ----------------
    base_comps = _collect_components(connection, base_version_id)
    target_comps = _collect_components(connection, target_version_id)
    all_comp_keys = sorted(set(base_comps.keys()) | set(target_comps.keys()))

    components_added: list[ComponentDiffItem] = []
    components_removed: list[ComponentDiffItem] = []
    components_changed: list[ComponentDiffItem] = []

    for k in all_comp_keys:
        in_base = k in base_comps
        in_target = k in target_comps
        if in_target and not in_base:
            t = target_comps[k]
            components_added.append(
                ComponentDiffItem(
                    name=t["name"],
                    geometry_candidate_changed=False,
                    base_candidate_id=None,
                    target_candidate_id=t["candidate_id"],
                    models_added=sorted(m["model_version_id"] for m in t["models"]),
                    models_removed=[],
                    models_changed=[],
                )
            )
        elif in_base and not in_target:
            b = base_comps[k]
            components_removed.append(
                ComponentDiffItem(
                    name=b["name"],
                    geometry_candidate_changed=False,
                    base_candidate_id=b["candidate_id"],
                    target_candidate_id=None,
                    models_added=[],
                    models_removed=sorted(m["model_version_id"] for m in b["models"]),
                    models_changed=[],
                )
            )
        else:
            b = base_comps[k]
            t = target_comps[k]
            geom_changed = b["candidate_id"] != t["candidate_id"]

            base_by_node = {m["node"]: m["model_version_id"] for m in b["models"] if m.get("node")}
            target_by_node = {m["node"]: m["model_version_id"] for m in t["models"] if m.get("node")}

            models_added = []
            models_removed = []
            models_changed = []

            for node in sorted(set(base_by_node) & set(target_by_node)):
                if base_by_node[node] != target_by_node[node]:
                    models_changed.append(target_by_node[node])

            for node in sorted(set(target_by_node) - set(base_by_node)):
                models_added.append(target_by_node[node])

            for node in sorted(set(base_by_node) - set(target_by_node)):
                models_removed.append(base_by_node[node])

            base_unkeyed = {m["model_version_id"] for m in b["models"] if not m.get("node")}
            target_unkeyed = {m["model_version_id"] for m in t["models"] if not m.get("node")}
            models_added.extend(sorted(target_unkeyed - base_unkeyed))
            models_removed.extend(sorted(base_unkeyed - target_unkeyed))

            if geom_changed or models_added or models_removed or models_changed:
                components_changed.append(
                    ComponentDiffItem(
                        name=t["name"],
                        geometry_candidate_changed=geom_changed,
                        base_candidate_id=b["candidate_id"],
                        target_candidate_id=t["candidate_id"],
                        models_added=sorted(models_added),
                        models_removed=sorted(models_removed),
                        models_changed=sorted(models_changed),
                    )
                )

    components_added.sort(key=lambda x: x.name.strip().lower())
    components_removed.sort(key=lambda x: x.name.strip().lower())
    components_changed.sort(key=lambda x: x.name.strip().lower())

    # ---------------- 3. Process Draft Units ----------------
    base_units = _collect_draft_units(base_ws_id)
    target_units = _collect_draft_units(target_ws_id)
    all_unit_keys = sorted(set(base_units.keys()) | set(target_units.keys()))

    units_added: list[str] = []
    units_removed: list[str] = []
    unit_params_changed: list[UnitParamDiffItem] = []

    for u_key in all_unit_keys:
        in_base = u_key in base_units
        in_target = u_key in target_units
        if in_target and not in_base:
            t = target_units[u_key]
            units_added.append(f"{t['draft_name']}:{t['unit_tag']}" if t["draft_name"] else t["unit_tag"])
        elif in_base and not in_target:
            b = base_units[u_key]
            units_removed.append(f"{b['draft_name']}:{b['unit_tag']}" if b["draft_name"] else b["unit_tag"])
        else:
            b = base_units[u_key]
            t = target_units[u_key]
            all_param_keys = sorted(set(b["params"].keys()) | set(t["params"].keys()))
            for p_key in all_param_keys:
                has_b = p_key in b["params"]
                has_t = p_key in t["params"]
                b_raw = b["params"].get(p_key) if has_b else None
                t_raw = t["params"].get(p_key) if has_t else None
                b_val = _extract_unit_param_value(b_raw) if has_b else None
                t_val = _extract_unit_param_value(t_raw) if has_t else None

                if not has_b or not has_t or b_val != t_val:
                    b_num = _parse_float(b_val)
                    t_num = _parse_float(t_val)
                    num_delta = (t_num - b_num) if (b_num is not None and t_num is not None) else None
                    unit_params_changed.append(
                        UnitParamDiffItem(
                            draft_name=t["draft_name"],
                            unit_tag=t["unit_tag"],
                            unit_type=t["unit_type"] or b["unit_type"],
                            param_key=p_key,
                            base_value=b_val,
                            target_value=t_val,
                            numeric_delta=num_delta,
                        )
                    )

    units_added.sort(key=str.lower)
    units_removed.sort(key=str.lower)
    unit_params_changed.sort(key=lambda x: (x.draft_name.lower(), x.unit_tag.lower(), x.param_key.lower()))

    # ---------------- 4. Simulation Results / KPIs ----------------
    base_results = _collect_simulation_results(connection, base_ws_id)
    target_results = _collect_simulation_results(connection, target_ws_id)
    all_result_keys = sorted(set(base_results.keys()) | set(target_results.keys()))

    results_added: list[ResultDiffItem] = []
    results_removed: list[ResultDiffItem] = []
    results_changed: list[ResultDiffItem] = []

    for k in all_result_keys:
        in_base = k in base_results
        in_target = k in target_results
        if in_target and not in_base:
            t = target_results[k]
            results_added.append(
                ResultDiffItem(
                    output_name=t["output_name"],
                    quantity=t["quantity"],
                    base_value=None,
                    target_value=t["value_text"],
                    base_unit=None,
                    target_unit=t["unit"],
                    numeric_delta=None,
                    relative_delta_pct=None,
                    base_status=None,
                    target_status=t["status"],
                )
            )
        elif in_base and not in_target:
            b = base_results[k]
            results_removed.append(
                ResultDiffItem(
                    output_name=b["output_name"],
                    quantity=b["quantity"],
                    base_value=b["value_text"],
                    target_value=None,
                    base_unit=b["unit"],
                    target_unit=None,
                    numeric_delta=None,
                    relative_delta_pct=None,
                    base_status=b["status"],
                    target_status=None,
                )
            )
        else:
            b = base_results[k]
            t = target_results[k]
            changed = (
                b["value_text"] != t["value_text"]
                or b["unit"] != t["unit"]
                or b["status"] != t["status"]
                or b["quantity"] != t["quantity"]
            )
            if changed:
                num_delta, rel_pct = _compute_numeric_deltas(
                    b["value_text"], t["value_text"], b["unit"], t["unit"]
                )
                results_changed.append(
                    ResultDiffItem(
                        output_name=t["output_name"],
                        quantity=t["quantity"] or b["quantity"],
                        base_value=b["value_text"],
                        target_value=t["value_text"],
                        base_unit=b["unit"],
                        target_unit=t["unit"],
                        numeric_delta=num_delta,
                        relative_delta_pct=rel_pct,
                        base_status=b["status"],
                        target_status=t["status"],
                    )
                )

    results_added.sort(key=lambda x: x.output_name.strip().lower())
    results_removed.sort(key=lambda x: x.output_name.strip().lower())
    results_changed.sort(key=lambda x: x.output_name.strip().lower())

    # ---------------- 5. Requirement Validations ----------------
    base_validations = _collect_validations(connection, base_ws_id)
    target_validations = _collect_validations(connection, target_ws_id)
    all_validation_keys = sorted(set(base_validations.keys()) | set(target_validations.keys()))

    validations_added: list[ValidationDiffItem] = []
    validations_removed: list[ValidationDiffItem] = []
    validations_changed: list[ValidationDiffItem] = []

    for k in all_validation_keys:
        in_base = k in base_validations
        in_target = k in target_validations
        if in_target and not in_base:
            t = target_validations[k]
            validations_added.append(
                ValidationDiffItem(
                    statement=t["statement"],
                    base_outcome=None,
                    target_outcome=t["outcome"],
                    base_reason_code=None,
                    target_reason_code=t["reason_code"],
                )
            )
        elif in_base and not in_target:
            b = base_validations[k]
            validations_removed.append(
                ValidationDiffItem(
                    statement=b["statement"],
                    base_outcome=b["outcome"],
                    target_outcome=None,
                    base_reason_code=b["reason_code"],
                    target_reason_code=None,
                )
            )
        else:
            b = base_validations[k]
            t = target_validations[k]
            changed = b["outcome"] != t["outcome"] or b["reason_code"] != t["reason_code"]
            if changed:
                validations_changed.append(
                    ValidationDiffItem(
                        statement=t["statement"],
                        base_outcome=b["outcome"],
                        target_outcome=t["outcome"],
                        base_reason_code=b["reason_code"],
                        target_reason_code=t["reason_code"],
                    )
                )

    validations_added.sort(key=lambda x: x.statement.strip().lower())
    validations_removed.sort(key=lambda x: x.statement.strip().lower())
    validations_changed.sort(key=lambda x: x.statement.strip().lower())

    counts = {
        "parameters_added": len(parameters_added),
        "parameters_removed": len(parameters_removed),
        "parameters_changed": len(parameters_changed),
        "components_added": len(components_added),
        "components_removed": len(components_removed),
        "components_changed": len(components_changed),
        "units_added": len(units_added),
        "units_removed": len(units_removed),
        "unit_params_changed": len(unit_params_changed),
        "results_added": len(results_added),
        "results_removed": len(results_removed),
        "results_changed": len(results_changed),
        "validations_added": len(validations_added),
        "validations_removed": len(validations_removed),
        "validations_changed": len(validations_changed),
    }

    total_changes = sum(counts.values())
    has_changes = total_changes > 0

    if not has_changes:
        summary = "No differences between versions."
    else:
        parts: list[str] = []
        if counts["parameters_added"] > 0:
            n = counts["parameters_added"]
            parts.append(f"{n} parameter{'s' if n != 1 else ''} added")
        if counts["parameters_removed"] > 0:
            n = counts["parameters_removed"]
            parts.append(f"{n} parameter{'s' if n != 1 else ''} removed")
        if counts["parameters_changed"] > 0:
            n = counts["parameters_changed"]
            parts.append(f"{n} parameter{'s' if n != 1 else ''} changed")

        if counts["components_added"] > 0:
            n = counts["components_added"]
            parts.append(f"{n} component{'s' if n != 1 else ''} added")
        if counts["components_removed"] > 0:
            n = counts["components_removed"]
            parts.append(f"{n} component{'s' if n != 1 else ''} removed")
        if counts["components_changed"] > 0:
            n = counts["components_changed"]
            parts.append(f"{n} component{'s' if n != 1 else ''} changed")

        if counts["units_added"] > 0:
            n = counts["units_added"]
            parts.append(f"{n} unit{'s' if n != 1 else ''} added")
        if counts["units_removed"] > 0:
            n = counts["units_removed"]
            parts.append(f"{n} unit{'s' if n != 1 else ''} removed")
        if counts["unit_params_changed"] > 0:
            n = counts["unit_params_changed"]
            parts.append(f"{n} unit param{'s' if n != 1 else ''} changed")

        if counts["results_added"] > 0:
            n = counts["results_added"]
            parts.append(f"{n} KPI{'s' if n != 1 else ''} added")
        if counts["results_removed"] > 0:
            n = counts["results_removed"]
            parts.append(f"{n} KPI{'s' if n != 1 else ''} removed")
        if counts["results_changed"] > 0:
            n = counts["results_changed"]
            parts.append(f"{n} KPI{'s' if n != 1 else ''} changed")

        if counts["validations_added"] > 0:
            n = counts["validations_added"]
            parts.append(f"{n} validation{'s' if n != 1 else ''} added")
        if counts["validations_removed"] > 0:
            n = counts["validations_removed"]
            parts.append(f"{n} validation{'s' if n != 1 else ''} removed")
        if counts["validations_changed"] > 0:
            n = counts["validations_changed"]
            parts.append(f"{n} validation{'s' if n != 1 else ''} changed")

        summary = ", ".join(parts) if parts else "No differences between versions."

    return DesignVersionDiff(
        project_id=project_id,
        base_version_id=base_version_id,
        target_version_id=target_version_id,
        has_changes=has_changes,
        summary=summary,
        counts=counts,
        parameters_added=parameters_added,
        parameters_removed=parameters_removed,
        parameters_changed=parameters_changed,
        components_added=components_added,
        components_removed=components_removed,
        components_changed=components_changed,
        units_added=units_added,
        units_removed=units_removed,
        unit_params_changed=unit_params_changed,
        results_added=results_added,
        results_removed=results_removed,
        results_changed=results_changed,
        validations_added=validations_added,
        validations_removed=validations_removed,
        validations_changed=validations_changed,
    )
