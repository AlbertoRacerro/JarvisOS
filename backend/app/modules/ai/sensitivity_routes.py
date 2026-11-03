from fastapi import APIRouter, HTTPException, status

from app.modules.ai.context_builder import ContextSelectionSpec
from app.modules.ai.sensitivity import (
    SensitivityNotFoundError,
    SensitivityPolicyError,
    approve_sanitized_derivative,
    build_external_context_preview,
    create_sanitized_derivative,
    create_sensitivity_label,
    get_current_sensitivity_label,
    get_sanitized_derivative,
    preview_manual_context,
    revalidate_sanitized_derivative,
    revoke_sanitized_derivative,
)
from app.modules.ai.sensitivity_models import (
    ManualContextPreviewRequest,
    SanitizedDerivativeApprove,
    SanitizedDerivativeCreate,
    SanitizedDerivativeRead,
    SensitivityContextPreviewRequest,
    SensitivityContextPreviewResponse,
    SensitivityLabelCreate,
    SensitivityLabelRead,
)

router = APIRouter(prefix="/ai/sensitivity", tags=["ai-sensitivity"])


def _preview_value_error(exc: ValueError) -> HTTPException:
    if str(exc) == "Workspace not found.":
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post(
    "/labels",
    response_model=SensitivityLabelRead,
    status_code=status.HTTP_201_CREATED,
)
def create_label(payload: SensitivityLabelCreate) -> SensitivityLabelRead:
    try:
        return create_sensitivity_label(payload)
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/labels/current", response_model=SensitivityLabelRead | None)
def read_current_label(
    workspace_id: str,
    subject_ref: str,
) -> SensitivityLabelRead | None:
    try:
        return get_current_sensitivity_label(workspace_id, subject_ref)
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/derivatives",
    response_model=SanitizedDerivativeRead,
    status_code=status.HTTP_201_CREATED,
)
def create_derivative(
    payload: SanitizedDerivativeCreate,
) -> SanitizedDerivativeRead:
    try:
        return create_sanitized_derivative(payload)
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/derivatives/{derivative_id}",
    response_model=SanitizedDerivativeRead,
)
def read_derivative(
    derivative_id: str,
    workspace_id: str,
) -> SanitizedDerivativeRead:
    try:
        return get_sanitized_derivative(
            workspace_id,
            derivative_id,
            refresh=False,
        )
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/derivatives/{derivative_id}/revalidate",
    response_model=SanitizedDerivativeRead,
)
def revalidate_derivative(
    derivative_id: str,
    workspace_id: str,
) -> SanitizedDerivativeRead:
    try:
        return revalidate_sanitized_derivative(
            workspace_id,
            derivative_id,
        )
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/derivatives/{derivative_id}/approve",
    response_model=SanitizedDerivativeRead,
)
def approve_derivative(
    derivative_id: str,
    payload: SanitizedDerivativeApprove,
    workspace_id: str,
) -> SanitizedDerivativeRead:
    try:
        return approve_sanitized_derivative(
            workspace_id,
            derivative_id,
            reviewer_notes=payload.reviewer_notes,
        )
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/derivatives/{derivative_id}/revoke",
    response_model=SanitizedDerivativeRead,
)
def revoke_derivative(
    derivative_id: str,
    workspace_id: str,
) -> SanitizedDerivativeRead:
    try:
        return revoke_sanitized_derivative(
            workspace_id,
            derivative_id,
        )
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/context-preview",
    response_model=SensitivityContextPreviewResponse,
)
def external_context_preview(
    payload: SensitivityContextPreviewRequest,
) -> SensitivityContextPreviewResponse:
    selection = ContextSelectionSpec(
        **payload.selection.model_dump()
    )
    try:
        return build_external_context_preview(
            payload.workspace_id,
            payload.budget_chars,
            selection,
        )
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise _preview_value_error(exc) from exc


@router.post(
    "/manual-context-preview",
    response_model=SensitivityContextPreviewResponse,
)
def manual_context_preview(
    payload: ManualContextPreviewRequest,
) -> SensitivityContextPreviewResponse:
    try:
        return preview_manual_context(
            payload.workspace_id,
            payload.context_blocks,
            payload.budget_chars,
        )
    except SensitivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SensitivityPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise _preview_value_error(exc) from exc
