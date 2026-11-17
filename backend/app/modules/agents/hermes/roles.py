"""Hermes named role registry. Deliberately stdlib-only and independent of heavy app imports."""
from __future__ import annotations

from dataclasses import dataclass

BASE_PRESENTATION_INSTRUCTIONS = (
    "Write formulas in LaTeX, as $...$ inline and $$...$$ for display; write a plain currency amount as \\$5."
)

SHELDON_INSTRUCTION_BLOCK = (
    "Devil's advocate mode (Sheldon): Rigorously challenge assumptions, scrutinize hypotheses, "
    "probe edge cases, and highlight potential failure modes or unstated premises."
)


@dataclass(frozen=True)
class HermesRole:
    role_id: str
    display_name: str
    purpose: str
    presentation_instructions: str
    toolsets: tuple[str, ...]
    supports_sheldon_mode: bool = False


ROLES: dict[str, HermesRole] = {
    "isaac": HermesRole(
        role_id="isaac",
        display_name="Isaac",
        purpose="Scientific reasoning colleague; analytical problem solving and theory.",
        presentation_instructions=(
            f"{BASE_PRESENTATION_INSTRUCTIONS} "
            "You are Isaac, a scientific reasoning colleague. Approach problems with rigorous derivation, "
            "first-principles analysis, structured explanations, and clear physical intuition."
        ),
        toolsets=("mcp-jarvis", "memory", "session_search"),
        supports_sheldon_mode=True,
    ),
    "gregor": HermesRole(
        role_id="gregor",
        display_name="Gregor",
        purpose="Scientific internet researcher (runs with existing broker tools until web research is brokered).",
        presentation_instructions=(
            f"{BASE_PRESENTATION_INSTRUCTIONS} "
            "You are Gregor, a scientific internet researcher. Synthesize evidence, cite literature, "
            "compare findings methodically, and highlight empirical data."
        ),
        toolsets=("mcp-jarvis", "memory", "session_search"),
        supports_sheldon_mode=False,
    ),
    "q": HermesRole(
        role_id="q",
        display_name="Q",
        purpose="Tool-using agent for workspace operations and procedure execution.",
        presentation_instructions=(
            f"{BASE_PRESENTATION_INSTRUCTIONS} "
            "You are Q, a tool-using agent. Formulate precise tool calls, inspect and modify workspace artifacts, "
            "and execute procedural steps with structured discipline."
        ),
        toolsets=("mcp-jarvis", "memory", "session_search"),
        supports_sheldon_mode=False,
    ),
    "alfred": HermesRole(
        role_id="alfred",
        display_name="Alfred",
        purpose="Mail, calendar, and documents assistant (runs with existing broker tools until mail and calendar are brokered).",
        presentation_instructions=(
            f"{BASE_PRESENTATION_INSTRUCTIONS} "
            "You are Alfred, an assistant for communications, calendar, and documentation. Maintain professional clarity, "
            "draft structured notes, organize schedule items, and summarize documents."
        ),
        toolsets=("mcp-jarvis", "memory", "session_search"),
        supports_sheldon_mode=False,
    ),
}

DEFAULT_ROLE_ID = "isaac"
ROLE_IDS: tuple[str, ...] = tuple(ROLES.keys())
HERMES_ROLES = ROLES


def get_role(role_id: str | None) -> HermesRole:
    if role_id is None or role_id == "default":
        return ROLES[DEFAULT_ROLE_ID]
    try:
        return ROLES[role_id]
    except KeyError as exc:
        raise ValueError(f"unknown Hermes role: {role_id}") from exc


def require_local_role_route(route: str) -> None:
    """Reject routes whose primary or fallback can leave local compute."""
    from app.modules.ai.execution import resolve_binding
    from app.modules.ai.provider_registry import load_default_provider_registry

    if not route.startswith("local:"):
        raise ValueError("Hermes requires a local route")
    registry = load_default_provider_registry()
    binding, _ = resolve_binding(route, registry.bindings)
    if binding is None or binding.requires_network or binding.execution_class != "local_compute":
        raise ValueError("Hermes route must bind to local compute")
    for fallback in registry.fallback_chains.get(route, ()):
        provider = registry.providers[fallback.provider_id]
        if provider.requires_network or provider.execution_class != "local_compute":
            raise ValueError("Hermes route fallback must bind to local compute")


def list_roles() -> list[HermesRole]:
    return list(ROLES.values())


def get_role_instructions(role_id: str | None, *, sheldon_mode: bool = False) -> str:
    role = get_role(role_id)
    instructions = role.presentation_instructions
    if sheldon_mode and role.supports_sheldon_mode:
        instructions = f"{instructions}\n\n{SHELDON_INSTRUCTION_BLOCK}"
    return instructions
