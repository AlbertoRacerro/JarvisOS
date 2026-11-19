"""Case comparison and verdict product object (spec 314, CASE-VERDICT-1).

Persists case comparison and verdict records as workspace product objects and provides
deterministic evaluation via Pint.
"""

from __future__ import annotations

import math
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from app.core.database import open_sqlite_connection
from app.modules.engineering import bench_runs_view
from app.modules.engineering.operator_models import CaseVerdictRead
from app.modules.engineering.operator_service import SAFE_ID
from app.modules.engineering.process_run_values import resolve_process_run_reference
from app.modules.engineering.readiness_board import _convert, _run_output
from app.modules.events.service import utc_now


class CreateCaseVerdictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    run_id: str
    quantity: str
    published_value: float
    published_unit: str
    rel_tol: float
    rationale: str | None = None


class UpdateCaseVerdictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["recorded", "accepted", "superseded"] | None = None
    rationale: str | None = None


def _require_workspace(workspace_id: str) -> None:
    if not SAFE_ID.fullmatch(workspace_id):
        raise HTTPException(
            404,
            detail={"code": "workspace_not_found", "message": f"Workspace '{workspace_id}' not found."},
        )
    with open_sqlite_connection(read_only=True) as connection:
        row = connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise HTTPException(
                404,
                detail={"code": "workspace_not_found", "message": f"Workspace '{workspace_id}' not found."},
            )


def list_verdicts(workspace_id: str) -> list[CaseVerdictRead]:
    """Lists stored verdicts for the workspace, newest first (404 if workspace does not exist)."""
    _require_workspace(workspace_id)
    with open_sqlite_connection(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT id, workspace_id, case_id, run_id, quantity, computed_value, computed_unit,
                   published_value, published_unit, rel_tol, deviation, verdict, status,
                   rationale, accepted_by, accepted_at, created_at, updated_at
            FROM case_verdicts
            WHERE workspace_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (workspace_id,),
        ).fetchall()
        return [
            CaseVerdictRead(
                id=str(r["id"]),
                workspace_id=str(r["workspace_id"]),
                case_id=str(r["case_id"]),
                run_id=str(r["run_id"]),
                quantity=str(r["quantity"]),
                computed_value=float(r["computed_value"]),
                computed_unit=str(r["computed_unit"]),
                published_value=float(r["published_value"]),
                published_unit=str(r["published_unit"]),
                rel_tol=float(r["rel_tol"]),
                deviation=float(r["deviation"]),
                verdict=r["verdict"],
                status=r["status"],
                rationale=r["rationale"],
                accepted_by=r["accepted_by"],
                accepted_at=r["accepted_at"],
                created_at=str(r["created_at"]),
                updated_at=str(r["updated_at"]),
            )
            for r in rows
        ]


def create_verdict(workspace_id: str, payload: CreateCaseVerdictRequest) -> CaseVerdictRead:
    """Deterministically computes deviation and verdict, recording a new case_verdict row."""
    _require_workspace(workspace_id)

    if not payload.case_id or not payload.case_id.strip():
        raise HTTPException(
            422,
            detail={"code": "case_id_invalid", "message": "case_id must be a non-empty string."},
        )
    if not payload.run_id or not payload.run_id.strip():
        raise HTTPException(
            422,
            detail={"code": "run_id_invalid", "message": "run_id must be a non-empty string."},
        )
    if not payload.quantity or not payload.quantity.strip():
        raise HTTPException(
            422,
            detail={"code": "quantity_invalid", "message": "quantity must be a non-empty string."},
        )
    if not math.isfinite(payload.published_value) or payload.published_value == 0:
        raise HTTPException(
            422,
            detail={"code": "published_value_invalid", "message": "published_value must be a finite non-zero number."},
        )
    if not payload.published_unit or not payload.published_unit.strip():
        raise HTTPException(
            422,
            detail={"code": "published_unit_invalid", "message": "published_unit must be a non-empty string."},
        )
    if not math.isfinite(payload.rel_tol) or payload.rel_tol <= 0.0 or payload.rel_tol > 1.0:
        raise HTTPException(
            422,
            detail={"code": "rel_tol_invalid", "message": "rel_tol must satisfy 0 < rel_tol <= 1.0."},
        )

    if payload.run_id.strip().startswith("process:"):
        code, output = resolve_process_run_reference(
            workspace_id, payload.run_id.strip(), payload.quantity.strip()
        )
        if code == "run_not_found":
            raise HTTPException(
                404,
                detail={"code": "run_not_found", "message": f"Run '{payload.run_id}' not found in workspace '{workspace_id}'."},
            )
        if code == "run_not_usable":
            raise HTTPException(
                422,
                detail={"code": "run_not_usable", "message": f"Run '{payload.run_id}' is not usable."},
            )
        if code == "run_lacks_quantity":
            raise HTTPException(
                422,
                detail={"code": "run_lacks_quantity", "message": f"Run '{payload.run_id}' lacks quantity '{payload.quantity}'."},
            )
    else:
        try:
            bench_data = bench_runs_view.list_runs(workspace_id)
        except bench_runs_view.BenchViewError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code}) from exc

        runs = bench_data.get("runs", [])
        matching_run = next((r for r in runs if r.get("run_id") == payload.run_id.strip()), None)
        if matching_run is None:
            raise HTTPException(
                404,
                detail={"code": "run_not_found", "message": f"Run '{payload.run_id}' not found in workspace '{workspace_id}'."},
            )
        if matching_run.get("verdict") not in ("usable", "usable_with_warnings"):
            raise HTTPException(
                422,
                detail={
                    "code": "run_not_usable",
                    "message": f"Run '{payload.run_id}' verdict is '{matching_run.get('verdict')}', must be usable.",
                },
            )
        root = bench_runs_view.bench_root(workspace_id)
        output = _run_output(root, payload.run_id.strip(), payload.quantity.strip())
    if output is None:
        raise HTTPException(
            422,
            detail={
                "code": "run_lacks_quantity",
                "message": f"Run '{payload.run_id}' lacks quantity '{payload.quantity}'.",
            },
        )

    computed_value, computed_unit = output
    converted = _convert(computed_value, computed_unit, payload.published_unit.strip())
    if converted is None:
        raise HTTPException(
            422,
            detail={
                "code": "unit_mismatch",
                "message": f"Unit '{computed_unit}' cannot be converted to '{payload.published_unit}'.",
            },
        )

    deviation = abs(converted - payload.published_value) / abs(payload.published_value)
    verdict: Literal["pass", "fail"] = "pass" if deviation <= payload.rel_tol else "fail"

    verdict_id = f"cv_{uuid4().hex[:16]}"
    now = utc_now()
    status: Literal["recorded"] = "recorded"
    rationale = payload.rationale.strip() if payload.rationale and payload.rationale.strip() else None

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO case_verdicts (
                id, workspace_id, case_id, run_id, quantity, computed_value, computed_unit,
                published_value, published_unit, rel_tol, deviation, verdict, status,
                rationale, accepted_by, accepted_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                verdict_id,
                workspace_id,
                payload.case_id.strip(),
                payload.run_id.strip(),
                payload.quantity.strip(),
                computed_value,
                computed_unit,
                payload.published_value,
                payload.published_unit.strip(),
                payload.rel_tol,
                deviation,
                verdict,
                status,
                rationale,
                now,
                now,
            ),
        )
        connection.commit()

    return CaseVerdictRead(
        id=verdict_id,
        workspace_id=workspace_id,
        case_id=payload.case_id.strip(),
        run_id=payload.run_id.strip(),
        quantity=payload.quantity.strip(),
        computed_value=computed_value,
        computed_unit=computed_unit,
        published_value=payload.published_value,
        published_unit=payload.published_unit.strip(),
        rel_tol=payload.rel_tol,
        deviation=deviation,
        verdict=verdict,
        status=status,
        rationale=rationale,
        accepted_by=None,
        accepted_at=None,
        created_at=now,
        updated_at=now,
    )


def update_verdict(
    workspace_id: str, verdict_id: str, payload: UpdateCaseVerdictRequest
) -> CaseVerdictRead:
    """Updates status and/or rationale for a workspace case verdict (404 across workspaces)."""
    _require_workspace(workspace_id)

    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT * FROM case_verdicts WHERE workspace_id = ? AND id = ?",
            (workspace_id, verdict_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                404,
                detail={"code": "verdict_not_found", "message": f"Verdict '{verdict_id}' not found in workspace '{workspace_id}'."},
            )

        new_status = row["status"]
        accepted_at = row["accepted_at"]
        accepted_by = row["accepted_by"]
        rationale = row["rationale"]

        if payload.status is not None:
            if payload.status not in ("recorded", "accepted", "superseded"):
                raise HTTPException(
                    422,
                    detail={"code": "status_invalid", "message": f"Status '{payload.status}' is invalid."},
                )
            if payload.status == "accepted" and row["status"] != "accepted":
                accepted_at = utc_now()
                accepted_by = "operator"
            elif payload.status == "accepted" and not accepted_at:
                accepted_at = utc_now()
                accepted_by = "operator"
            new_status = payload.status

        if payload.rationale is not None:
            cleaned = payload.rationale.strip()
            rationale = cleaned if cleaned else None

        now = utc_now()
        connection.execute(
            """
            UPDATE case_verdicts
            SET status = ?, rationale = ?, accepted_by = ?, accepted_at = ?, updated_at = ?
            WHERE workspace_id = ? AND id = ?
            """,
            (new_status, rationale, accepted_by, accepted_at, now, workspace_id, verdict_id),
        )
        connection.commit()

        updated_row = connection.execute(
            "SELECT * FROM case_verdicts WHERE workspace_id = ? AND id = ?",
            (workspace_id, verdict_id),
        ).fetchone()

    return CaseVerdictRead(
        id=str(updated_row["id"]),
        workspace_id=str(updated_row["workspace_id"]),
        case_id=str(updated_row["case_id"]),
        run_id=str(updated_row["run_id"]),
        quantity=str(updated_row["quantity"]),
        computed_value=float(updated_row["computed_value"]),
        computed_unit=str(updated_row["computed_unit"]),
        published_value=float(updated_row["published_value"]),
        published_unit=str(updated_row["published_unit"]),
        rel_tol=float(updated_row["rel_tol"]),
        deviation=float(updated_row["deviation"]),
        verdict=updated_row["verdict"],
        status=updated_row["status"],
        rationale=updated_row["rationale"],
        accepted_by=updated_row["accepted_by"],
        accepted_at=updated_row["accepted_at"],
        created_at=str(updated_row["created_at"]),
        updated_at=str(updated_row["updated_at"]),
    )
