from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/agents/hermes", tags=["agents"])


@router.get("/status")
def read_hermes_status(request: Request) -> dict[str, Any]:
    return request.app.state.hermes_supervisor.status()
