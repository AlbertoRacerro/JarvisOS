"""Consult routes (spec 380): read-only status, the pending packet, and the maintainer's approve / edit / reject.

No route writes ``consult.toml``: mode, brief approval and trusted accounts are the maintainer's file in the vault.
Until the Relay consult session exists (part b) there is no transport, so approval refuses ``transport_unavailable``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.database import open_sqlite_connection
from app.modules.consult.brief import BriefUnavailable, approved_brief
from app.modules.consult.config import load_consult_config
from app.modules.consult.service import (
    MAX_CONTEXT,
    MAX_QUESTION,
    ConsultOutcome,
    approve_consult,
    edit_consult,
    read_answer,
    reject_consult,
)

router = APIRouter(prefix="/ai/consult", tags=["consult"])


class ConsultApproval(BaseModel):
    packet_sha256: str = Field(min_length=64, max_length=64)


class ConsultEdit(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    context: str = Field(default="", max_length=MAX_CONTEXT)


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
    return {
        "mode": config.mode,
        "reason": config.reason,
        "brief_approved": brief_approved,
        "trusted_accounts": [{"id": a.id, "training_off_verified_on": a.training_off_verified_on.isoformat()}
                             for a in config.trusted_accounts],
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
        outcome = approve_consult(connection, consult_id, payload.packet_sha256, transport=None)
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
