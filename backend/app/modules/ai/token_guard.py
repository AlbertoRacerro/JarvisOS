from dataclasses import dataclass
from math import ceil

from app.modules.ai.models import AISettingsRead, SmokeTestTokenMetadata


@dataclass(frozen=True)
class TokenGuardDecision:
    allowed: bool
    reason: str | None
    metadata: SmokeTestTokenMetadata


def estimate_tokens(text: str) -> int:
    return max(1, ceil(len(text) / 4))


def evaluate_token_guard(settings: AISettingsRead, *, input_text: str, estimated_output_tokens: int = 80) -> TokenGuardDecision:
    estimated_input = estimate_tokens(input_text)
    current_usage = settings.scaleway_input_tokens_month_to_date + settings.scaleway_output_tokens_month_to_date
    projected_usage = current_usage + estimated_input + estimated_output_tokens

    blocked = (
        projected_usage > settings.scaleway_monthly_token_cap
        or projected_usage > settings.scaleway_hard_stop_token_cap
    )
    reason = "scaleway_monthly_token_cap_exceeded" if blocked else None

    return TokenGuardDecision(
        allowed=not blocked,
        reason=reason,
        metadata=SmokeTestTokenMetadata(
            blocked_by_token_cap=blocked,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output_tokens,
            reported_input_tokens=None,
            reported_output_tokens=None,
            monthly_token_cap=settings.scaleway_monthly_token_cap,
            hard_stop_token_cap=settings.scaleway_hard_stop_token_cap,
            token_usage_month_to_date=current_usage,
        ),
    )


def metadata_with_reported_usage(
    metadata: SmokeTestTokenMetadata,
    *,
    reported_input_tokens: int | None,
    reported_output_tokens: int | None,
) -> SmokeTestTokenMetadata:
    usage_source = "actual" if reported_input_tokens is not None or reported_output_tokens is not None else "estimated"
    return SmokeTestTokenMetadata(
        blocked_by_token_cap=metadata.blocked_by_token_cap,
        estimated_input_tokens=metadata.estimated_input_tokens,
        estimated_output_tokens=metadata.estimated_output_tokens,
        reported_input_tokens=reported_input_tokens,
        reported_output_tokens=reported_output_tokens,
        monthly_token_cap=metadata.monthly_token_cap,
        hard_stop_token_cap=metadata.hard_stop_token_cap,
        token_usage_month_to_date=metadata.token_usage_month_to_date,
        usage_source=usage_source,
    )
