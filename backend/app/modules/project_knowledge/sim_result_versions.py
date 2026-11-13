from __future__ import annotations

import sqlite3
from uuid import uuid4


def backfill_simulation_result_value_versions(
    connection: sqlite3.Connection,
    *,
    value_id: str | None = None,
) -> int:
    """Backfill simulation_result_value_versions for any unversioned values (idempotent)."""
    query = """
        SELECT
            v.id,
            v.value_text,
            v.unit,
            v.conditions_json,
            v.status,
            v.created_at,
            r.action AS res_action,
            r.reason AS res_reason,
            r.actor AS res_actor,
            r.created_at AS res_created_at,
            r.original_value_text,
            r.original_unit,
            r.original_conditions_json
        FROM simulation_result_values v
        LEFT JOIN simulation_result_resolutions r ON r.value_id = v.id
        WHERE NOT EXISTS (
            SELECT 1 FROM simulation_result_value_versions ver WHERE ver.value_id = v.id
        )
    """
    params: list[object] = []
    if value_id is not None:
        query += " AND v.id = ?"
        params.append(value_id)
    query += " ORDER BY v.created_at ASC, v.id ASC"

    rows = connection.execute(query, params).fetchall()
    inserted_count = 0
    for row in rows:
        v_id = str(row["id"])
        val_text = str(row["value_text"])
        val_unit = str(row["unit"])
        val_conds = str(row["conditions_json"])
        val_status = str(row["status"])
        val_created = str(row["created_at"])

        has_resolution = row["res_action"] is not None
        if not has_resolution:
            connection.execute(
                """
                INSERT INTO simulation_result_value_versions (
                    id, value_id, version_index, value_text, unit,
                    conditions_json, status, action, reason, actor,
                    created_at, origin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    v_id,
                    0,
                    val_text,
                    val_unit,
                    val_conds,
                    val_status,
                    "ingest",
                    None,
                    None,
                    val_created,
                    "backfilled",
                ),
            )
            inserted_count += 1
        else:
            orig_text = str(row["original_value_text"])
            orig_unit = str(row["original_unit"])
            orig_conds = str(row["original_conditions_json"])
            connection.execute(
                """
                INSERT INTO simulation_result_value_versions (
                    id, value_id, version_index, value_text, unit,
                    conditions_json, status, action, reason, actor,
                    created_at, origin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    v_id,
                    0,
                    orig_text,
                    orig_unit,
                    orig_conds,
                    "exception",
                    "ingest",
                    None,
                    None,
                    val_created,
                    "backfilled",
                ),
            )
            res_action = str(row["res_action"])
            res_reason = str(row["res_reason"]) if row["res_reason"] is not None else None
            res_actor = str(row["res_actor"]) if row["res_actor"] is not None else "operator"
            res_created = str(row["res_created_at"])
            connection.execute(
                """
                INSERT INTO simulation_result_value_versions (
                    id, value_id, version_index, value_text, unit,
                    conditions_json, status, action, reason, actor,
                    created_at, origin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    v_id,
                    1,
                    val_text,
                    val_unit,
                    val_conds,
                    val_status,
                    res_action,
                    res_reason,
                    res_actor,
                    res_created,
                    "backfilled",
                ),
            )
            inserted_count += 2

    return inserted_count
