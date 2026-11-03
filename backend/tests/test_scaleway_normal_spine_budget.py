from types import SimpleNamespace

import pytest

from app.modules.ai import budget
from app.modules.ai.contracts import AIPolicyMode
from app.modules.ai.models import AISettingsRead


def _settings(**overrides: object) -> AISettingsRead:
    values: dict[str, object] = {
        "policy_mode": AIPolicyMode.FAST_DEV,
        "monthly_api_budget_usd": 10.0,
        "api_spend_month_to_date_usd": 0.0,
        "paid_ai_enabled": True,
        "default_ai_provider": "scaleway",
        "default_ai_model": "gemma-4-26b-a4b-it",
        "provider_mode": "scaleway",
        "use_fake_provider_when_budget_zero": False,
        "scaleway_enabled": True,
        "scaleway_smoke_test_enabled": True,
        "scaleway_live_smoke_test_enabled": True,
        "scaleway_monthly_token_cap": 1_000,
        "scaleway_hard_stop_token_cap": 1_000,
        "scaleway_free_tier_reference_tokens": 1_000_000,
        "scaleway_input_tokens_month_to_date": 0,
        "scaleway_output_tokens_month_to_date": 0,
        "usage_total_tokens": 0,
        "smoke_test_mode_enabled": True,
        "max_direct_continuations": 0,
        "direct_continuation_policy_version": "test",
        "updated_at": "2026-08-04T00:00:00+00:00",
    }
    values.update(overrides)
    return AISettingsRead(**values)


def _allow_registry_scaleway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget, "scaleway_api_key_configured", lambda: True)
    monkeypatch.setattr(
        budget,
        "resolve_secret_ref",
        lambda _ref: SimpleNamespace(key_present=True),
    )
    monkeypatch.setattr(
        budget,
        "_registry_provider",
        lambda provider_id: SimpleNamespace(
            enabled=True,
            api_key_ref="env:SCALEWAY_API_KEY",
            monthly_token_cap=1_000_000,
            monthly_cost_cap_usd=25.0,
        )
        if provider_id == "scaleway"
        else None,
    )


def _usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reserved_input_tokens: int = 0,
    reserved_output_tokens: int = 0,
    cost_usd: float = 0.0,
    reserved_cost_usd: float = 0.0,
) -> budget.ScalewayUsageSnapshot:
    return budget.ScalewayUsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        reserved_input_tokens=reserved_input_tokens,
        reserved_output_tokens=reserved_output_tokens,
        reserved_cost_usd=reserved_cost_usd,
    )


def _allow_empty_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_registry_scaleway(monkeypatch)
    monkeypatch.setattr(
        budget,
        "scaleway_usage_snapshot",
        lambda _settings: _usage(),
    )


@pytest.mark.parametrize(
    ("settings", "blocking_reason"),
    [
        ({"provider_mode": "fake"}, "scaleway_provider_mode_required"),
        ({"scaleway_enabled": False}, "scaleway_disabled"),
    ],
)
def test_scaleway_normal_route_preserves_route_level_switches(
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, object],
    blocking_reason: str,
) -> None:
    _allow_empty_usage(monkeypatch)

    gate = budget.evaluate_provider_budget_gate(
        _settings(**settings),
        "scaleway",
    )

    assert gate.allowed is False
    assert gate.blocking_reason == blocking_reason


def test_scaleway_normal_route_ignores_wrapper_only_smoke_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_empty_usage(monkeypatch)
    settings = _settings(
        scaleway_smoke_test_enabled=False,
        scaleway_live_smoke_test_enabled=False,
    )

    gate = budget.evaluate_provider_budget_gate(settings, "scaleway")
    status = budget.evaluate_ai_status(settings)

    assert gate.allowed is True
    assert gate.blocking_reason is None
    assert status.external_calls_allowed is True
    assert status.blocking_reason is None


@pytest.mark.parametrize(
    ("settings", "blocking_reason"),
    [
        (
            {"scaleway_smoke_test_enabled": False},
            "scaleway_smoke_test_disabled",
        ),
        (
            {"scaleway_live_smoke_test_enabled": False},
            "scaleway_live_smoke_test_disabled",
        ),
    ],
)
def test_scaleway_live_wrapper_preserves_wrapper_only_switches(
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, object],
    blocking_reason: str,
) -> None:
    _allow_empty_usage(monkeypatch)

    reason = budget.evaluate_live_scaleway_smoke_gate(
        _settings(**settings),
        "scaleway",
    )

    assert reason == blocking_reason


def test_scaleway_gate_counts_active_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_registry_scaleway(monkeypatch)
    monkeypatch.setattr(
        budget,
        "scaleway_usage_snapshot",
        lambda _settings: _usage(
            input_tokens=60,
            output_tokens=20,
            reserved_input_tokens=10,
            reserved_output_tokens=10,
        ),
    )

    gate = budget.evaluate_provider_budget_gate(
        _settings(
            scaleway_monthly_token_cap=100,
            scaleway_hard_stop_token_cap=200,
        ),
        "scaleway",
    )

    assert gate.allowed is False
    assert gate.blocking_reason == "scaleway_monthly_token_cap_exhausted"
    assert gate.usage_tokens_month_to_date == 100


def test_scaleway_status_uses_same_combined_usage_as_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_registry_scaleway(monkeypatch)
    snapshot = _usage(
        input_tokens=70,
        output_tokens=10,
        reserved_input_tokens=12,
        reserved_output_tokens=8,
        cost_usd=1.5,
        reserved_cost_usd=0.25,
    )
    monkeypatch.setattr(
        budget,
        "scaleway_usage_snapshot",
        lambda _settings: snapshot,
    )

    status = budget.evaluate_ai_status(
        _settings(
            scaleway_monthly_token_cap=100,
            scaleway_hard_stop_token_cap=200,
        )
    )

    assert status.scaleway_input_tokens_month_to_date == 82
    assert status.scaleway_output_tokens_month_to_date == 18
    assert status.usage_total_tokens == 100
    assert status.external_calls_allowed is False
    assert status.blocking_reason == "scaleway_monthly_token_cap_exhausted"


def test_scaleway_gate_combines_historical_and_routed_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_registry_scaleway(monkeypatch)
    monkeypatch.setattr(
        budget,
        "scaleway_usage_snapshot",
        lambda _settings: _usage(
            input_tokens=1,
            cost_usd=24.0,
            reserved_cost_usd=1.0,
        ),
    )

    gate = budget.evaluate_provider_budget_gate(_settings(), "scaleway")

    assert gate.allowed is False
    assert gate.blocking_reason == "scaleway_monthly_cost_cap_exhausted"
    assert gate.cost_month_to_date_usd == 25.0
