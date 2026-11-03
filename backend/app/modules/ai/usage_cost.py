from app.modules.ai.provider_registry import (
    load_default_provider_registry,
    resolve_model_pricing,
)


def actual_registry_cost_usd(
    *,
    provider_id: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> float | None:
    """Price provider-reported actual tokens with the concrete registry authority.

    ``None`` means the binding could not be priced from the current default registry;
    callers must then preserve conservative reservation accounting instead of treating
    the usage as verified cost evidence.
    """

    try:
        pricing = resolve_model_pricing(
            load_default_provider_registry(),
            provider_id,
            model_id,
        )
    except ValueError:
        return None
    return (
        input_tokens * pricing.input_usd_per_1m_tokens
        + output_tokens * pricing.output_usd_per_1m_tokens
    ) / 1_000_000
