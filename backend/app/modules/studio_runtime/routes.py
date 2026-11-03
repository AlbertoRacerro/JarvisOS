"""Explicit operator execution surface for the isolated Studio runtime."""

from fastapi import APIRouter, Depends

from app.modules.runner.origin import require_runner_origin
from app.modules.studio_runtime.runtime import RunRecord, RunRequest, run, status

router = APIRouter(prefix="/studio/runtime", tags=["studio-runtime"])


@router.get("/status")
def runtime_status() -> dict:
    return status()


@router.post("/runs", response_model=RunRecord, dependencies=[Depends(require_runner_origin)])
def execute_explicit_run(request: RunRequest) -> RunRecord:
    return run(request)
