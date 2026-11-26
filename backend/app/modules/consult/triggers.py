"""Deterministic objective consult triggers (Spec 383).

Pure deterministic evaluator for expert consult triggers (zero model calls, no learned router).
Fires:
  - explicit_user_request: user prompt matches explicit consult phrases (e.g. "chiedi a opus",
    "chiedi al consulente", "ask opus", "consult the expert"). Recommended kind: ideas (or physics/literature/critique if specified).
  - plan_label: task_label in jarvis_turn_plan set_plan (creative -> ideas, physics -> physics, literature -> literature).
  - numeric_disagreement: two independent numeric tool outputs reporting the same quantity/unit
    disagreeing by > 15% relative (via Pint conversion). Recommended kind: critique.
  - solver_double_failure: repeated solver failure (jarvis_python, jarvis_bench_run, process_run)
    failing >= 2 times in the same turn. Recommended kind: physics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from app.modules.process_kernel.units import parse_unit_token, unit_registry

# Match explicit phrases with word boundaries
_EXPLICIT_PHRASES = (
    r"\bchiedi\s+a\s+opus\b",
    r"\bask\s+opus\b",
    r"\bchiedi\s+al\s+consulente\b",
    r"\bask\s+(?:the\s+)?consultant\b",
    r"\bconsult\s+(?:the|an)\s+expert\b",
    r"\bask\s+(?:the|an)\s+expert\b",
    r"\bchiedi\s+all['\s]esperto\b",
    r"\bchiedi\s+a\s+claude\b",
    r"\bask\s+claude\b",
)
_EXPLICIT_RE = re.compile("|".join(_EXPLICIT_PHRASES), re.IGNORECASE)

_PHYSICS_PROMPT_RE = re.compile(r"\b(?:physics|fisica)\b", re.IGNORECASE)
_LITERATURE_PROMPT_RE = re.compile(r"\b(?:literature|letteratura|papers?|articoli?)\b", re.IGNORECASE)
_CRITIQUE_PROMPT_RE = re.compile(r"\b(?:critique|critica|recensione|revisione)\b", re.IGNORECASE)

PLAN_TASK_LABELS_TO_KIND: dict[str, str] = {
    "creative": "ideas",
    "physics": "physics",
    "literature": "literature",
}

SOLVER_TOOL_NAMES = frozenset({
    "jarvis_python",
    "python",
    "calc_run",
    "jarvis_bench_run",
    "bench_run",
    "jarvis_process_act",
    "process_act",
    "jarvis_process_run",
    "process_run",
})

_QUANTITY_STR_RE = re.compile(
    r"^(?<![\w.])(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(%|[A-Za-zµ°][A-Za-zµ°0-9/*^.-]{0,24})(?!\w)$"
)


def normalize_tool_name(tool_name: str) -> str:
    """Strip MCP and namespace prefixes."""
    name = tool_name
    if name.startswith("mcp__jarvis__"):
        name = name[len("mcp__jarvis__"):]
    if name.startswith("jarvis."):
        name = name.replace("jarvis.", "jarvis_")
    return name.lower().strip()


@dataclass(frozen=True)
class FiredTrigger:
    code: str
    recommended_kind: str
    reason: str


@dataclass(frozen=True)
class ConsultTriggerResult:
    trigger_codes: list[str]
    recommended_kind: str | None
    fired_triggers: list[FiredTrigger] = field(default_factory=list)

    def __iter__(self):
        return iter((self.trigger_codes, self.recommended_kind))

    def __bool__(self) -> bool:
        return bool(self.trigger_codes)


def _extract_quantities_from_output(output: Any) -> list[tuple[str, float, str]]:
    """Extract (quantity_name, numeric_value, unit_string) from a tool result."""
    quantities: list[tuple[str, float, str]] = []

    def collect(value: Any, key_context: str = "", depth: int = 0) -> None:
        if depth > 4 or len(quantities) >= 50:
            return
        if isinstance(value, dict):
            # Form 1: {"name": ..., "value": 100, "unit": "Pa"}
            val = value.get("value")
            unit = value.get("unit")
            if isinstance(val, (int, float)) and not isinstance(val, bool) and isfinite(float(val)) and isinstance(unit, str) and unit.strip():
                name = value.get("name") or value.get("metric") or value.get("label") or key_context
                if isinstance(name, str) and name.strip():
                    quantities.append((name.lower().strip(), float(val), unit.strip()))

            for k, v in value.items():
                k_str = str(k).strip()
                # Form 2: {"pressure_drop": "236.8 Pa"}
                if isinstance(v, str):
                    m = _QUANTITY_STR_RE.match(v.strip())
                    if m:
                        num_val = float(m.group(1))
                        unit_str = m.group(2)
                        if isfinite(num_val) and unit_str != "%":
                            quantities.append((k_str.lower(), num_val, unit_str))
                # Form 3: {"pressure_drop": 236.8} with unit at outer dict
                elif isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(float(v)):
                    if isinstance(unit, str) and unit.strip() and k not in {"value", "unit", "status", "code"}:
                        quantities.append((k_str.lower(), float(v), unit.strip()))
                collect(v, key_context=k_str, depth=depth + 1)
        elif isinstance(value, list):
            for item in value[:20]:
                collect(item, key_context=key_context, depth=depth + 1)

    collect(output)
    return quantities


def _evaluate_numeric_disagreements(
    tool_outputs: list[dict[str, Any]],
    relative_threshold: float,
) -> tuple[bool, str]:
    """Check if two independent tool outputs report the same quantity disagreeing by > relative_threshold."""
    if len(tool_outputs) < 2:
        return False, ""

    per_output_quantities: list[list[tuple[str, float, str]]] = [
        _extract_quantities_from_output(out) for out in tool_outputs
    ]

    reg = unit_registry()

    for i in range(len(per_output_quantities)):
        for j in range(i + 1, len(per_output_quantities)):
            quantities_i = per_output_quantities[i]
            quantities_j = per_output_quantities[j]

            for name_i, val_i, unit_i in quantities_i:
                for name_j, val_j, unit_j in quantities_j:
                    if name_i == name_j and name_i:
                        try:
                            u_i_str = parse_unit_token(unit_i)
                            u_j_str = parse_unit_token(unit_j)
                            q_i = (val_i * reg.Unit(u_i_str)).to_base_units()
                            q_j = (val_j * reg.Unit(u_j_str)).to_base_units()

                            if q_i.dimensionality == q_j.dimensionality:
                                mag_i = float(q_i.magnitude)
                                mag_j = float(q_j.magnitude)
                                denom = max(abs(mag_i), abs(mag_j))
                                if denom > 0:
                                    rel_diff = abs(mag_i - mag_j) / denom
                                    if rel_diff > relative_threshold:
                                        reason = (
                                            f"Quantity '{name_i}' differs by {rel_diff:.1%} "
                                            f"({val_i} {unit_i} vs {val_j} {unit_j}), exceeding {relative_threshold:.0%} threshold."
                                        )
                                        return True, reason
                        except Exception:
                            continue

    return False, ""


def evaluate_consult_triggers(
    *,
    prompt: str | None = None,
    user_prompt: str | None = None,
    active_plan: dict[str, Any] | None = None,
    task_label: str | None = None,
    turn_tool_outputs: list[dict[str, Any]] | None = None,
    tool_outputs: list[dict[str, Any]] | None = None,
    solver_failures: dict[str, int] | list[str] | None = None,
    tool_failures: dict[str, int] | list[str] | list[dict[str, Any]] | None = None,
    relative_disagreement_threshold: float = 0.15,
    light: bool = False,
    turn_light: bool = False,
) -> ConsultTriggerResult:
    """Pure deterministic evaluator for consult triggers.

    Returns fired trigger codes and recommended kind (or empty if none fire or if light turn).
    """
    if light or turn_light:
        return ConsultTriggerResult(trigger_codes=[], recommended_kind=None, fired_triggers=[])

    fired: list[FiredTrigger] = []

    # 1. explicit_user_request
    effective_prompt = (prompt if prompt is not None else (user_prompt or "")).strip()
    if effective_prompt and _EXPLICIT_RE.search(effective_prompt):
        if _PHYSICS_PROMPT_RE.search(effective_prompt):
            kind = "physics"
        elif _LITERATURE_PROMPT_RE.search(effective_prompt):
            kind = "literature"
        elif _CRITIQUE_PROMPT_RE.search(effective_prompt):
            kind = "critique"
        else:
            kind = "ideas"
        fired.append(FiredTrigger(
            code="explicit_user_request",
            recommended_kind=kind,
            reason=f"User prompt matches explicit consult request pattern; recommended kind '{kind}'.",
        ))

    # 2. plan_label
    label = task_label or (active_plan.get("task_label") if isinstance(active_plan, dict) else None)
    if isinstance(label, str):
        label_norm = label.lower().strip()
        if label_norm in PLAN_TASK_LABELS_TO_KIND:
            plan_kind = PLAN_TASK_LABELS_TO_KIND[label_norm]
            fired.append(FiredTrigger(
                code="plan_label",
                recommended_kind=plan_kind,
                reason=f"Turn plan task_label is '{label_norm}'; recommended kind '{plan_kind}'.",
            ))

    # 3. numeric_disagreement
    effective_outputs = turn_tool_outputs if turn_tool_outputs is not None else (tool_outputs or [])
    disagreed, disagreement_reason = _evaluate_numeric_disagreements(
        effective_outputs,
        relative_threshold=relative_disagreement_threshold,
    )
    if disagreed:
        fired.append(FiredTrigger(
            code="numeric_disagreement",
            recommended_kind="critique",
            reason=disagreement_reason,
        ))

    # 4. solver_double_failure
    counts: dict[str, int] = {}
    failures_src = solver_failures if solver_failures is not None else tool_failures
    if isinstance(failures_src, dict):
        for k, v in failures_src.items():
            norm_k = normalize_tool_name(str(k))
            if norm_k in SOLVER_TOOL_NAMES and isinstance(v, int):
                counts[norm_k] = counts.get(norm_k, 0) + v
    elif isinstance(failures_src, list):
        for item in failures_src:
            if isinstance(item, str):
                norm_item = normalize_tool_name(item)
                if norm_item in SOLVER_TOOL_NAMES:
                    counts[norm_item] = counts.get(norm_item, 0) + 1
            elif isinstance(item, dict):
                t_name = str(item.get("tool_name") or item.get("name") or "")
                norm_t = normalize_tool_name(t_name)
                is_fail = item.get("status") in {"failed", "error"} or bool(item.get("error"))
                if norm_t in SOLVER_TOOL_NAMES and is_fail:
                    counts[norm_t] = counts.get(norm_t, 0) + 1

    double_failure_tool = next((t for t, c in counts.items() if c >= 2), None)
    if double_failure_tool is not None:
        fired.append(FiredTrigger(
            code="solver_double_failure",
            recommended_kind="physics",
            reason=f"Solver tool '{double_failure_tool}' failed {counts[double_failure_tool]} times in this turn.",
        ))

    trigger_codes = [t.code for t in fired]
    recommended_kind = fired[0].recommended_kind if fired else None

    return ConsultTriggerResult(
        trigger_codes=trigger_codes,
        recommended_kind=recommended_kind,
        fired_triggers=fired,
    )
