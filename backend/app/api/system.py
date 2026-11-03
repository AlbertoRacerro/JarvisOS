from fastapi import APIRouter

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import get_database_info
from app.core.packs import default_workspace_slug, load_packs
from app.core.paths import build_paths
from app.modules.ai.gateway import AIGateway
from app.modules.ai.settings import ensure_ai_settings

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/info")
def system_info() -> dict[str, object]:
    settings = get_settings()
    paths = build_paths(settings)
    database = get_database_info()
    ai_status = AIGateway().status() if database.initialized else None

    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "data_root": str(paths.data_root),
        "data_root_exists": paths.data_root_exists,
        "paths": paths.as_strings(),
        "database": {
            "engine": database.engine,
            "database_file": database.database_file,
            "configured": database.configured,
            "ready": database.ready,
            "initialized": database.initialized,
            "bootstrap_required": database.bootstrap_required,
            "bootstrap_action": database.bootstrap_action,
            "schema": {
                "current_migration_id": database.schema_current.migration_id,
                "current_migration_name": database.schema_current.name,
                "current_migration_status": database.schema_current.status,
                "current_migration_applied_at": database.schema_current.applied_at,
                "applied_migration_count": database.applied_migration_count,
            },
        },
        "ai": {
            "provider": ai_status.default_ai_provider if ai_status else settings.ai_provider,
            "gateway_configured": True,
            "provider_configured": bool(
                ai_status
                and (
                    ai_status.active_provider_mode == "fake"
                    or ai_status.scaleway_api_key_configured
                )
            ),
            "provider_calls_enabled": bool(ai_status and ai_status.external_calls_allowed),
            "provider_mode": ai_status.active_provider_mode if ai_status else "not_initialized",
            "monthly_budget_usd": ai_status.monthly_api_budget_usd if ai_status else 0,
            "spend_month_to_date_usd": ai_status.spend_month_to_date_usd if ai_status else 0,
            "scaleway_enabled": ai_status.scaleway_enabled if ai_status else False,
            "scaleway_api_key_configured": ai_status.scaleway_api_key_configured if ai_status else False,
            "scaleway_provider_implementation": ai_status.scaleway_provider_implementation if ai_status else "not_initialized",
            "scaleway_smoke_test_enabled": ai_status.scaleway_smoke_test_enabled if ai_status else False,
            "scaleway_live_smoke_test_enabled": ai_status.scaleway_live_smoke_test_enabled if ai_status else False,
            "scaleway_monthly_token_cap": ai_status.scaleway_monthly_token_cap if ai_status else 0,
            "scaleway_hard_stop_token_cap": ai_status.scaleway_hard_stop_token_cap if ai_status else 0,
            "scaleway_free_tier_reference_tokens": ai_status.scaleway_free_tier_reference_tokens if ai_status else 0,
            "scaleway_input_tokens_month_to_date": ai_status.scaleway_input_tokens_month_to_date if ai_status else 0,
            "scaleway_output_tokens_month_to_date": ai_status.scaleway_output_tokens_month_to_date if ai_status else 0,
            "blocking_reason": ai_status.blocking_reason if ai_status else "database_not_initialized",
        },
    }


@router.post("/initialize")
def initialize_system() -> dict[str, object]:
    database = initialize_storage(seed_default=True)
    ai_settings = ensure_ai_settings()
    return {
        "status": "ok",
        "database": {
            "engine": database.engine,
            "database_file": database.database_file,
            "ready": database.ready,
            "initialized": database.initialized,
            "bootstrap_required": database.bootstrap_required,
            "bootstrap_action": database.bootstrap_action,
            "schema": {
                "current_migration_id": database.schema_current.migration_id,
                "current_migration_name": database.schema_current.name,
                "current_migration_status": database.schema_current.status,
                "current_migration_applied_at": database.schema_current.applied_at,
                "applied_migration_count": database.applied_migration_count,
            },
        },
        "default_workspace": default_workspace_slug(),
        "packs": [{"id": pack.id, "ui_labels": pack.ui_labels} for pack in load_packs()],
        "ai_settings": {
            "monthly_api_budget_usd": ai_settings.monthly_api_budget_usd,
            "provider_mode": ai_settings.provider_mode,
        },
    }
