from fastapi import APIRouter, HTTPException

from app.modules.ai.jarvis_context import (
    JarvisContextConflictError,
    JarvisContextError,
    build_jarvis_context_preview,
)
from app.modules.ai.jarvis_context_models import JarvisContextPreview, JarvisContextRequest

router = APIRouter(prefix="/jarvis/context", tags=["ai"])


@router.post("/preview", response_model=JarvisContextPreview)
def preview_jarvis_context(payload: JarvisContextRequest) -> JarvisContextPreview:
    try:
        return build_jarvis_context_preview(payload)
    except JarvisContextConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except JarvisContextError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
