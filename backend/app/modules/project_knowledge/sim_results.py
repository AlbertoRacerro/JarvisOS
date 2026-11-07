from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Literal, cast
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.events.service import utc_now
from app.modules.flowsheet.freshness import (
    FreshnessError,
    PreparedFreshnessInvalidation,
    persist_freshness_invalidation,
    prepare_freshness_invalidation,
)
from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import parse_unit_token
from app.modules.project_knowledge.models import (
    SimulationResultBatchRead,
    SimulationResultCheckRead,
    SimulationResultNotificationRead,
    SimulationResultNotificationTarget,
    SimulationResultResolutionRead,
    SimulationResultResolveRequest,
    SimulationResultValueRead,
)
from app.modules.project_knowledge.service import (
    SCALAR_EXTRACTOR_ID,
    SCALAR_EXTRACTOR_VERSION,
    ProjectKnowledgeError,
    _canonical_json,
    _extract_scalar_from_run_payload,
    _require_workspace,
)

CHECK_NAMES = (
    "source_presence_and_digest",
    "quantity_and_unit_consistency",
    "plausible_range",
    "duplicate",
    "contradiction",
)


def _target_href(workspace_id: str, run_id: str, value_id: str) -> str:
    return f"/memory/project-basis?workspaceId={workspace_id}&runId={run_id}&valueId={value_id}"


def _extract_conditions(output_item: dict[str, object]) -> dict[str, object]:
    raw_conditions = output_item.get("conditions")
    if isinstance(raw_conditions, dict) and raw_conditions:
        cleaned: dict[str, object] = {}
        for key, val in sorted(raw_conditions.items()):
            if isinstance(key, str) and key.strip() and isinstance(val, str | int | float | bool):
                if isinstance(val, float) and not isfinite(val):
                    continue
                cleaned[key.strip()] = val
        if cleaned:
            return cleaned
    return {}


def _extract_bounds(
    connection: sqlite3.Connection,
    workspace_id: str,
    output_name: str,
    quantity: str,
    unit: str,
    output_item: dict[str, object],
) -> tuple[Decimal, Decimal] | None:
    for min_key, max_key in (("min", "max"), ("value_min", "value_max")):
        if min_key in output_item and max_key in output_item:
            raw_min = output_item.get(min_key)
            raw_max = output_item.get(max_key)
            if raw_min is not None and raw_max is not None and not isinstance(raw_min, bool) and not isinstance(raw_max, bool):
                try:
                    d_min = Decimal(str(raw_min))
                    d_max = Decimal(str(raw_max))
                    if d_min.is_finite() and d_max.is_finite() and d_min <= d_max:
                        return d_min, d_max
                except InvalidOperation:
                    return None
    raw_bounds = output_item.get("bounds")
    if isinstance(raw_bounds, dict):
        raw_min = raw_bounds.get("min")
        raw_max = raw_bounds.get("max")
        if raw_min is not None and raw_max is not None and not isinstance(raw_min, bool) and not isinstance(raw_max, bool):
            try:
                d_min = Decimal(str(raw_min))
                d_max = Decimal(str(raw_max))
                if d_min.is_finite() and d_max.is_finite() and d_min <= d_max:
                    return d_min, d_max
            except InvalidOperation:
                return None

    param_row = connection.execute(
        """
        SELECT value_min, value_max, unit
        FROM parameters
        WHERE workspace_id = ?
          AND name IN (?, ?)
          AND status != 'rejected'
          AND lifecycle_state = 'active'
          AND value_min IS NOT NULL
          AND value_max IS NOT NULL
        ORDER BY CASE WHEN status = 'accepted' THEN 0 ELSE 1 END, updated_at DESC, id DESC
        LIMIT 1
        """,
        (workspace_id, output_name, quantity),
    ).fetchone()
    if param_row is not None and str(param_row["unit"]).strip() == unit:
        try:
            d_min = Decimal(str(param_row["value_min"]))
            d_max = Decimal(str(param_row["value_max"]))
            if d_min.is_finite() and d_max.is_finite() and d_min <= d_max:
                return d_min, d_max
        except InvalidOperation:
            return None
    return None


def _evaluate_value_checks(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    batch_id: str,
    value_id: str | None,
    output_name: str,
    source_payload: str,
    source_payload_digest: str,
    value_text: str,
    unit: str,
    quantity: str | None,
    conditions: dict[str, object],
    output_item: dict[str, object],
    peer_items: list[dict[str, object]],
    is_correction: bool = False,
) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []

    # 1. source_presence_and_digest
    actual_digest = hashlib.sha256(source_payload.encode("utf-8")).hexdigest()
    if actual_digest != source_payload_digest:
        checks.append(("source_presence_and_digest", "failed", "source_digest_mismatch"))
    else:
        try:
            decoded_root = json.loads(source_payload)
            outputs_map = decoded_root.get("outputs") if isinstance(decoded_root, dict) and isinstance(decoded_root.get("outputs"), dict) else decoded_root
            extracted_val, extracted_unit = _extract_scalar_from_run_payload(_canonical_json(outputs_map), output_name)
            if not is_correction and (extracted_val != value_text or extracted_unit != unit):
                checks.append(("source_presence_and_digest", "failed", "source_value_mismatch"))
            else:
                checks.append(("source_presence_and_digest", "passed", "source_verified"))
        except ProjectKnowledgeError:
            checks.append(("source_presence_and_digest", "failed", "source_missing"))

    # 2. quantity_and_unit_consistency
    cleaned_quantity = (quantity or "").strip()
    cleaned_unit = unit.strip()
    if not cleaned_quantity:
        checks.append(("quantity_and_unit_consistency", "failed", "missing_quantity_metadata"))
    elif not cleaned_unit:
        checks.append(("quantity_and_unit_consistency", "failed", "missing_unit"))
    else:
        try:
            parse_unit_token(cleaned_unit)
            unit_mismatch = False
            basis_param = connection.execute(
                """
                SELECT unit FROM parameters
                WHERE workspace_id = ? AND name IN (?, ?)
                  AND status = 'accepted' AND lifecycle_state = 'active'
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (workspace_id, output_name, cleaned_quantity),
            ).fetchone()
            if basis_param is not None and str(basis_param["unit"]).strip() != cleaned_unit:
                unit_mismatch = True
            basis_req = connection.execute(
                """
                SELECT criterion_expected_unit FROM requirements
                WHERE workspace_id = ? AND status = 'active'
                  AND basis_kind = 'acceptance_criterion'
                  AND criterion_output_name IN (?, ?)
                  AND criterion_expected_unit IS NOT NULL
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (workspace_id, output_name, cleaned_quantity),
            ).fetchone()
            if basis_req is not None and str(basis_req["criterion_expected_unit"]).strip() != cleaned_unit:
                unit_mismatch = True
            if unit_mismatch:
                checks.append(("quantity_and_unit_consistency", "failed", "incompatible_unit"))
            else:
                checks.append(("quantity_and_unit_consistency", "passed", "unit_consistent"))
        except ProcessKernelError:
            checks.append(("quantity_and_unit_consistency", "failed", "incompatible_unit"))

    # 3. plausible_range
    try:
        dec_val = Decimal(value_text)
    except InvalidOperation:
        dec_val = Decimal("NaN")
    if not dec_val.is_finite():
        checks.append(("plausible_range", "failed", "implausible_non_finite"))
    elif not conditions:
        checks.append(("plausible_range", "failed", "missing_conditions"))
    else:
        bounds = _extract_bounds(connection, workspace_id, output_name, cleaned_quantity or output_name, cleaned_unit, output_item)
        if bounds is None:
            checks.append(("plausible_range", "failed", "missing_plausible_bounds"))
        elif dec_val < bounds[0] or dec_val > bounds[1]:
            checks.append(("plausible_range", "failed", "out_of_plausible_range"))
        else:
            checks.append(("plausible_range", "passed", "within_plausible_range"))

    # 4. duplicate & 5. contradiction
    conditions_canonical = _canonical_json(conditions)
    has_duplicate = False
    has_contradiction = False
    contradiction_reason = "no_contradiction"

    def _same_numeric_or_text(left: object, right: object) -> bool:
        left_s = str(left or "").strip()
        right_s = str(right or "").strip()
        try:
            left_d = Decimal(left_s)
            right_d = Decimal(right_s)
            if left_d.is_finite() and right_d.is_finite():
                return left_d == right_d
        except InvalidOperation:
            pass
        return left_s == right_s

    if cleaned_quantity and conditions:
        for peer in peer_items:
            peer_name = str(peer.get("output_name") or "")
            if not peer_name or peer_name >= output_name:
                continue
            if peer.get("quantity") == cleaned_quantity and peer.get("conditions_json") == conditions_canonical:
                if _same_numeric_or_text(peer.get("value_text"), value_text) and peer.get("unit") == cleaned_unit:
                    has_duplicate = True
                else:
                    has_contradiction = True
                    contradiction_reason = "batch_value_contradiction"

        existing_rows = connection.execute(
            """
            SELECT v.id, v.value_text, v.unit
            FROM simulation_result_values v
            JOIN simulation_result_batches b ON b.id = v.batch_id
            WHERE b.workspace_id = ?
              AND v.quantity = ?
              AND v.conditions_json = ?
              AND (? IS NULL OR v.id != ?)
            """,
            (workspace_id, cleaned_quantity, conditions_canonical, value_id, value_id),
        ).fetchall()
        for row in existing_rows:
            if _same_numeric_or_text(row["value_text"], value_text) and str(row["unit"]) == cleaned_unit:
                has_duplicate = True
            else:
                has_contradiction = True
                contradiction_reason = "existing_value_contradiction"

    # Also check contradiction against accepted Project Basis parameter (when fixed/targeted) or active decision
    basis_params = connection.execute(
        """
        SELECT value, unit, value_status, value_min, value_max FROM parameters
        WHERE workspace_id = ? AND name IN (?, ?)
          AND status = 'accepted' AND lifecycle_state = 'active'
          AND value IS NOT NULL
        """,
        (workspace_id, output_name, cleaned_quantity or output_name),
    ).fetchall()
    for bp in basis_params:
        # When the parameter defines a bounded range (value_min and value_max) and is not a fixed point target
        # without bounds, plausible_range checks the bounds while contradiction checks fixed/accepted point values
        if bp["value_min"] is not None and bp["value_max"] is not None and str(bp["value_status"]) not in {"fixed", "accepted"}:
            continue
        if str(bp["unit"]).strip() == cleaned_unit and not _same_numeric_or_text(bp["value"], value_text):
            has_contradiction = True
            contradiction_reason = "basis_parameter_contradiction"

    basis_decisions = connection.execute(
        """
        SELECT decision_text FROM decisions
        WHERE workspace_id = ? AND status = 'accepted' AND basis_lifecycle_state = 'active'
        """,
        (workspace_id,),
    ).fetchall()
    for bd in basis_decisions:
        text = str(bd["decision_text"] or "")
        for token_name in (output_name, cleaned_quantity):
            if not token_name:
                continue
            prefix = f"{token_name}="
            if prefix in text:
                rhs = text.split(prefix, 1)[1].split()[0].strip(",;.")
                try:
                    rhs_dec = Decimal(rhs)
                    if rhs_dec.is_finite() and not _same_numeric_or_text(rhs_dec, value_text):
                        has_contradiction = True
                        contradiction_reason = "basis_decision_contradiction"
                except InvalidOperation:
                    pass

    if has_duplicate:
        checks.append(("duplicate", "failed", "duplicate_quantity_and_conditions"))
    else:
        checks.append(("duplicate", "passed", "unique_value"))

    if has_contradiction:
        checks.append(("contradiction", "failed", contradiction_reason))
    else:
        checks.append(("contradiction", "passed", "consistent_with_basis"))

    return checks


def _unvalidated_input_value_ids_for_run(
    connection: sqlite3.Connection,
    workspace_id: str,
    input_payload: str | None,
) -> list[str]:
    if not input_payload:
        return []
    try:
        decoded = json.loads(input_payload)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, dict):
        return []
    flagged_ids: set[str] = set()
    for name, item in decoded.items():
        if not isinstance(item, dict):
            continue
        ref_value_id = item.get("source_result_value_id")
        if isinstance(ref_value_id, str) and ref_value_id.strip():
            row = connection.execute(
                """
                SELECT v.id, v.status
                FROM simulation_result_values v
                JOIN simulation_result_batches b ON b.id = v.batch_id
                WHERE v.id = ? AND b.workspace_id = ?
                """,
                (ref_value_id.strip(), workspace_id),
            ).fetchone()
            if row is not None and str(row["status"]) in {"exception", "confirmed"}:
                flagged_ids.add(str(row["id"]))
        param_id = item.get("source_parameter_id")
        if isinstance(param_id, str) and param_id.strip():
            p_row = connection.execute(
                "SELECT name, source_ref FROM parameters WHERE id = ? AND workspace_id = ?",
                (param_id.strip(), workspace_id),
            ).fetchone()
            if p_row is not None:
                p_name = str(p_row["name"])
                v_rows = connection.execute(
                    """
                    SELECT v.id FROM simulation_result_values v
                    JOIN simulation_result_batches b ON b.id = v.batch_id
                    WHERE b.workspace_id = ? AND v.output_name = ? AND v.status IN ('exception', 'confirmed')
                    """,
                    (workspace_id, p_name),
                ).fetchall()
                for vr in v_rows:
                    flagged_ids.add(str(vr["id"]))
        raw_val = item.get("value")
        raw_unit = item.get("unit")
        if (
            not (isinstance(ref_value_id, str) and ref_value_id.strip())
            and not (isinstance(param_id, str) and param_id.strip())
            and raw_val is not None
            and not isinstance(raw_val, bool)
            and isinstance(raw_unit, str)
            and raw_unit.strip()
        ):
            input_dec: Decimal | None = None
            try:
                dec = Decimal(str(raw_val).strip())
                if dec.is_finite():
                    input_dec = dec
            except InvalidOperation:
                input_dec = None
            v_by_name = connection.execute(
                """
                SELECT v.id, v.value_text, v.status FROM simulation_result_values v
                JOIN simulation_result_batches b ON b.id = v.batch_id
                WHERE b.workspace_id = ? AND (v.output_name = ? OR v.quantity = ?)
                  AND v.unit = ?
                """,
                (workspace_id, str(name), str(name), raw_unit.strip()),
            ).fetchall()
            has_validated_match = False
            matching_flagged_ids: list[str] = []
            for vr in v_by_name:
                cand_text = str(vr["value_text"]).strip()
                matched = False
                if input_dec is not None:
                    try:
                        cand_dec = Decimal(cand_text)
                        if cand_dec.is_finite() and cand_dec == input_dec:
                            matched = True
                    except InvalidOperation:
                        pass
                if not matched and cand_text == str(raw_val).strip():
                    matched = True
                if matched:
                    if str(vr["status"]) in {"passed", "corrected"}:
                        has_validated_match = True
                    elif str(vr["status"]) in {"exception", "confirmed"}:
                        matching_flagged_ids.append(str(vr["id"]))
            if not has_validated_match:
                for fid in matching_flagged_ids:
                    flagged_ids.add(fid)
    return sorted(flagged_ids)


def ingest_validated_calc_v0_run(
    workspace_id: str,
    run_id: str,
    *,
    validated_outputs: dict[str, dict[str, object]] | None = None,
) -> SimulationResultBatchRead | None:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_workspace(connection, workspace_id)
            run = connection.execute(
                """
                SELECT r.*, mv.implementation_kind
                FROM simulation_runs r
                LEFT JOIN model_versions mv ON mv.id = r.model_version_id
                WHERE r.id = ? AND r.workspace_id = ?
                """,
                (run_id, workspace_id),
            ).fetchone()
            if run is None:
                raise ProjectKnowledgeError("scalar_run_missing", "Simulation run was not found in workspace.")
            if str(run["status"]) != "succeeded" or not run["output_payload"]:
                connection.rollback()
                return None
            impl_kind = str(run["implementation_kind"] or "")
            if impl_kind not in {"calc_v0", "studio_calc_v1"}:
                connection.rollback()
                return None

            output_payload = str(run["output_payload"])
            source_digest = hashlib.sha256(output_payload.encode("utf-8")).hexdigest()
            existing = connection.execute(
                """
                SELECT id FROM simulation_result_batches
                WHERE run_id = ? AND source_payload_digest = ? AND extractor_version = ?
                """,
                (run_id, source_digest, SCALAR_EXTRACTOR_VERSION),
            ).fetchone()
            if existing is not None:
                batch_read = _read_batch_from_connection(connection, workspace_id, str(existing["id"]))
                connection.commit()
                return batch_read

            has_succeeded_job = connection.execute(
                """
                SELECT id FROM runner_jobs
                WHERE simulation_run_id = ? AND workspace_id = ? AND status = 'succeeded'
                LIMIT 1
                """,
                (run_id, workspace_id),
            ).fetchone()
            has_calc_artifact = connection.execute(
                """
                SELECT id FROM run_artifacts
                WHERE simulation_run_id = ? AND workspace_id = ? AND role = 'calc_result_json'
                LIMIT 1
                """,
                (run_id, workspace_id),
            ).fetchone()
            if has_succeeded_job is None or has_calc_artifact is None:
                connection.rollback()
                return None

            try:
                decoded_payload = json.loads(output_payload)
            except (TypeError, json.JSONDecodeError):
                connection.rollback()
                return None
            if not isinstance(decoded_payload, dict):
                connection.rollback()
                return None
            raw_outputs = decoded_payload.get("outputs")
            if not isinstance(raw_outputs, dict) or not raw_outputs:
                connection.rollback()
                return None
            for out_name, out_item in raw_outputs.items():
                if not isinstance(out_name, str) or not out_name.strip() or not isinstance(out_item, dict):
                    connection.rollback()
                    return None
                raw_val = out_item.get("value")
                if isinstance(raw_val, bool):
                    connection.rollback()
                    return None
                try:
                    num_val = float(raw_val)  # type: ignore[arg-type]
                except (OverflowError, TypeError, ValueError):
                    connection.rollback()
                    return None
                if not isfinite(num_val):
                    connection.rollback()
                    return None
                if not isinstance(out_item.get("unit"), str) or not str(out_item.get("unit")).strip():
                    connection.rollback()
                    return None
            if validated_outputs is not None and set(validated_outputs.keys()) != set(raw_outputs.keys()):
                connection.rollback()
                return None

            batch_id = str(uuid4())
            now = utc_now()
            connection.execute(
                """
                INSERT INTO simulation_result_batches (
                    id, workspace_id, run_id, source_payload_digest,
                    extractor_version, notification_state, created_at
                ) VALUES (?, ?, ?, ?, ?, 'none', ?)
                """,
                (batch_id, workspace_id, run_id, source_digest, SCALAR_EXTRACTOR_VERSION, now),
            )

            outputs_canonical = _canonical_json(raw_outputs)
            input_payload = run["input_payload"]
            upstream_flagged = _unvalidated_input_value_ids_for_run(connection, workspace_id, input_payload)

            prepared_peers: list[dict[str, object]] = []
            extracted_items: list[tuple[str, dict[str, object], str, str, str | None, dict[str, object]]] = []
            for output_name in sorted(raw_outputs.keys()):
                raw_item = raw_outputs[output_name]
                if not isinstance(raw_item, dict):
                    connection.rollback()
                    return None
                val_text, val_unit = _extract_scalar_from_run_payload(outputs_canonical, output_name)
                raw_qty = raw_item.get("quantity")
                qty: str | None = str(raw_qty).strip() if isinstance(raw_qty, str) and raw_qty.strip() else None
                if qty is None:
                    basis_qty_row = connection.execute(
                        """
                        SELECT name FROM parameters
                        WHERE workspace_id = ? AND name = ?
                          AND status = 'accepted' AND lifecycle_state = 'active'
                        LIMIT 1
                        """,
                        (workspace_id, output_name.strip()),
                    ).fetchone()
                    if basis_qty_row is not None:
                        qty = str(basis_qty_row["name"]).strip()
                    else:
                        req_qty_row = connection.execute(
                            """
                            SELECT criterion_output_name FROM requirements
                            WHERE workspace_id = ? AND status = 'active'
                              AND basis_kind = 'acceptance_criterion'
                              AND criterion_output_name = ?
                            LIMIT 1
                            """,
                            (workspace_id, output_name.strip()),
                        ).fetchone()
                        if req_qty_row is not None:
                            qty = str(req_qty_row["criterion_output_name"]).strip()
                conds = _extract_conditions(raw_item)
                if upstream_flagged:
                    conds = {**conds, "_unvalidated_input_value_ids": upstream_flagged}
                extracted_items.append((output_name, raw_item, val_text, val_unit, qty, conds))
                prepared_peers.append(
                    {
                        "output_name": output_name,
                        "quantity": qty,
                        "value_text": val_text,
                        "unit": val_unit,
                        "conditions_json": _canonical_json(conds),
                    }
                )

            has_exceptions = False
            for output_name, raw_item, val_text, val_unit, qty, conds in extracted_items:
                checks = _evaluate_value_checks(
                    connection,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    value_id=None,
                    output_name=output_name,
                    source_payload=output_payload,
                    source_payload_digest=source_digest,
                    value_text=val_text,
                    unit=val_unit,
                    quantity=qty,
                    conditions=conds,
                    output_item=raw_item,
                    peer_items=prepared_peers,
                )
                failed = any(outcome == "failed" for _name, outcome, _reason in checks)
                status = "exception" if failed else "passed"
                if failed:
                    has_exceptions = True

                scalar_id: str | None = None
                existing_scalar = connection.execute(
                    "SELECT id FROM simulation_run_scalar_results WHERE run_id = ? AND output_name = ?",
                    (run_id, output_name),
                ).fetchone()
                if existing_scalar is not None:
                    scalar_id = str(existing_scalar["id"])
                else:
                    scalar_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO simulation_run_scalar_results (
                            id, run_id, output_name, value_text, unit, source_payload_digest,
                            extractor_id, extractor_version, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scalar_id,
                            run_id,
                            output_name,
                            val_text,
                            val_unit,
                            source_digest,
                            SCALAR_EXTRACTOR_ID,
                            SCALAR_EXTRACTOR_VERSION,
                            now,
                        ),
                    )

                value_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO simulation_result_values (
                        id, batch_id, output_name, source_payload_digest,
                        extractor_id, extractor_version, quantity, value_text, unit,
                        conditions_json, status, admitted_scalar_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value_id,
                        batch_id,
                        output_name,
                        source_digest,
                        SCALAR_EXTRACTOR_ID,
                        SCALAR_EXTRACTOR_VERSION,
                        qty,
                        val_text,
                        val_unit,
                        _canonical_json(conds),
                        status,
                        scalar_id,
                        now,
                    ),
                )
                for check_name, outcome, reason_code in checks:
                    connection.execute(
                        """
                        INSERT INTO simulation_result_checks (
                            id, value_id, check_name, outcome, reason_code, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (str(uuid4()), value_id, check_name, outcome, reason_code, now),
                    )

            if has_exceptions:
                connection.execute(
                    "UPDATE simulation_result_batches SET notification_state = 'pending' WHERE id = ?",
                    (batch_id,),
                )

            batch_read = _read_batch_from_connection(connection, workspace_id, batch_id)
            connection.commit()
            return batch_read
        except Exception:
            connection.rollback()
            raise


def _read_value_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    run_id: str,
    row: sqlite3.Row,
    *,
    batch_unvalidated_inputs: list[str] | None = None,
) -> SimulationResultValueRead:
    value_id = str(row["id"])
    check_rows = connection.execute(
        "SELECT * FROM simulation_result_checks WHERE value_id = ? ORDER BY check_name ASC",
        (value_id,),
    ).fetchall()
    checks = [
        SimulationResultCheckRead(
            id=str(cr["id"]),
            value_id=value_id,
            check_name=str(cr["check_name"]),
            outcome=cast(Literal["passed", "failed"], str(cr["outcome"])),
            reason_code=str(cr["reason_code"]),
            created_at=str(cr["created_at"]),
        )
        for cr in check_rows
    ]
    res_row = connection.execute(
        "SELECT * FROM simulation_result_resolutions WHERE value_id = ?",
        (value_id,),
    ).fetchone()
    resolution: SimulationResultResolutionRead | None = None
    if res_row is not None:
        resolution = SimulationResultResolutionRead(
            id=str(res_row["id"]),
            value_id=value_id,
            action=cast(Literal["correct", "confirm"], str(res_row["action"])),
            reason=str(res_row["reason"]),
            actor=str(res_row["actor"]),
            original_value=str(res_row["original_value_text"]),
            original_unit=str(res_row["original_unit"]),
            original_conditions=json.loads(str(res_row["original_conditions_json"])),
            corrected_value=res_row["corrected_value_text"],
            corrected_unit=res_row["corrected_unit"],
            corrected_conditions=(
                None
                if not res_row["corrected_conditions_json"]
                else json.loads(str(res_row["corrected_conditions_json"]))
            ),
            created_at=str(res_row["created_at"]),
        )
    raw_conds = json.loads(str(row["conditions_json"]))
    internal_unvalidated = raw_conds.pop("_unvalidated_input_value_ids", []) if isinstance(raw_conds, dict) else []
    status = cast(Literal["passed", "exception", "corrected", "confirmed"], str(row["status"]))
    unvalidated_source = status in {"exception", "confirmed"} or bool(internal_unvalidated) or bool(batch_unvalidated_inputs)
    return SimulationResultValueRead(
        id=value_id,
        batch_id=str(row["batch_id"]),
        run_id=run_id,
        workspace_id=workspace_id,
        output_name=str(row["output_name"]),
        source_payload_digest=str(row["source_payload_digest"]),
        extractor_id=str(row["extractor_id"]),
        extractor_version=str(row["extractor_version"]),
        quantity=row["quantity"],
        value=str(row["value_text"]),
        unit=str(row["unit"]),
        conditions=raw_conds if isinstance(raw_conds, dict) else {},
        status=status,
        admitted_scalar_id=row["admitted_scalar_id"],
        unvalidated_source=unvalidated_source,
        target_href=_target_href(workspace_id, run_id, value_id),
        checks=checks,
        resolution=resolution,
        created_at=str(row["created_at"]),
    )


def _read_batch_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    batch_id: str,
) -> SimulationResultBatchRead:
    batch = connection.execute(
        "SELECT * FROM simulation_result_batches WHERE id = ? AND workspace_id = ?",
        (batch_id, workspace_id),
    ).fetchone()
    if batch is None:
        raise ProjectKnowledgeError("result_batch_not_found", "Simulation result batch was not found in workspace.")
    run_id = str(batch["run_id"])
    run_row = connection.execute(
        "SELECT input_payload FROM simulation_runs WHERE id = ? AND workspace_id = ?",
        (run_id, workspace_id),
    ).fetchone()
    unvalidated_inputs = _unvalidated_input_value_ids_for_run(
        connection, workspace_id, None if run_row is None else run_row["input_payload"]
    )
    value_rows = connection.execute(
        "SELECT * FROM simulation_result_values WHERE batch_id = ? ORDER BY output_name ASC",
        (batch_id,),
    ).fetchall()
    values = [
        _read_value_from_connection(
            connection,
            workspace_id,
            run_id,
            vr,
            batch_unvalidated_inputs=unvalidated_inputs,
        )
        for vr in value_rows
    ]
    exception_count = sum(1 for item in values if item.status == "exception")
    return SimulationResultBatchRead(
        id=batch_id,
        workspace_id=workspace_id,
        run_id=run_id,
        source_payload_digest=str(batch["source_payload_digest"]),
        extractor_version=str(batch["extractor_version"]),
        notification_state=cast(Literal["none", "pending", "read"], str(batch["notification_state"])),
        exception_count=exception_count,
        unvalidated_input_value_ids=unvalidated_inputs,
        values=values,
        created_at=str(batch["created_at"]),
    )


def list_simulation_result_batches(workspace_id: str) -> list[SimulationResultBatchRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            "SELECT id FROM simulation_result_batches WHERE workspace_id = ? ORDER BY created_at DESC, id DESC",
            (workspace_id,),
        ).fetchall()
        return [_read_batch_from_connection(connection, workspace_id, str(row["id"])) for row in rows]


def list_simulation_result_notifications(workspace_id: str) -> list[SimulationResultNotificationRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            """
            SELECT * FROM simulation_result_batches
            WHERE workspace_id = ? AND notification_state IN ('pending', 'read')
            ORDER BY created_at DESC, id DESC
            """,
            (workspace_id,),
        ).fetchall()
        notifications: list[SimulationResultNotificationRead] = []
        for row in rows:
            batch = _read_batch_from_connection(connection, workspace_id, str(row["id"]))
            targets = [
                SimulationResultNotificationTarget(
                    value_id=val.id,
                    output_name=val.output_name,
                    status="exception" if val.status == "exception" else "confirmed",
                    failed_check_codes=[c.reason_code for c in val.checks if c.outcome == "failed"],
                    target_href=val.target_href,
                )
                for val in batch.values
                if val.status in {"exception", "confirmed"} and any(c.outcome == "failed" for c in val.checks)
            ]
            if not targets:
                continue
            notifications.append(
                SimulationResultNotificationRead(
                    batch_id=batch.id,
                    workspace_id=workspace_id,
                    run_id=batch.run_id,
                    notification_state="pending" if batch.notification_state == "pending" else "read",
                    exception_count=len(targets),
                    message=f"Run {batch.run_id[:8]} ingested with {len(targets)} flagged value(s) requiring review.",
                    targets=targets,
                    created_at=batch.created_at,
                )
            )
        return notifications


def resolve_simulation_result_exception(
    value_id: str,
    payload: SimulationResultResolveRequest,
) -> SimulationResultValueRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_workspace(connection, payload.workspace_id)
            row = connection.execute(
                """
                SELECT v.*, b.workspace_id, b.run_id, r.output_payload, r.input_payload
                FROM simulation_result_values v
                JOIN simulation_result_batches b ON b.id = v.batch_id
                JOIN simulation_runs r ON r.id = b.run_id
                WHERE v.id = ? AND b.workspace_id = ?
                """,
                (value_id, payload.workspace_id),
            ).fetchone()
            if row is None:
                raise ProjectKnowledgeError("result_value_not_found", "Simulation result value was not found in workspace.")
            if str(row["status"]) != "exception":
                raise ProjectKnowledgeError("result_value_not_exception", "Only unresolved exceptions can be corrected or confirmed.")

            existing_res = connection.execute(
                "SELECT * FROM simulation_result_resolutions WHERE value_id = ?",
                (value_id,),
            ).fetchone()
            orig_value = str(existing_res["original_value_text"]) if existing_res else str(row["value_text"])
            orig_unit = str(existing_res["original_unit"]) if existing_res else str(row["unit"])
            orig_conds_json = str(existing_res["original_conditions_json"]) if existing_res else str(row["conditions_json"])
            orig_conds = json.loads(orig_conds_json)
            internal_unvalidated = orig_conds.get("_unvalidated_input_value_ids") if isinstance(orig_conds, dict) else None

            now = utc_now()
            run_id = str(row["run_id"])
            batch_id = str(row["batch_id"])
            output_name = str(row["output_name"])
            source_payload = str(row["output_payload"] or "")
            source_digest = str(row["source_payload_digest"])
            qty: str | None = (
                payload.corrected_quantity.strip()
                if payload.corrected_quantity is not None and payload.corrected_quantity.strip()
                else (str(row["quantity"]).strip() if row["quantity"] is not None and str(row["quantity"]).strip() else None)
            )
            if qty is None and payload.action == "correct":
                basis_qty_row = connection.execute(
                    """
                    SELECT name FROM parameters
                    WHERE workspace_id = ? AND name = ?
                      AND status = 'accepted' AND lifecycle_state = 'active'
                    LIMIT 1
                    """,
                    (payload.workspace_id, output_name.strip()),
                ).fetchone()
                if basis_qty_row is not None:
                    qty = str(basis_qty_row["name"]).strip()
                else:
                    req_qty_row = connection.execute(
                        """
                        SELECT criterion_output_name FROM requirements
                        WHERE workspace_id = ? AND status = 'active'
                          AND basis_kind = 'acceptance_criterion'
                          AND criterion_output_name = ?
                        LIMIT 1
                        """,
                        (payload.workspace_id, output_name.strip()),
                    ).fetchone()
                    if req_qty_row is not None:
                        qty = str(req_qty_row["criterion_output_name"]).strip()

            if payload.action == "confirm":
                new_status = "confirmed"
                corr_value = None
                corr_unit = None
                corr_conds_json = None
                effective_val = str(row["value_text"])
                effective_unit = str(row["unit"])
            else:
                try:
                    dec_corr = Decimal(str(payload.corrected_value).strip())
                except InvalidOperation as exc:
                    raise ProjectKnowledgeError("scalar_non_numeric", "Corrected value must be numeric.") from exc
                if not dec_corr.is_finite():
                    raise ProjectKnowledgeError("scalar_non_finite", "Corrected value must be finite.")
                corr_value = format(dec_corr, "f")
                corr_unit = (payload.corrected_unit or str(row["unit"])).strip()
                base_conds = {k: v for k, v in (orig_conds.items() if isinstance(orig_conds, dict) else ()) if k != "_unvalidated_input_value_ids"}
                next_conds: dict[str, object] = dict(payload.corrected_conditions) if payload.corrected_conditions is not None else base_conds
                if internal_unvalidated:
                    next_conds["_unvalidated_input_value_ids"] = internal_unvalidated
                corr_conds_json = _canonical_json(next_conds)
                effective_val = corr_value
                effective_unit = corr_unit

                # Re-run deterministic checks on the corrected value
                decoded_root = json.loads(source_payload) if source_payload else {}
                outputs_obj = decoded_root.get("outputs") if isinstance(decoded_root, dict) else None
                raw_outputs: dict[str, object] = dict(outputs_obj) if isinstance(outputs_obj, dict) else {}
                raw_item_candidate = raw_outputs.get(output_name)
                raw_item = dict(raw_item_candidate) if isinstance(raw_item_candidate, dict) else {}
                peer_rows = connection.execute(
                    "SELECT output_name, quantity, value_text, unit, conditions_json FROM simulation_result_values WHERE batch_id = ? AND id != ?",
                    (batch_id, value_id),
                ).fetchall()
                peers = [dict(pr) for pr in peer_rows]
                checks = _evaluate_value_checks(
                    connection,
                    workspace_id=payload.workspace_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    value_id=value_id,
                    output_name=output_name,
                    source_payload=source_payload,
                    source_payload_digest=source_digest,
                    value_text=corr_value,
                    unit=corr_unit,
                    quantity=qty,
                    conditions={k: v for k, v in next_conds.items() if k != "_unvalidated_input_value_ids"},
                    output_item=raw_item,
                    peer_items=peers,
                    is_correction=True,
                )
                connection.execute("DELETE FROM simulation_result_checks WHERE value_id = ?", (value_id,))
                for check_name, outcome, reason_code in checks:
                    connection.execute(
                        """
                        INSERT INTO simulation_result_checks (
                            id, value_id, check_name, outcome, reason_code, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (str(uuid4()), value_id, check_name, outcome, reason_code, now),
                    )
                failed = any(outcome == "failed" for _n, outcome, _r in checks)
                new_status = "exception" if failed else "corrected"

                connection.execute(
                    """
                    UPDATE simulation_result_values
                    SET quantity = ?, value_text = ?, unit = ?, conditions_json = ?, status = ?
                    WHERE id = ?
                    """,
                    (qty, corr_value, corr_unit, corr_conds_json, new_status, value_id),
                )

            if payload.action == "confirm":
                connection.execute(
                    "UPDATE simulation_result_values SET status = 'confirmed' WHERE id = ?",
                    (value_id,),
                )

            if existing_res is not None:
                connection.execute(
                    """
                    UPDATE simulation_result_resolutions
                    SET action = ?, reason = ?, actor = 'operator',
                        corrected_value_text = ?, corrected_unit = ?, corrected_conditions_json = ?,
                        created_at = ?
                    WHERE value_id = ?
                    """,
                    (
                        payload.action,
                        payload.reason.strip(),
                        corr_value,
                        corr_unit,
                        corr_conds_json,
                        now,
                        value_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO simulation_result_resolutions (
                        id, value_id, action, reason, actor,
                        original_value_text, original_unit, original_conditions_json,
                        corrected_value_text, corrected_unit, corrected_conditions_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, 'operator', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        value_id,
                        payload.action,
                        payload.reason.strip(),
                        orig_value,
                        orig_unit,
                        orig_conds_json,
                        corr_value,
                        corr_unit,
                        corr_conds_json,
                        now,
                    ),
                )

            # Update batch notification_state to 'read' once all exceptions in the batch are resolved
            remaining_unresolved = connection.execute(
                "SELECT COUNT(*) AS cnt FROM simulation_result_values WHERE batch_id = ? AND status = 'exception'",
                (batch_id,),
            ).fetchone()["cnt"]
            if int(remaining_unresolved) == 0:
                connection.execute(
                    "UPDATE simulation_result_batches SET notification_state = 'read' WHERE id = ? AND notification_state = 'pending'",
                    (batch_id,),
                )

            # Invalidate dependent evidence through existing freshness graph where connected,
            # and mark dependent requirement validations stale when value changed
            _invalidate_dependent_evidence(
                connection,
                workspace_id=payload.workspace_id,
                run_id=run_id,
                output_name=output_name,
                scalar_id=row["admitted_scalar_id"],
                effective_value=effective_val,
                effective_unit=effective_unit,
                now=now,
            )

            updated_row = connection.execute(
                "SELECT * FROM simulation_result_values WHERE id = ?",
                (value_id,),
            ).fetchone()
            assert updated_row is not None
            run_row = connection.execute(
                "SELECT input_payload FROM simulation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            unvalidated_inputs = _unvalidated_input_value_ids_for_run(
                connection, payload.workspace_id, None if run_row is None else run_row["input_payload"]
            )
            result = _read_value_from_connection(
                connection,
                payload.workspace_id,
                run_id,
                updated_row,
                batch_unvalidated_inputs=unvalidated_inputs,
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def _invalidate_dependent_evidence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    output_name: str,
    scalar_id: str | None,
    effective_value: str,
    effective_unit: str,
    now: str,
) -> None:
    del scalar_id, effective_value, effective_unit
    # Find any parameter derived from this run's runner_job or simulation_run and record freshness invalidation if linked
    job_rows = connection.execute(
        "SELECT id FROM runner_jobs WHERE simulation_run_id = ? AND workspace_id = ?",
        (run_id, workspace_id),
    ).fetchall()
    source_refs = [f"simulation_run:{run_id}", *(f"runner_job:{jr['id']}" for jr in job_rows)]
    placeholders = ",".join("?" for _ in source_refs)
    param_rows = connection.execute(
        f"""
        SELECT id FROM parameters
        WHERE workspace_id = ? AND name = ? AND source_ref IN ({placeholders})
        """,
        (workspace_id, output_name, *source_refs),
    ).fetchall()
    for pr in param_rows:
        superseded_param_id = str(pr["id"])
        already_invalidated = connection.execute(
            "SELECT id FROM freshness_invalidations WHERE workspace_id = ? AND superseded_parameter_id = ?",
            (workspace_id, superseded_param_id),
        ).fetchone()
        if already_invalidated is not None:
            continue
        try:
            prepared: PreparedFreshnessInvalidation = prepare_freshness_invalidation(
                connection,
                workspace_id=workspace_id,
                superseded_parameter_id=superseded_param_id,
                replacement_parameter_id=superseded_param_id,
                created_at=now,
            )
        except FreshnessError as exc:
            if exc.code == "parameter_replacement_not_found":
                continue
            raise ProjectKnowledgeError(exc.code, exc.message) from exc
        persist_freshness_invalidation(connection, prepared)
