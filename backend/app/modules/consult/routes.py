"""Consult routes (spec 380): read-only status, the pending packet, and the maintainer's approve / edit / reject.

No route writes ``consult.toml``: mode, brief approval and trusted accounts are the maintainer's file in the vault.
Until the Relay consult session exists (part b) there is no transport, so approval refuses ``transport_unavailable``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.database import open_sqlite_connection
from app.modules.consult.brief import BriefUnavailable, approved_brief, current_brief_digest
from app.modules.consult.config import load_consult_config
from app.modules.consult.service import (
    MAX_CONTEXT,
    MAX_QUESTION,
    ConsultOutcome,
    ConsultTransport,
    approve_consult,
    edit_consult,
    read_answer,
    reject_consult,
)
from app.modules.consult.transport import DEFAULT_CONSULT_TRANSPORT
from app.modules.events.service import utc_now

router = APIRouter(prefix="/ai/consult", tags=["consult"])

TRANSPORT: ConsultTransport | None = DEFAULT_CONSULT_TRANSPORT


class ConsultApproval(BaseModel):
    packet_sha256: str = Field(min_length=64, max_length=64)


class ConsultEdit(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    context: str = Field(default="", max_length=MAX_CONTEXT)


class ConsultFeedback(BaseModel):
    verdict: str = Field(pattern=r"^(accepted|rejected)$")
    note: str | None = Field(default=None, max_length=300)
    workspace_id: str | None = None


def _outcome(outcome: ConsultOutcome) -> dict[str, Any]:
    return {"status": outcome.status, "code": outcome.code, "consult_id": outcome.consult_id,
            "reason": outcome.reason, "offending": list(outcome.offending), "answer_id": outcome.answer_id}


@router.get("/status")
def read_consult_status() -> dict[str, Any]:
    config = load_consult_config()
    try:
        approved_brief(config.brief_sha256)
        brief_approved = True
    except BriefUnavailable:
        brief_approved = False
    current_digest = current_brief_digest()
    audit_items: list[dict[str, Any]] = []
    if config.mode == "auto":
        with open_sqlite_connection(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT id, kind, model_id, state, created_at, packet_sha256
                FROM consult_attempts
                WHERE audit = 1
                ORDER BY created_at DESC, rowid DESC
                LIMIT 20
                """
            ).fetchall()
            audit_items = [dict(row) for row in rows]
    return {
        "mode": config.mode,
        "reason": config.reason,
        "brief_approved": brief_approved,
        "current_brief_digest": current_digest,
        "trusted_accounts": [{"id": a.id, "training_off_verified_on": a.training_off_verified_on.isoformat()}
                             for a in config.trusted_accounts],
        "sage_model_id": config.sage_model_id,
        "audit_items": audit_items,
    }


@router.get("/{consult_id}")
def read_consult_attempt(consult_id: str) -> dict[str, Any]:
    with open_sqlite_connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT id, kind, state, mode, packet_text, packet_sha256, brief_sha256, modules_json, gate_steps_json, "
            "result_code, account_id, model_id, approval, audit, created_at FROM consult_attempts WHERE id = ?",
            (consult_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="consult_unknown")
    record = dict(row)
    record["modules"] = json.loads(record.pop("modules_json"))
    record["gate_steps"] = json.loads(record.pop("gate_steps_json"))
    record["audit"] = bool(record["audit"])
    return record


@router.post("/{consult_id}/approve")
def approve_consult_attempt(consult_id: str, payload: ConsultApproval) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        outcome = approve_consult(connection, consult_id, payload.packet_sha256, transport=TRANSPORT)
        connection.commit()
    return _outcome(outcome)


@router.post("/{consult_id}/edit")
def edit_consult_attempt(consult_id: str, payload: ConsultEdit) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        outcome = edit_consult(connection, consult_id, payload.question, payload.context)
        connection.commit()
    return _outcome(outcome)


@router.post("/{consult_id}/reject")
def reject_consult_attempt(consult_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        outcome = reject_consult(connection, consult_id)
        connection.commit()
    return _outcome(outcome)


@router.get("/{consult_id}/answer")
def read_consult_answer(consult_id: str, offset: int = 0) -> dict[str, Any]:
    with open_sqlite_connection(read_only=True) as connection:
        return read_answer(connection, consult_id, offset)


@router.post("/{consult_id}/feedback")
def record_consult_feedback(
    consult_id: str,
    payload: ConsultFeedback,
    workspace_id: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
) -> dict[str, Any]:
    current_ws = payload.workspace_id or workspace_id or x_workspace_id
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT id, workspace_id, state, gate_steps_json FROM consult_attempts WHERE id = ?",
            (consult_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="consult_unknown")

        attempt_id, attempt_ws, state, gate_steps_json = row[0], row[1], row[2], row[3]
        if state != "sent":
            raise HTTPException(status_code=400, detail="consult_not_sent")

        if current_ws and attempt_ws and current_ws != attempt_ws:
            raise HTTPException(status_code=400, detail="cross_workspace")

        trigger_codes: list[str] = []
        if gate_steps_json:
            try:
                gate_steps = json.loads(gate_steps_json)
                for step in gate_steps:
                    if step.get("step") == "triggers" and "trigger_codes" in step:
                        trigger_codes.extend(step["trigger_codes"])
            except Exception:
                pass

        feedback_id = f"cf-{uuid.uuid4().hex[:16]}"
        now = utc_now()
        connection.execute(
            """
            INSERT INTO consult_feedback (id, attempt_id, verdict, note, trigger_codes_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (feedback_id, attempt_id, payload.verdict, payload.note, json.dumps(trigger_codes), now),
        )
        connection.commit()
    return {
        "status": "ok",
        "feedback_id": feedback_id,
        "consult_id": consult_id,
        "verdict": payload.verdict,
        "note": payload.note,
        "trigger_codes": trigger_codes,
        "created_at": now,
    }


@router.get("/{consult_id}/feedback")
def read_consult_feedback(consult_id: str) -> dict[str, Any]:
    with open_sqlite_connection(read_only=True) as connection:
        row = connection.execute(
            """
            SELECT id, attempt_id, verdict, note, trigger_codes_json, created_at
            FROM consult_feedback
            WHERE attempt_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (consult_id,),
        ).fetchone()
    if row is None:
        return {"status": "none", "consult_id": consult_id, "verdict": None}
    return {
        "status": "ok",
        "feedback_id": row[0],
        "consult_id": row[1],
        "verdict": row[2],
        "note": row[3],
        "trigger_codes": json.loads(row[4]) if row[4] else [],
        "created_at": row[5],
    }
