from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from . import service
from .models import CardCreate, EvaluationInput, ReviewAction, SetCreate, ValueAction, ValueEdit

router = APIRouter(prefix="/workspaces/{workspace_id}/bio-models", tags=["bio-models"])


def _error(exc: service.BioModelError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc), **exc.detail})


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except service.BioModelError as exc:
        raise _error(exc) from exc


@router.get("/forms")
def get_forms() -> list[dict[str, Any]]:
    return service.list_forms()


@router.get("/sets")
def get_sets(workspace_id: str) -> list[dict[str, Any]]:
    return _call(service.list_sets, workspace_id)


@router.post("/sets", status_code=201)
def post_set(workspace_id: str, payload: SetCreate) -> dict[str, Any]:
    return _call(service.create_set, workspace_id, payload.name, payload.species, payload.strain)


@router.get("/sets/{set_id}")
def get_set(workspace_id: str, set_id: str) -> dict[str, Any]:
    return _call(service.get_set, workspace_id, set_id)


@router.post("/sets/{set_id}/duplicate", status_code=201)
def duplicate_set(workspace_id: str, set_id: str, payload: ValueAction) -> dict[str, Any]:
    return _call(service.duplicate_set, workspace_id, set_id, payload.expected_revision, payload.expected_digest)


@router.put("/sets/{set_id}/values/{symbol}")
def edit_value(workspace_id: str, set_id: str, symbol: str, payload: ValueEdit) -> dict[str, Any]:
    return _call(service.edit_set_value, workspace_id, set_id, symbol, payload.model_dump(exclude={"expected_revision", "expected_digest"}, exclude_unset=True),
                 payload.expected_revision, payload.expected_digest)


@router.post("/sets/{set_id}/values/{symbol}/verify")
def verify_value(workspace_id: str, set_id: str, symbol: str, payload: ValueAction) -> dict[str, Any]:
    return _call(service.verify_value, workspace_id, set_id, symbol, payload.expected_revision, payload.expected_digest,
                 locator_confirmed=payload.locator_confirmed, locator_confirmation=payload.locator_confirmation)


@router.post("/sets/{set_id}/values/{symbol}/review")
def review_value(workspace_id: str, set_id: str, symbol: str, payload: ReviewAction) -> dict[str, Any]:
    return _call(service.review_value, workspace_id, set_id, symbol, payload.reviewer, payload.note,
                 payload.expected_revision, payload.expected_digest)


@router.get("/sets/{set_id}/history")
def get_history(workspace_id: str, set_id: str) -> list[dict[str, Any]]:
    return _call(service.history, workspace_id, set_id)


@router.get("/cards")
def get_cards(workspace_id: str) -> list[dict[str, Any]]:
    return _call(service.list_cards, workspace_id)


@router.post("/cards", status_code=201)
def post_card(workspace_id: str, payload: CardCreate) -> dict[str, Any]:
    return _call(service.create_card, workspace_id, payload.name, payload.parameter_set_id, payload.factors, payload.mu_max,
                 n_source=payload.n_source)


@router.post("/cards/{card_id}/evaluate")
def evaluate_card(workspace_id: str, card_id: str, payload: EvaluationInput) -> dict[str, Any]:
    return _call(service.evaluate_card, workspace_id, card_id, payload.operating_point)
