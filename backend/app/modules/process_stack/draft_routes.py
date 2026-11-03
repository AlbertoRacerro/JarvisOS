"""HTTP routes for the Jarvis-owned process draft (spec 155)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.modules.process_stack import draft, solve_jobs
from app.modules.process_stack.draft_models import (
    CreateDraft,
    PatchRequest,
    ProposalDecision,
    ProposalRequest,
    RestoreRequest,
    SolveRequest,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/process/drafts", tags=["process-draft"])


def _error(exc: draft.DraftError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.detail})


@router.get("/registry")
def registry(workspace_id: str) -> dict[str, Any]:
    try:
        draft.drafts_root(workspace_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc
    return draft.registry_projection(workspace_id)


@router.get("")
def drafts(workspace_id: str) -> list[dict[str, Any]]:
    try:
        return draft.list_drafts(workspace_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("")
def create(workspace_id: str, payload: CreateDraft) -> dict[str, Any]:
    try:
        return draft.create_draft(workspace_id, payload.name)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}")
def get(workspace_id: str, draft_id: str) -> dict[str, Any]:
    try:
        return draft.projection(workspace_id, draft_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/patch")
def patch(workspace_id: str, draft_id: str, payload: PatchRequest) -> dict[str, Any]:
    try:
        return draft.patch(workspace_id, draft_id, payload.expected_revision, list(payload.ops))
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/revisions")
def revisions(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    try:
        return draft.list_revisions(workspace_id, draft_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/restore")
def restore(workspace_id: str, draft_id: str, payload: RestoreRequest) -> dict[str, Any]:
    try:
        return draft.restore(workspace_id, draft_id, payload.expected_revision, payload.source_revision)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/revisions/{revision}/{action}")
async def execute(workspace_id: str, draft_id: str, revision: str, action: str) -> dict[str, Any]:
    try:
        return await run_in_threadpool(draft.execute, workspace_id, draft_id, revision, action)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/runs")
def runs(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    try:
        return draft.run_summaries(workspace_id, draft_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/runs/{run_id}")
def run(workspace_id: str, draft_id: str, run_id: str) -> dict[str, Any]:
    try:
        return draft.get_run(workspace_id, draft_id, run_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/runs/{run_id}/results")
def run_results(workspace_id: str, draft_id: str, run_id: str) -> dict[str, Any]:
    try:
        return draft.run_results(workspace_id, draft_id, run_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/solves")
def solves(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    try:
        return solve_jobs.list_solves(workspace_id, draft_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/solves", status_code=202)
async def start_solve(workspace_id: str, draft_id: str, payload: SolveRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(solve_jobs.start, workspace_id, draft_id, payload.revision, payload.retry_of)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/solves/{run_id}")
def solve_status(workspace_id: str, draft_id: str, run_id: str, after: int = Query(default=0, ge=0)) -> dict[str, Any]:
    try:
        return solve_jobs.status(workspace_id, draft_id, run_id, after)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/solves/{run_id}/stop")
def stop_solve(workspace_id: str, draft_id: str, run_id: str) -> dict[str, Any]:
    try:
        return solve_jobs.stop(workspace_id, draft_id, run_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/compare")
def compare_runs(workspace_id: str, draft_id: str, a: str, b: str) -> dict[str, Any]:
    try:
        return solve_jobs.compare(workspace_id, draft_id, a, b)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.get("/{draft_id}/proposals")
def proposals(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    try:
        return draft.list_proposals(workspace_id, draft_id)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/proposals")
def propose(workspace_id: str, draft_id: str, payload: ProposalRequest) -> dict[str, Any]:
    try:
        return draft.create_proposal(workspace_id, draft_id, payload)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/proposals/{proposal_id}/approve")
def approve(workspace_id: str, draft_id: str, proposal_id: str, payload: ProposalDecision) -> dict[str, Any]:
    try:
        return draft.decide_proposal(workspace_id, draft_id, proposal_id, approve=True,
                                     accepted_changes=payload.accepted_changes)
    except draft.DraftError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/proposals/{proposal_id}/reject")
def reject(workspace_id: str, draft_id: str, proposal_id: str) -> dict[str, Any]:
    try:
        return draft.decide_proposal(workspace_id, draft_id, proposal_id, approve=False)
    except draft.DraftError as exc:
        raise _error(exc) from exc
