from fastapi import APIRouter

from app.core.config import get_settings
from app.core.paths import build_paths

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict[str, str]:
    settings = get_settings()
    paths = build_paths(settings)

    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "data_root": str(paths.data_root),
    }
