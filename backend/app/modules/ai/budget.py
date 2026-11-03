from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.database import open_sqlite_connection
from app.modules.ai.contracts import AIPolicyMode
from app.modules.ai.models import AISettingsRead, AIStatusRead
from app.modules.ai.providers.deepseek import DeepSeekProvider
from app.modules.secrets.storage import get_effective_scaleway_api_key, resolve_secret_ref

SCALEWAY_STUB_IMPLEMENTATION = "stub_no_external_calls"
SCALEWAY_LIVE_IMPLEMENTATION = "live_chat_completions"
DEEPSEEK_PROVIDER_MODE = "deepseek"
ALPHA_EXTERNAL_PROVIDER_CALL = "external_provider_call"


@dataclass(frozen=True)
class AlphaGateDecision:
    allowed: bool
    reason: str
    operation: str
    provider_id: str | None = None
    usage_tokens_month_to_date: int = 0
    cost_month_to_date_usd: float = 0.0


@dataclass(frozen=True)
class ProviderBudgetGate:
    allowed: bool
    blocking_reason: str | None = None
    provider_id: str | None = None
    usage_tokens_month_to_date: int = 0
    cost_month_to_date_usd: float = 0.0


@dataclass(frozen=True)
class ScalewayUsageSnapshot:
    input_tokens: int
    output_tokens: int
    cost_usd: float
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_usd: float

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.reserved_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.output_tokens + self.reserved_output_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return self.cost_usd + self.reserved_cost_usd


def evaluate_alpha_execution_gate(
    *,
    settings: AISettingsRead | None,
    provider_id: str | None,
    operation: str,
) -> AlphaGateDecision:
    """Evaluate the server-owned external-provider execution boundary.

    The execution spine decides whether a binding requires network access. It
    invokes this gate only for the concrete network binding about to execute,
    including every fallback attempt. Callers cannot mark an external binding
    as read-only or authorize it with request-payload flags.
    """
    if operation != ALPHA_EXTERNAL_PROVIDER_CALL:
        return AlphaGateDecision(
            False,
            f"alpha_gate_unsupported_operation:{operation}",
            operation,
            provider_id,
        )
    if settings is None:
        return AlphaGateDecision(
            False,
            "alpha_gate_missing_context",
            operation,
            provider_id,
        )
    if provider_id is None or not provider_id.strip():
        return AlphaGateDecision(
            False,
            "alpha_gate_missing_provider",
            operation,
            provider_id,
        )

    policy = _evaluate_external_provider_policy(settings, provider_id)
    return AlphaGateDecision(
        policy.allowed,
        "alpha_gate_open"
        if policy.allowed
        else (policy.blocking_reason or "alpha_gate_closed"),
        operation,
        provider_id,
        policy.usage_tokens_month_to_date,
        policy.cost_month_to_date_usd,
    )


def evaluate_provider_budget_gate(
    settings: AISettingsRead, provider_id: str
) -> ProviderBudgetGate:
    """Compatibility wrapper used by the execution spine and status surfaces."""
    decision = evaluate_alpha_execution_gate(
        settings=settings,
        provider_id=provider_id,
        operation=ALPHA_EXTERNAL_PROVIDER_CALL,
    )
    return ProviderBudgetGate(
        decision.allowed,
        None if decision.allowed else decision.reason,
        decision.provider_id,
        decision.usage_tokens_month_to_date,
        decision.cost_month_to_date_usd,
    )


def _evaluate_external_provider_policy(
    settings: AISettingsRead,
    provider_id: str,
    *,
    provider_mode: str | None = None,
) -> ProviderBudgetGate:
    if settings.policy_mode == AIPolicyMode.DISABLED:
        return ProviderBudgetGate(False, "ai_policy_disabled", provider_id)
    if not settings.paid_ai_enabled:
        return ProviderBudgetGate(False, "paid_ai_disabled", provider_id)
    if settings.monthly_api_budget_usd <= 0:
        return ProviderBudgetGate(False, "monthly_budget_zero", provider_id)
    if settings.api_spend_month_to_date_usd >= settings.monthly_api_budget_usd:
        return ProviderBudgetGate(False, "monthly_budget_exhausted", provider_id)

    if provider_id == "scaleway":
        switch_reason = _scaleway_switch_blocking_reason(
            settings,
            provider_mode=provider_mode or settings.provider_mode,
        )
        if switch_reason is not None:
            return ProviderBudgetGate(False, switch_reason, provider_id)

    provider = _registry_provider(provider_id)
    if provider is None or not provider.enabled:
        return ProviderBudgetGate(False, f"{provider_id}_disabled", provider_id)
    if provider.api_key_ref and not resolve_secret_ref(provider.api_key_ref).key_present:
        return ProviderBudgetGate(False, f"{provider_id}_api_key_missing", provider_id)

    if provider_id == "scaleway":
        usage = scaleway_usage_snapshot(settings)
        cap_reason = _scaleway_cap_blocking_reason(settings, usage.total_tokens)
        if cap_reason is not None:
            return ProviderBudgetGate(
                False,
                cap_reason,
                provider_id,
                usage.total_tokens,
                usage.total_cost_usd,
            )
        if (
            provider.monthly_token_cap > 0
            and usage.total_tokens >= provider.monthly_token_cap
        ):
            return ProviderBudgetGate(
                False,
                "scaleway_monthly_token_cap_exhausted",
                provider_id,
                usage.total_tokens,
                usage.total_cost_usd,
            )
        if (
            provider.monthly_cost_cap_usd > 0
            and usage.total_cost_usd >= provider.monthly_cost_cap_usd
        ):
            return ProviderBudgetGate(
                False,
                "scaleway_monthly_cost_cap_exhausted",
                provider_id,
                usage.total_tokens,
                usage.total_cost_usd,
            )
        return ProviderBudgetGate(
            True,
            None,
            provider_id,
            usage.total_tokens,
            usage.total_cost_usd,
        )

    usage_tokens, cost = provider_month_to_date_usage(provider_id)
    if provider.monthly_token_cap > 0 and usage_tokens >= provider.monthly_token_cap:
        return ProviderBudgetGate(
            False,
            f"{provider_id}_monthly_token_cap_exhausted",
            provider_id,
            usage_tokens,
            cost,
        )
    if provider.monthly_cost_cap_usd > 0 and cost >= provider.monthly_cost_cap_usd:
        return ProviderBudgetGate(
            False,
            f"{provider_id}_monthly_cost_cap_exhausted",
            provider_id,
            usage_tokens,
            cost,
        )
    return ProviderBudgetGate(True, None, provider_id, usage_tokens, cost)


def provider_month_to_date_usage(provider_id: str) -> tuple[int, float]:
    input_tokens, output_tokens, cost = _provider_month_to_date_usage_split(provider_id)
    return input_tokens + output_tokens, cost


def _provider_month_to_date_usage_split(
    provider_id: str,
) -> tuple[int, int, float]:
    month_start = datetime.now(UTC).strftime("%Y-%m-01")
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(COALESCE(input_tokens, 0)), 0) AS input_tokens,
                COALESCE(SUM(COALESCE(output_tokens, 0)), 0) AS output_tokens,
                COALESCE(SUM(COALESCE(cost_estimate, 0)), 0) AS total_cost
            FROM ai_jobs
            WHERE provider_id = ? AND created_at >= ?
            """,
            (provider_id, month_start),
        ).fetchone()
    return (
        int(row["input_tokens"]),
        int(row["output_tokens"]),
        float(row["total_cost"]),
    )


def _provider_active_reservation_usage(
    provider_id: str,
) -> tuple[int, int, float]:
    now = datetime.now(UTC)
    month_start = now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).isoformat()
    now_iso = now.isoformat()
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(projected_input_tokens), 0) AS input_tokens,
                COALESCE(SUM(projected_output_tokens), 0) AS output_tokens,
                COALESCE(SUM(projected_cost_upper_usd), 0) AS total_cost
            FROM egress_budget_reservations
            WHERE provider_id = ?
              AND created_at >= ?
              AND (
                    state = 'in_flight'
                    OR (state = 'active' AND expires_at > ?)
              )
            """,
            (provider_id, month_start, now_iso),
        ).fetchone()
    return (
        int(row["input_tokens"]),
        int(row["output_tokens"]),
        float(row["total_cost"]),
    )


def scaleway_usage_snapshot(settings: AISettingsRead) -> ScalewayUsageSnapshot:
    routed_input, routed_output, routed_cost = _provider_month_to_date_usage_split(
        "scaleway"
    )
    reserved_input, reserved_output, reserved_cost = (
        _provider_active_reservation_usage("scaleway")
    )
    return ScalewayUsageSnapshot(
        input_tokens=settings.scaleway_input_tokens_month_to_date + routed_input,
        output_tokens=settings.scaleway_output_tokens_month_to_date + routed_output,
        cost_usd=routed_cost,
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
        reserved_cost_usd=reserved_cost,
    )


def _registry_provider(provider_id: str):
    from app.modules.ai.provider_registry import load_default_provider_registry

    return load_default_provider_registry().providers.get(provider_id)


def scaleway_api_key_configured() -> bool:
    return get_effective_scaleway_api_key().key_present


def evaluate_ai_status(
    settings: AISettingsRead, provider_mode: str | None = None
) -> AIStatusRead:
    mode = provider_mode or settings.provider_mode
    key_configured = scaleway_api_key_configured()
    policy_disabled = settings.policy_mode == AIPolicyMode.DISABLED
    scaleway_usage = scaleway_usage_snapshot(settings)

    external_calls_allowed = False
    blocking_reason: str | None = None
    if policy_disabled:
        blocking_reason = "ai_policy_disabled"
    elif mode == "fake":
        blocking_reason = None
    elif mode == "scaleway":
        gate = _evaluate_external_provider_policy(
            settings,
            "scaleway",
            provider_mode=mode,
        )
        external_calls_allowed = gate.allowed
        blocking_reason = gate.blocking_reason
    elif mode == DEEPSEEK_PROVIDER_MODE:
        gate = evaluate_provider_budget_gate(settings, DEEPSEEK_PROVIDER_MODE)
        external_calls_allowed = gate.allowed
        blocking_reason = gate.blocking_reason
    elif _registry_provider(mode) is not None:
        gate = evaluate_provider_budget_gate(settings, mode)
        external_calls_allowed = gate.allowed
        blocking_reason = gate.blocking_reason
    else:
        blocking_reason = "unsupported_provider_mode"

    return AIStatusRead(
        policy_mode=settings.policy_mode,
        ai_enabled=not policy_disabled,
        active_provider_mode=mode,
        provider_mode=mode,
        provider_id=_provider_id_for_mode(mode),
        adapter_enabled=_adapter_enabled_for_mode(mode, settings),
        fake_provider_enabled=True,
        scaleway_enabled=settings.scaleway_enabled,
        scaleway_api_key_configured=key_configured,
        scaleway_provider_implementation=SCALEWAY_LIVE_IMPLEMENTATION
        if settings.scaleway_live_smoke_test_enabled
        else SCALEWAY_STUB_IMPLEMENTATION,
        scaleway_smoke_test_enabled=settings.scaleway_smoke_test_enabled,
        scaleway_live_smoke_test_enabled=settings.scaleway_live_smoke_test_enabled,
        paid_ai_enabled=settings.paid_ai_enabled,
        monthly_api_budget_usd=settings.monthly_api_budget_usd,
        spend_month_to_date_usd=settings.api_spend_month_to_date_usd,
        scaleway_monthly_token_cap=settings.scaleway_monthly_token_cap,
        scaleway_hard_stop_token_cap=settings.scaleway_hard_stop_token_cap,
        scaleway_free_tier_reference_tokens=(
            settings.scaleway_free_tier_reference_tokens
        ),
        scaleway_input_tokens_month_to_date=scaleway_usage.total_input_tokens,
        scaleway_output_tokens_month_to_date=scaleway_usage.total_output_tokens,
        usage_total_tokens=scaleway_usage.total_tokens,
        budget_status=_budget_status(settings),
        credential_status=_credential_status(mode, key_configured),
        external_calls_allowed=external_calls_allowed,
        blocking_reason=blocking_reason,
        default_ai_provider=settings.default_ai_provider,
        default_ai_model=settings.default_ai_model,
    )


def evaluate_live_scaleway_smoke_gate(
    settings: AISettingsRead, provider_mode: str
) -> str | None:
    wrapper_reason = _scaleway_smoke_switch_blocking_reason(
        settings,
        provider_mode=provider_mode,
    )
    if wrapper_reason is not None:
        return wrapper_reason
    gate = _evaluate_external_provider_policy(
        settings,
        "scaleway",
        provider_mode=provider_mode,
    )
    return gate.blocking_reason


def _scaleway_switch_blocking_reason(
    settings: AISettingsRead,
    *,
    provider_mode: str,
) -> str | None:
    """Route-level Scaleway switches shared by status, execution and 059b."""
    if provider_mode != "scaleway":
        return "scaleway_provider_mode_required"
    if not settings.scaleway_enabled:
        return "scaleway_disabled"
    return None


def _scaleway_smoke_switch_blocking_reason(
    settings: AISettingsRead,
    *,
    provider_mode: str,
) -> str | None:
    route_reason = _scaleway_switch_blocking_reason(
        settings,
        provider_mode=provider_mode,
    )
    if route_reason is not None:
        return route_reason
    if not settings.scaleway_smoke_test_enabled:
        return "scaleway_smoke_test_disabled"
    if not settings.scaleway_live_smoke_test_enabled:
        return "scaleway_live_smoke_test_disabled"
    return None


def _scaleway_cap_blocking_reason(
    settings: AISettingsRead,
    current_usage: int,
) -> str | None:
    if settings.scaleway_monthly_token_cap <= 0:
        return "scaleway_monthly_token_cap_zero"
    if settings.scaleway_hard_stop_token_cap <= 0:
        return "scaleway_hard_stop_token_cap_zero"
    if current_usage >= settings.scaleway_monthly_token_cap:
        return "scaleway_monthly_token_cap_exhausted"
    if current_usage >= settings.scaleway_hard_stop_token_cap:
        return "scaleway_hard_stop_token_cap_exhausted"
    return None


def _provider_id_for_mode(provider_mode: str) -> str:
    if provider_mode in {"fake", "scaleway", DEEPSEEK_PROVIDER_MODE}:
        return provider_mode
    if _registry_provider(provider_mode) is not None:
        return provider_mode
    return "unknown"


def _adapter_enabled_for_mode(
    provider_mode: str, settings: AISettingsRead
) -> bool:
    if provider_mode == "fake":
        return True
    if provider_mode == "scaleway":
        return settings.scaleway_enabled
    if provider_mode == DEEPSEEK_PROVIDER_MODE:
        return True
    provider = _registry_provider(provider_mode)
    if provider is not None:
        return provider.enabled
    return False


def _budget_status(settings: AISettingsRead) -> str:
    if settings.policy_mode == AIPolicyMode.DISABLED:
        return "ai_policy_disabled"
    if not settings.paid_ai_enabled:
        return "paid_ai_disabled"
    if settings.monthly_api_budget_usd <= 0:
        return "monthly_budget_zero"
    if settings.api_spend_month_to_date_usd >= settings.monthly_api_budget_usd:
        return "monthly_budget_exhausted"
    return "available"


def _credential_status(provider_mode: str, key_configured: bool) -> str:
    if provider_mode == "scaleway":
        return "present" if key_configured else "missing"
    if provider_mode == DEEPSEEK_PROVIDER_MODE:
        provider = _registry_provider(provider_mode)
        if provider is not None and provider.api_key_ref:
            return (
                "present"
                if resolve_secret_ref(provider.api_key_ref).key_present
                else "missing"
            )
        return "present" if DeepSeekProvider().status().configured else "missing"
    provider = _registry_provider(provider_mode)
    if provider is not None and provider.api_key_ref:
        return (
            "present"
            if resolve_secret_ref(provider.api_key_ref).key_present
            else "missing"
        )
    if provider_mode == "fake":
        return "not_required"
    return "unknown"
