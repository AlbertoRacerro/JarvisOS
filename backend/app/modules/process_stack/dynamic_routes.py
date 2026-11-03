"""Headless API for workspace-scoped Process dynamic runs (spec 172)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.modules.process_stack import dynamic_jobs

router = APIRouter(prefix="/workspaces/{workspace_id}/process/drafts/{draft_id}/dynamic",
                    tags=["process-dynamic"])


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(min_length=1, max_length=128)


def _error(exc: dynamic_jobs.DynamicJobError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.detail})


@router.post("/runs", status_code=202)
async def start(workspace_id: str, draft_id: str, payload: StartRun) -> dict[str, Any]:
    try:
        return await run_in_threadpool(dynamic_jobs.start, workspace_id, draft_id, payload.scenario_id)
    except dynamic_jobs.DynamicJobError as exc:
        raise _error(exc) from exc


@router.get("/runs")
def list_runs(workspace_id: str, draft_id: str, limit: int = Query(default=50, ge=1)) -> list[dict[str, Any]]:
    try:
        return dynamic_jobs.list_jobs(workspace_id, draft_id, limit)
    except dynamic_jobs.DynamicJobError as exc:
        raise _error(exc) from exc


@router.get("/runs/{job_id}")
def get_run(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    try:
        return dynamic_jobs.status(workspace_id, draft_id, job_id)
    except dynamic_jobs.DynamicJobError as exc:
        raise _error(exc) from exc


@router.post("/runs/{job_id}/cancel")
def cancel(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    try:
        return dynamic_jobs.cancel(workspace_id, draft_id, job_id)
    except dynamic_jobs.DynamicJobError as exc:
        raise _error(exc) from exc


@router.get("/runs/{job_id}/manifest")
def manifest(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    try:
        return dynamic_jobs.read_manifest(workspace_id, draft_id, job_id)
    except dynamic_jobs.DynamicJobError as exc:
        raise _error(exc) from exc


@router.get("/runs/{job_id}/series")
def series(workspace_id: str, draft_id: str, job_id: str,
           channels: str | None = None, start_s: float | None = None, end_s: float | None = None,
           max_points: int = Query(default=dynamic_jobs.DEFAULT_SERIES_POINTS, ge=1)) -> dict[str, Any]:
    try:
        return dynamic_jobs.read_series(workspace_id, draft_id, job_id,
                                        channels=channels.split(",") if channels else None,
                                        start_s=start_s, end_s=end_s, max_points=max_points)
    except dynamic_jobs.DynamicJobError as exc:
        raise _error(exc) from exc
