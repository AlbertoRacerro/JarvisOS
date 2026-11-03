from __future__ import annotations

from dataclasses import dataclass

from app.modules.ai.execution import resolve_binding

CHARS_PER_TOKEN_ESTIMATE = 4
DEFAULT_OUTPUT_TOKENS = 1024


@dataclass(frozen=True)
class RoutePrice:
    input_usd_per_1m_tokens: float
    output_usd_per_1m_tokens: float
    currency: str = "USD"


ROUTE_COST_REGISTRY: dict[str, RoutePrice] = {
    "external:cheap": RoutePrice(input_usd_per_1m_tokens=0.10, output_usd_per_1m_tokens=0.20),
    "external:reasoning": RoutePrice(input_usd_per_1m_tokens=0.60, output_usd_per_1m_tokens=1.20),
}


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN_ESTIMATE - 1) // CHARS_PER_TOKEN_ESTIMATE)


def estimate_route_cost(route_class: str, prompt: str, max_output_tokens: int | None = None) -> dict[str, object]:
    price = ROUTE_COST_REGISTRY[route_class]
    input_tokens = estimate_tokens(prompt)
    output_tokens = max_output_tokens or DEFAULT_OUTPUT_TOKENS
    cost = (
        input_tokens * price.input_usd_per_1m_tokens / 1_000_000
        + output_tokens * price.output_usd_per_1m_tokens / 1_000_000
    )
    return {
        "label": "estimate",
        "formula": "ceil(prompt_chars/4) input tokens plus max output tokens, multiplied by route registry prices",
        "currency": price.currency,
        "input_tokens": input_tokens,
        "max_output_tokens": output_tokens,
        "input_usd_per_1m_tokens": price.input_usd_per_1m_tokens,
        "output_usd_per_1m_tokens": price.output_usd_per_1m_tokens,
        "estimated_cost_usd": round(cost, 8),
    }


def build_escalation_proposal(
    *,
    prompt: str,
    proposal_ledger_id: str | None,
    max_output_tokens: int | None,
    sensitivity_hint: str,
) -> dict[str, object]:
    route_class = "external:reasoning"
    binding, decision = resolve_binding(route_class)
    warning = None
    if sensitivity_hint in {"confidential", "sensitive_ip"}:
        warning = f"Prompt was classified as {sensitivity_hint}; confirm only if this may leave the machine."
    return {
        "proposal_ledger_id": proposal_ledger_id,
        "proposed_route_class": route_class,
        "provider_id": binding.provider_id if binding is not None else decision.provider_id,
        "model_id": binding.model_id if binding is not None else decision.model_id,
        "estimated_cost": estimate_route_cost(route_class, prompt, max_output_tokens),
        "outbound_text": prompt,
        "context_excluded": True,
        "sensitivity_warning": warning,
    }
