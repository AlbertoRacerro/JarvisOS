"""Degrees-of-freedom and missing-inputs report for process drafts (spec 257).

Deterministic read-only report listing required unit parameters and feed specifications
as set, unset or available from prior simulation results on the design-value path.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from app.core.database import open_sqlite_connection
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import (
    FEED_ALTERNATIVES,
    QUANTITY_UNITS,
    STREAM_SPECS,
    DraftQuantity,
)


def _match_design_value(
    connection: sqlite3.Connection,
    workspace_id: str,
    tag: str,
    key: str,
    quantity_kind: str,
) -> dict[str, Any] | None:
    allowed_units = QUANTITY_UNITS.get(quantity_kind, (None, ()))[1]
    row = connection.execute(
        """
        SELECT v.id AS value_id, v.value_text, v.unit, v.status, v.conditions_json, v.output_name,
               b.run_id, b.id AS batch_id
        FROM simulation_result_values v
        JOIN simulation_result_batches b ON b.id = v.batch_id
        WHERE b.workspace_id = ? AND v.quantity = ?
        ORDER BY v.created_at DESC, v.id DESC
        LIMIT 1
        """,
        (workspace_id, f"{tag}.{key}"),
    ).fetchone()
    if row is None:
        return None

    # (a) stored unit is in QUANTITY_UNITS[quantity_kind][1]
    unit_str = str(row["unit"])
    if unit_str not in allowed_units:
        return None

    # (b) json.loads(v.conditions_json) with _unvalidated_input_value_ids removed is empty
    raw_cond = row["conditions_json"]
    if not raw_cond:
        conds: dict[str, Any] = {}
    else:
        try:
            conds = json.loads(raw_cond)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(conds, dict):
        return None

    unvalidated_inputs = conds.get("_unvalidated_input_value_ids")
    remaining = {k: v for k, v in conds.items() if k != "_unvalidated_input_value_ids"}
    if remaining:
        return None

    # (c) value text parses as a finite float
    try:
        val_float = float(row["value_text"])
        if not math.isfinite(val_float):
            return None
    except (ValueError, TypeError):
        return None

    try:
        converted = draft._si(DraftQuantity(value=val_float, unit=unit_str), quantity_kind, key)
    except Exception:
        return None

    status_str = str(row["status"])
    # Spec 253: an exception stays a red dot after the user confirms it; only passed and corrected values validate.
    validation = "red_dot" if (status_str in {"exception", "confirmed"} or bool(unvalidated_inputs)) else "passed"

    return {
        "value_id": str(row["value_id"]),
        "value": val_float,
        "unit": unit_str,
        "si": converted["si"],
        "source": {
            "run_id": str(row["run_id"]),
            "batch_id": str(row["batch_id"]),
            "output_name": str(row["output_name"]),
        },
        "validation": validation,
    }


def dof_report(workspace_id: str, draft_id: str) -> dict[str, Any]:
    """Compile the degrees-of-freedom report for a process draft head revision."""
    directory = draft.draft_dir(workspace_id, draft_id)
    head = draft._head(directory)
    record = draft.load_revision(directory, head["revision"])
    document = record["document"]
    objects = document.get("objects", {})

    entries: list[dict[str, Any]] = []

    with open_sqlite_connection() as connection:
        # 1. Units sorted by tag
        units = sorted(
            (item for item in objects.values() if item.get("kind") == "unit"),
            key=lambda item: item["tag"],
        )
        for unit in units:
            tag = unit["tag"]
            params = unit.get("params", {})
            for param in draft.required_unit_params(unit):
                is_set = param.key in params
                entry: dict[str, Any] = {
                    "object": tag,
                    "kind": "unit",
                    "key": param.key,
                    "label": param.label,
                    "quantity_kind": param.kind,
                    "status": "set" if is_set else "unset",
                }
                if not is_set:
                    dv = _match_design_value(connection, workspace_id, tag, param.key, param.kind)
                    if dv is not None:
                        entry["status"] = "available"
                        entry["design_value"] = dv
                entries.append(entry)

        # 2. Feed streams (kind 'stream', type not 'EnergyStream', source is None, sorted by tag)
        feed_streams = sorted(
            (
                item
                for item in objects.values()
                if item.get("kind") == "stream"
                and item.get("type") != "EnergyStream"
                and item.get("source") is None
            ),
            key=lambda item: item["tag"],
        )
        for stream in feed_streams:
            tag = stream["tag"]
            spec = stream.get("spec", {})

            # 2a. pressure
            is_set = "pressure" in spec
            pressure_spec = STREAM_SPECS["pressure"]
            p_entry: dict[str, Any] = {
                "object": tag,
                "kind": "stream",
                "key": "pressure",
                "label": pressure_spec[2],
                "quantity_kind": pressure_spec[0],
                "status": "set" if is_set else "unset",
            }
            if not is_set:
                dv = _match_design_value(connection, workspace_id, tag, "pressure", pressure_spec[0])
                if dv is not None:
                    p_entry["status"] = "available"
                    p_entry["design_value"] = dv
            entries.append(p_entry)

            # 2b. FEED_ALTERNATIVES groups
            for group_name, group_keys in FEED_ALTERNATIVES.items():
                keys = list(group_keys)
                is_set = any(k in spec for k in keys)
                label = " or ".join(STREAM_SPECS[k][2].lower() for k in keys)
                g_entry: dict[str, Any] = {
                    "object": tag,
                    "kind": "stream",
                    "key": group_name,
                    "label": label,
                    "alternatives": keys,
                    "status": "set" if is_set else "unset",
                }
                if not is_set:
                    for alt_key in keys:
                        alt_kind = STREAM_SPECS[alt_key][0]
                        dv = _match_design_value(connection, workspace_id, tag, alt_key, alt_kind)
                        if dv is not None:
                            g_entry["status"] = "available"
                            g_entry["design_value"] = dv
                            break
                entries.append(g_entry)

            # 2c. composition (composition never matches)
            is_set = bool(spec.get("composition"))
            c_entry: dict[str, Any] = {
                "object": tag,
                "kind": "stream",
                "key": "composition",
                "label": "Composition",
                "status": "set" if is_set else "unset",
            }
            entries.append(c_entry)

    counts = {
        "set": sum(1 for e in entries if e["status"] == "set"),
        "unset": sum(1 for e in entries if e["status"] == "unset"),
        "available": sum(1 for e in entries if e["status"] == "available"),
    }

    return {
        "workspace_id": workspace_id,
        "draft_id": draft_id,
        "revision": head["revision"],
        "entries": entries,
        "counts": counts,
    }
