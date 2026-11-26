"""Broker tool implementation for jarvis_turn_plan (Spec 362)."""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from app.core.database import open_sqlite_connection
from app.modules.ai.agent_contracts import CapabilityGrantRef, StructuredToolCall
from app.modules.events.service import log_event

VALID_STATUSES = frozenset({"pending", "in_progress", "done", "blocked"})
VALID_TASK_LABELS = frozenset({"creative", "physics", "literature", "engineering", "other"})
_FORBIDDEN_TERMS = ("token", "secret", "password", "credential", "authorization", "api_key")


def _sanitize_text(value: str) -> str:
    pack_path = os.environ.get("JARVISOS_PACK_PATH")
    if pack_path and pack_path in value:
        value = value.replace(pack_path, "[pack_root]")
    return value


def _has_secret(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in _FORBIDDEN_TERMS)


def dispatch_turn_plan_tool(
    call: StructuredToolCall,
    workspace_id: str,
    error: str,
    *,
    grant: CapabilityGrantRef | None = None,
    interaction_id: str | None = None,
    active_plan: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    if grant is None and (call.session_ref is None or not workspace_id):
        return None, "capability_denied"

    arguments = call.arguments
    if not isinstance(arguments, dict):
        return None, "invalid_arguments"

    op = arguments.get("op")
    if op not in {"set_plan", "update_step", "record_finding", "read_plan"}:
        return None, "invalid_op"

    # Reference or initialize plan dictionary
    if active_plan is None:
        active_plan = {}
    if "steps" not in active_plan or not isinstance(active_plan["steps"], list):
        active_plan["steps"] = []
    if "findings" not in active_plan or not isinstance(active_plan["findings"], list):
        active_plan["findings"] = []

    target_interaction_id: str = (
        interaction_id
        or (grant.scope.jarvis_thread_id if grant is not None else None)
        or (call.session_ref.jarvis_thread_id if call.session_ref is not None else None)
        or ""
    )

    if op == "read_plan":
        read_res: dict[str, Any] = {
            "status": "succeeded",
            "steps": list(active_plan["steps"]),
            "findings": list(active_plan["findings"]),
        }
        if "task_label" in active_plan:
            read_res["task_label"] = active_plan.get("task_label")
        return read_res, error

    if op == "set_plan":
        raw_task_label = arguments.get("task_label")
        if raw_task_label is not None:
            if not isinstance(raw_task_label, str) or raw_task_label.strip().lower() not in VALID_TASK_LABELS:
                return None, "invalid_task_label"
            active_plan["task_label"] = raw_task_label.strip().lower()

        raw_steps = arguments.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) > 10:
            return None, "invalid_steps"
        new_steps: list[dict[str, Any]] = []
        for step in raw_steps:
            if not isinstance(step, dict):
                return None, "invalid_steps"
            s_id = str(step.get("id") or "").strip()
            title = str(step.get("title") or "").strip()
            status = str(step.get("status") or "").strip().lower()
            note = step.get("note")
            if not s_id or len(s_id) > 64:
                return None, "invalid_step_id"
            if not title or len(title) > 160:
                return None, "invalid_step_title"
            if status not in VALID_STATUSES:
                return None, "invalid_step_status"
            if note is not None:
                note_str = str(note)
                if len(note_str) > 200:
                    return None, "invalid_step_note"
                note_str = _sanitize_text(note_str)
            else:
                note_str = None
            title_sanitized = _sanitize_text(title)
            new_steps.append({
                "id": s_id,
                "title": title_sanitized,
                "status": status,
                **({"note": note_str} if note_str is not None else {}),
            })
        active_plan["steps"] = new_steps
        _persist_plan_event(
            workspace_id=workspace_id,
            interaction_id=target_interaction_id,
            steps=active_plan["steps"],
            findings=active_plan["findings"],
            task_label=active_plan.get("task_label"),
        )
        set_res: dict[str, Any] = {
            "status": "succeeded",
            "steps": list(active_plan["steps"]),
            "findings": list(active_plan["findings"]),
        }
        if "task_label" in active_plan:
            set_res["task_label"] = active_plan.get("task_label")
        return set_res, error

    if op == "update_step":
        step_id = str(arguments.get("id") or arguments.get("step_id") or "").strip()
        status = str(arguments.get("status") or "").strip().lower()
        note = arguments.get("note")
        if not step_id or len(step_id) > 64:
            return None, "invalid_step_id"
        if status not in VALID_STATUSES:
            return None, "invalid_step_status"
        if note is not None:
            note_str = str(note)
            if len(note_str) > 200:
                return None, "invalid_step_note"
            note_str = _sanitize_text(note_str)
        else:
            note_str = None

        found = False
        for s in active_plan["steps"]:
            if s.get("id") == step_id:
                s["status"] = status
                if note_str is not None:
                    s["note"] = note_str
                found = True
                break
        if not found:
            return None, "step_not_found"

        _persist_plan_event(
            workspace_id=workspace_id,
            interaction_id=target_interaction_id,
            steps=active_plan["steps"],
            findings=active_plan["findings"],
        )
        return {
            "status": "succeeded",
            "steps": list(active_plan["steps"]),
            "findings": list(active_plan["findings"]),
        }, error

    if op == "record_finding":
        records_to_add: list[dict[str, Any]] = []
        if "findings" in arguments and isinstance(arguments["findings"], list):
            for item in arguments["findings"]:
                if isinstance(item, dict):
                    records_to_add.append(item)
        elif "finding" in arguments and isinstance(arguments["finding"], dict):
            records_to_add.append(arguments["finding"])
        elif "key" in arguments:
            records_to_add.append({
                "key": arguments.get("key"),
                "value": arguments.get("value"),
                "unit": arguments.get("unit"),
                "source_ref": arguments.get("source_ref"),
            })
        else:
            return None, "missing_finding"

        if not records_to_add:
            return None, "missing_finding"

        for rec in records_to_add:
            k = str(rec.get("key") or "").strip()
            v = rec.get("value")
            if not k or len(k) > 128:
                return None, "invalid_finding_key"
            if v is None:
                return None, "invalid_finding_value"
            u = str(rec.get("unit")).strip() if rec.get("unit") is not None else None
            if u is not None and len(u) > 32:
                return None, "invalid_finding_unit"
            s = str(rec.get("source_ref") or "").strip() if rec.get("source_ref") is not None else None
            if s is not None and len(s) > 200:
                return None, "invalid_finding_source_ref"

            val_str = str(v)
            total_len = len(k) + len(val_str) + len(u or "") + len(s or "")
            if total_len > 240:
                return None, "finding_too_large"

            # Check secrets and pack paths
            if _has_secret(k) or _has_secret(val_str) or (s and _has_secret(s)):
                return None, "finding_contains_secret"
            k = _sanitize_text(k)
            val_str = _sanitize_text(val_str)
            if s:
                s = _sanitize_text(s)

            # Store value keeping number type if original was int or float (excluding bool)
            stored_val: str | int | float = (
                v if isinstance(v, (int, float)) and not isinstance(v, bool) else val_str
            )
            clean_rec: dict[str, Any] = {"key": k, "value": stored_val}
            if u:
                clean_rec["unit"] = u
            if s:
                clean_rec["source_ref"] = s

            # Update if key already exists, else append
            existing_idx = next(
                (i for i, item in enumerate(active_plan["findings"]) if item.get("key") == k),
                None,
            )
            if existing_idx is not None:
                active_plan["findings"][existing_idx] = clean_rec
            else:
                if len(active_plan["findings"]) >= 24:
                    return None, "findings_limit_exceeded"
                active_plan["findings"].append(clean_rec)

        _persist_plan_event(
            workspace_id=workspace_id,
            interaction_id=target_interaction_id,
            steps=active_plan["steps"],
            findings=active_plan["findings"],
        )
        return {
            "status": "succeeded",
            "steps": list(active_plan["steps"]),
            "findings": list(active_plan["findings"]),
        }, error

    return None, "unhandled_op"


def _persist_plan_event(
    *, workspace_id: str, interaction_id: str, steps: list[dict[str, Any]], findings: list[dict[str, Any]],
    task_label: str | None = None,
) -> None:
    try:
        payload: dict[str, Any] = {
            "interaction_id": interaction_id,
            "steps": steps,
            "findings": findings,
        }
        if task_label is not None:
            payload["task_label"] = task_label
        with open_sqlite_connection() as connection:
            log_event(
                connection,
                event_type="hermes.turn_plan",
                actor="jarvis",
                target_type="ai_thread_interaction",
                target_id=interaction_id,
                workspace_id=workspace_id,
                payload=payload,
            )
            connection.commit()
    except sqlite3.Error:
        pass
