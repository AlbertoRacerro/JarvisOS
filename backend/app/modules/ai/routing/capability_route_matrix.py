CAPABILITY_SIMPLE = "simple"
CAPABILITY_GENERAL_REASONING = "general_reasoning"
CAPABILITY_CODING = "coding"
CAPABILITY_HEAVY_CODING = "heavy_coding"
CAPABILITY_DEEP_REASONING = "deep_reasoning"

CONTEXT_LEVEL_NONE = "none"
CONTEXT_LEVEL_LIGHT = "light"
CONTEXT_LEVEL_STANDARD = "standard"
CONTEXT_LEVEL_DEEP = "deep"

LOCAL_CAPABILITY_ROUTE_MATRIX = {
    CAPABILITY_SIMPLE: "local:fast",
    CAPABILITY_GENERAL_REASONING: "local:general",
    CAPABILITY_CODING: "local:coder",
    CAPABILITY_HEAVY_CODING: "local:coder_heavy",
    CAPABILITY_DEEP_REASONING: "local:general",
}


def local_route_for_capability(capability: str) -> str:
    return LOCAL_CAPABILITY_ROUTE_MATRIX.get(capability, LOCAL_CAPABILITY_ROUTE_MATRIX[CAPABILITY_SIMPLE])


ROUTE_CONTEXT_BUDGET_CHARS = {
    "local:fast": {
        CONTEXT_LEVEL_NONE: 0,
        CONTEXT_LEVEL_LIGHT: 4000,
        CONTEXT_LEVEL_STANDARD: 6000,
    },
    "local:general": {
        CONTEXT_LEVEL_NONE: 0,
        CONTEXT_LEVEL_LIGHT: 6000,
        CONTEXT_LEVEL_STANDARD: 16000,
        CONTEXT_LEVEL_DEEP: 24000,
    },
    "local:coder": {
        CONTEXT_LEVEL_NONE: 0,
        CONTEXT_LEVEL_LIGHT: 4000,
        CONTEXT_LEVEL_STANDARD: 10000,
    },
    "local:coder_heavy": {
        CONTEXT_LEVEL_NONE: 0,
        CONTEXT_LEVEL_LIGHT: 6000,
        CONTEXT_LEVEL_STANDARD: 16000,
        CONTEXT_LEVEL_DEEP: 32000,
    },
}


def route_supported_context_level(route_class: str, requested_level: str) -> tuple[str, str]:
    budgets = ROUTE_CONTEXT_BUDGET_CHARS.get(route_class, ROUTE_CONTEXT_BUDGET_CHARS["local:fast"])
    if requested_level == CONTEXT_LEVEL_DEEP and CONTEXT_LEVEL_DEEP not in budgets:
        return CONTEXT_LEVEL_STANDARD, "deep_downgraded_for_selected_local_route"
    if requested_level not in budgets:
        return CONTEXT_LEVEL_NONE, "context_level_not_supported_by_selected_local_route"
    if requested_level == CONTEXT_LEVEL_NONE:
        return CONTEXT_LEVEL_NONE, "workspace_context_not_requested"
    return requested_level, "route_aware_context_budget_applied"


def context_budget_chars_for_route_level(
    route_class: str,
    context_level: str,
    *,
    max_budget_chars: int | None = None,
) -> int:
    budgets = ROUTE_CONTEXT_BUDGET_CHARS.get(route_class, ROUTE_CONTEXT_BUDGET_CHARS["local:fast"])
    budget_chars = int(budgets.get(context_level, 0))
    if max_budget_chars is not None:
        return min(budget_chars, max_budget_chars)
    return budget_chars
