import sqlite3
from typing import Literal

from app.core.database import open_sqlite_connection
from app.modules.ai.contracts import AIPolicyMode
from app.modules.ai.egress_persistence import project_egress_availability
from app.modules.ai.models import (
    AISettingsRead,
    AISettingsUpdate,
    AIStatusRead,
    ProviderCredentialCapabilities,
    ProviderCredentialStatus,
    ProviderSettingsProvider,
    ProviderSettingsRead,
)
from app.modules.ai.provider_registry import ProviderConfig, ProviderRegistry, load_default_provider_registry
from app.modules.events.service import utc_now
from app.modules.secrets.service import (
    read_scaleway_secret_mutation_capabilities,
    read_scaleway_secret_status,
    resolve_provider_secret_ref,
)

SETTINGS_ID = "default"


def _to_bool(value: int | bool) -> bool:
    return bool(value)


# Fail closed: unclassified prompt text is never external-eligible unless the operator opts into FAST_DEV.
DEFAULT_POLICY_MODE = AIPolicyMode.STRICT_IP


def _policy_mode(value: object) -> AIPolicyMode:
    try:
        return AIPolicyMode(str(value or DEFAULT_POLICY_MODE.value))
    except ValueError:
        return DEFAULT_POLICY_MODE


def _policy_mode_value(value: object) -> str:
    if isinstance(value, AIPolicyMode):
        return value.value
    return _policy_mode(value).value


def _row_to_settings(row: sqlite3.Row) -> AISettingsRead:
    import json
    import os

    from app.core.packs import hermes_role_pack_defaults
    from app.modules.agents.hermes.roles import ROLES

    data = dict(row)
    input_tokens = int(data["scaleway_input_tokens_month_to_date"])
    output_tokens = int(data["scaleway_output_tokens_month_to_date"])
    raw_routes = data.get("hermes_role_routes_json")
    stored_routes: dict[str, str] = {}
    if raw_routes:
        try:
            decoded = json.loads(raw_routes) if isinstance(raw_routes, str) else dict(raw_routes)
            if isinstance(decoded, dict):
                stored_routes = {key: value for key, value in decoded.items()
                                 if isinstance(key, str) and isinstance(value, str)}
        except Exception:
            stored_routes = {}
    pack_defaults = hermes_role_pack_defaults()
    env_default = os.getenv("JARVIS_HERMES_INFERENCE_ROUTE", "local:llamacpp")
    effective_routes: dict[str, str] = {}
    for role_id in ROLES:
        effective_routes[role_id] = (
            stored_routes.get(role_id)
            or pack_defaults.get(role_id)
            or env_default
        )
    return AISettingsRead(
        policy_mode=_policy_mode(data.get("policy_mode")),
        monthly_api_budget_usd=float(data["monthly_api_budget_usd"]),
        api_spend_month_to_date_usd=float(data["api_spend_month_to_date_usd"]),
        paid_ai_enabled=_to_bool(data["paid_ai_enabled"]),
        default_ai_provider=data["default_ai_provider"],
        default_ai_model=data["default_ai_model"],
        provider_mode=data["provider_mode"],
        use_fake_provider_when_budget_zero=_to_bool(data["use_fake_provider_when_budget_zero"]),
        scaleway_enabled=_to_bool(data["scaleway_enabled"]),
        scaleway_smoke_test_enabled=_to_bool(data["scaleway_smoke_test_enabled"]),
        scaleway_live_smoke_test_enabled=_to_bool(data.get("scaleway_live_smoke_test_enabled", 0)),
        scaleway_monthly_token_cap=int(data["scaleway_monthly_token_cap"]),
        scaleway_hard_stop_token_cap=int(data["scaleway_hard_stop_token_cap"]),
        scaleway_free_tier_reference_tokens=int(data["scaleway_free_tier_reference_tokens"]),
        scaleway_input_tokens_month_to_date=input_tokens,
        scaleway_output_tokens_month_to_date=output_tokens,
        usage_total_tokens=input_tokens + output_tokens,
        smoke_test_mode_enabled=_to_bool(data["smoke_test_mode_enabled"]),
        max_direct_continuations=int(data["max_direct_continuations"]),
        direct_continuation_policy_version=str(data["direct_continuation_policy_version"]),
        hermes_role_routes=effective_routes,
        updated_at=data["updated_at"],
    )


def ensure_ai_settings() -> AISettingsRead:
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO ai_settings (
                id, policy_mode, monthly_api_budget_usd, api_spend_month_to_date_usd,
                paid_ai_enabled, default_ai_provider, default_ai_model,
                provider_mode, use_fake_provider_when_budget_zero,
                scaleway_enabled, scaleway_token_cap,
                scaleway_tokens_month_to_date, scaleway_smoke_test_enabled,
                scaleway_live_smoke_test_enabled,
                scaleway_monthly_token_cap, scaleway_hard_stop_token_cap,
                scaleway_free_tier_reference_tokens, scaleway_input_tokens_month_to_date,
                scaleway_output_tokens_month_to_date, smoke_test_mode_enabled,
                hermes_role_routes_json, updated_at
            ) VALUES (?, ?, 0, 0, 0, 'fake', 'fake-modeling-draft-v1', 'fake', 1, 0, 0, 0, 0, 0, 500000, 800000, 1000000, 0, 0, 0, '{}', ?)
            """,
            (SETTINGS_ID, DEFAULT_POLICY_MODE.value, now),
        )
        connection.commit()
    return get_ai_settings()


def get_ai_settings() -> AISettingsRead:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM ai_settings WHERE id = ?", (SETTINGS_ID,)).fetchone()
    if row is None:
        return ensure_ai_settings()
    return _row_to_settings(row)


def update_ai_settings(payload: AISettingsUpdate) -> AISettingsRead:
    import json

    # AISettingsUpdate.validate_hermes_role_routes already admitted only known roles on local-compute routes.
    current = get_ai_settings()
    values = current.model_dump()
    updates = payload.model_dump(exclude_unset=True)
    values.update({key: value for key, value in updates.items() if value is not None})

    now = utc_now()
    with open_sqlite_connection() as connection:
        raw_current = {}
        row = connection.execute("SELECT hermes_role_routes_json FROM ai_settings WHERE id = ?", (SETTINGS_ID,)).fetchone()
        if row and row["hermes_role_routes_json"]:
            try:
                raw_current = json.loads(row["hermes_role_routes_json"])
            except Exception:
                raw_current = {}
        if payload.hermes_role_routes is not None:
            raw_current.update(payload.hermes_role_routes)
        routes_json = json.dumps(raw_current)

        connection.execute(
            """
            UPDATE ai_settings
            SET policy_mode = ?, monthly_api_budget_usd = ?, paid_ai_enabled = ?,
                default_ai_provider = ?, default_ai_model = ?, provider_mode = ?,
                use_fake_provider_when_budget_zero = ?, scaleway_enabled = ?,
                scaleway_smoke_test_enabled = ?, scaleway_live_smoke_test_enabled = ?,
                scaleway_monthly_token_cap = ?, scaleway_hard_stop_token_cap = ?,
                scaleway_input_tokens_month_to_date = ?,
                scaleway_output_tokens_month_to_date = ?,
                smoke_test_mode_enabled = ?, max_direct_continuations = ?,
                hermes_role_routes_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                _policy_mode_value(values["policy_mode"]),
                float(values["monthly_api_budget_usd"]),
                int(bool(values["paid_ai_enabled"])),
                str(values["default_ai_provider"]),
                str(values["default_ai_model"]),
                str(values["provider_mode"]),
                int(bool(values["use_fake_provider_when_budget_zero"])),
                int(bool(values["scaleway_enabled"])),
                int(bool(values["scaleway_smoke_test_enabled"])),
                int(bool(values["scaleway_live_smoke_test_enabled"])),
                int(values["scaleway_monthly_token_cap"]),
                int(values["scaleway_hard_stop_token_cap"]),
                int(values["scaleway_input_tokens_month_to_date"]),
                int(values["scaleway_output_tokens_month_to_date"]),
                int(bool(values["smoke_test_mode_enabled"])),
                int(values["max_direct_continuations"]),
                routes_json,
                now,
                SETTINGS_ID,
            ),
        )
        connection.commit()
    return get_ai_settings()


def resolve_role_route(role_id: str) -> str:
    """Resolve the effective local route for a Hermes role.

    Resolution order:
    1. Explicit stored setting in ai_settings
    2. Loaded pack default ([hermes_roles] in pack manifest)
    3. JARVIS_HERMES_INFERENCE_ROUTE env var (default 'local:llamacpp')
    """
    import os

    from app.core.packs import hermes_role_pack_defaults
    from app.modules.agents.hermes.roles import get_role

    role = get_role(role_id)
    try:
        return get_ai_settings().hermes_role_routes[role.role_id]
    except sqlite3.OperationalError as exc:
        if "no such table: ai_settings" not in str(exc):
            raise
        return (hermes_role_pack_defaults().get(role.role_id)
                or os.getenv("JARVIS_HERMES_INFERENCE_ROUTE", "local:llamacpp"))


def record_scaleway_token_usage(*, input_tokens: int, output_tokens: int) -> AISettingsRead:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE ai_settings
            SET scaleway_input_tokens_month_to_date = scaleway_input_tokens_month_to_date + ?,
                scaleway_output_tokens_month_to_date = scaleway_output_tokens_month_to_date + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (input_tokens, output_tokens, utc_now(), SETTINGS_ID),
        )
        connection.commit()
    return get_ai_settings()


def project_canonical_ai_status(status: AIStatusRead) -> AIStatusRead:
    """Overlay canonical owner-derived egress/accounting truth on status."""
    registry = load_default_provider_registry()
    provider_id = status.provider_id
    provider = registry.providers.get(provider_id)
    projection = project_egress_availability(provider_id, registry=registry)
    update: dict[str, object] = {
        "spend_month_to_date_usd": projection.global_actual_cost_usd,
        "budget_status": (
            "monthly_budget_exhausted"
            if projection.budget_exhausted
            else "within_budget"
        ),
    }
    if provider is None or provider.requires_network:
        update.update(
            external_calls_allowed=projection.available,
            blocking_reason=projection.blocking_reason,
        )
    return status.model_copy(update=update)


def get_provider_settings(status: AIStatusRead) -> ProviderSettingsRead:
    """Project canonical provider/settings truth without exposing credential material."""
    status = project_canonical_ai_status(status)
    registry = load_default_provider_registry()
    settings = get_ai_settings()
    providers = [
        _provider_settings_entry(provider, registry=registry)
        for provider in sorted(registry.providers.values(), key=lambda item: item.provider_id)
    ]
    return ProviderSettingsRead(
        providers=providers,
        default_provider_id=settings.default_ai_provider,
        policy_mode=settings.policy_mode,
        external_calls_allowed=status.external_calls_allowed,
        blocking_reason=status.blocking_reason,
        monthly_api_budget_usd=settings.monthly_api_budget_usd,
        spend_month_to_date_usd=status.spend_month_to_date_usd,
    )


def _provider_settings_entry(
    provider: ProviderConfig,
    *,
    registry: ProviderRegistry,
) -> ProviderSettingsProvider:
    credential, capabilities = _credential_projection(provider)
    projection = project_egress_availability(provider.provider_id, registry=registry)
    return ProviderSettingsProvider(
        provider_id=provider.provider_id,
        kind=provider.kind,
        enabled=provider.enabled,
        requires_network=provider.requires_network,
        execution_class=str(provider.execution_class),
        monthly_token_cap=provider.monthly_token_cap,
        monthly_cost_cap_usd=provider.monthly_cost_cap_usd,
        external_calls_allowed=projection.available,
        blocking_reason=projection.blocking_reason,
        credential=credential,
        credential_capabilities=capabilities,
    )


def _credential_projection(
    provider: ProviderConfig,
) -> tuple[ProviderCredentialStatus, ProviderCredentialCapabilities]:
    if provider.api_key_ref is None:
        return (
            ProviderCredentialStatus(
                key_present=False,
                effective_source="not_required",
                persisted_state="not_supported",
                reason_code="credential_not_required",
            ),
            ProviderCredentialCapabilities(),
        )

    if provider.provider_id == "scaleway":
        owner_status = read_scaleway_secret_status()
        owner_capabilities = read_scaleway_secret_mutation_capabilities()
        return (
            ProviderCredentialStatus(
                key_present=owner_status.key_present,
                effective_source=_scaleway_effective_source(owner_status),
                persisted_state=owner_status.persisted_state,
                reason_code=owner_status.reason_code,
            ),
            ProviderCredentialCapabilities(
                replace_persisted=owner_capabilities.replace_persisted,
                delete_persisted=owner_capabilities.delete_persisted,
            ),
        )

    try:
        owner_secret = resolve_provider_secret_ref(provider.api_key_ref)
    except ValueError:
        return (
            ProviderCredentialStatus(
                key_present=False,
                effective_source="unknown",
                persisted_state="not_supported",
                reason_code="credential_reference_unsupported",
            ),
            ProviderCredentialCapabilities(),
        )

    return (
        ProviderCredentialStatus(
            key_present=owner_secret.key_present,
            effective_source="environment" if owner_secret.key_present else "absent",
            persisted_state="not_supported",
            reason_code=None if owner_secret.key_present else "credential_environment_missing",
        ),
        ProviderCredentialCapabilities(),
    )


def _scaleway_effective_source(owner_status: object) -> Literal[
    "environment", "secure_persisted", "absent", "invalid", "unknown"
]:
    effective_source = getattr(owner_status, "effective_source", "none")
    if effective_source == "environment":
        return "environment"
    if effective_source == "secure_persisted":
        return "secure_persisted"
    if getattr(owner_status, "reason_code", None) == "secret_environment_invalid":
        return "invalid"
    if getattr(owner_status, "persisted_state", None) == "unavailable":
        return "unknown"
    return "absent"
