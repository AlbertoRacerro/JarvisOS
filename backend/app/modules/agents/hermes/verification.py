"""Bounded, offline checks of a completed Hermes answer.

Warnings are intentionally generic: neither answer fragments nor tool values enter events.
"""
from __future__ import annotations

import re
from math import isfinite
from typing import Any

from app.modules.process_kernel.units import parse_unit_token, unit_registry

_QUANTITY = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(%|[A-Za-zµ°][A-Za-zµ°0-9/*^.-]{0,24})(?!\w)")
_ENGINEERING = re.compile(r"^(?:kg|mg|g|m|cm|mm|km|L|mL|Pa|kPa|MPa|bar|K|kelvin|°C|W|kW|J|s|h|mol|N|Hz|ppm|psi|[A-Za-z]+/(?:m|s|L))", re.I)
_NONNEGATIVE = re.compile(r"\b(?:density|volume)\s*(?:is|of|=|:)?\s*(-\d+(?:\.\d+)?)\b", re.I)
_ABSOLUTE_PRESSURE = re.compile(r"\b(?:absolute\s+pressure|pressure\s*\(absolute\))\s*(?:is|of|=|:)?\s*(-\d+(?:\.\d+)?)\b", re.I)
_TOTAL = re.compile(r"\b(?:total|sum|combined|components?)\s*(?:is|of|=|:)?\s*(\d+(?:\.\d+)?)\s*%", re.I)


def verify_answer(final_response: str, turn_tool_outputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return event-safe checks; unexpected input only skips checks."""
    checks: list[dict[str, str]] = []

    def warn(kind: str, summary: str) -> None:
        if len(checks) < 20 and not any(c["kind"] == kind and c["summary"] == summary for c in checks):
            checks.append({"kind": kind, "severity": "warning", "summary": summary[:200]})

    try:
        text = final_response[:100_000] if isinstance(final_response, str) else ""
        quantities: list[tuple[float, str, int]] = []
        for match in _QUANTITY.finditer(text):
            value, unit = float(match[1]), match[2]
            if not isfinite(value):
                continue
            if unit == "%":
                quantities.append((value, unit, match.start()))
                continue
            if not _ENGINEERING.match(unit):
                continue
            try:
                parse_unit_token(unit)
            except Exception:
                warn("unit", "An engineering unit in the answer is unrecognised or malformed.")
                continue
            quantities.append((value, unit, match.start()))
            if value < 0 and unit.lower() in {"k", "kelvin"}:
                warn("physical_bounds", "An absolute Kelvin temperature is negative.")

        if _NONNEGATIVE.search(text) or _ABSOLUTE_PRESSURE.search(text):
            warn("physical_bounds", "An absolute density, volume, or pressure is negative.")
        if any(float(m[1]) > 100 for m in _TOTAL.finditer(text)):
            warn("physical_bounds", "A bounded percentage total exceeds 100%.")
        for match in re.finditer(r"\b(?:components?|fractions?|percentages?)\s*:\s*((?:\d+(?:\.\d+)?\s*%\s*(?:\+\s*)?){2,})", text, re.I):
            if sum(float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", match[1])) > 100:
                warn("physical_bounds", "A bounded component percentage sum exceeds 100%.")

        # Compare only a labelled answer quantity with a numeric value under the
        # same key and unit in a succeeded tool result. This avoids accidental
        # matches between unrelated numbers which happen to share a unit.
        outputs: list[tuple[str, float, str]] = []
        def collect(value: Any, depth: int = 0) -> None:
            if depth > 5 or len(outputs) >= 500:
                return
            if isinstance(value, dict):
                if isinstance(value.get("value"), (int, float)) and isinstance(value.get("unit"), str):
                    name = value.get("name") or value.get("metric") or value.get("label")
                    outputs.append((name.lower() if isinstance(name, str) else "", float(value["value"]), value["unit"]))
                for key, item in value.items():
                    if isinstance(item, dict) and isinstance(item.get("value"), (int, float)) and isinstance(item.get("unit"), str):
                        outputs.append((str(key).lower(), float(item["value"]), item["unit"]))
                    collect(item, depth + 1)
            elif isinstance(value, list):
                for item in value[:100]:
                    collect(item, depth + 1)
        for output in (turn_tool_outputs or [])[:50]:
            collect(output)
        registry = unit_registry()
        for value, unit, pos in quantities:
            if unit == "%":
                continue
            context = text[max(0, pos - 70):pos].lower()
            same_unit = [item for item in outputs if item[2] == unit]
            for name, tool_value, tool_unit in outputs:
                if not ((name and name in context) or (len(same_unit) == 1 and (name, tool_value, tool_unit) == same_unit[0])):
                    continue
                if not isfinite(tool_value) or value == 0 or tool_value == 0:
                    continue
                try:
                    converted = (tool_value * registry.Unit(parse_unit_token(tool_unit))).to(parse_unit_token(unit)).magnitude
                    ratio = abs(value / converted)
                except Exception:
                    continue
                if ratio >= 10 or ratio <= 0.1:
                    warn("magnitude", "An answer quantity differs by at least 10× from a same-turn tool result.")
                    break
    except Exception:
        pass
    return {"status": "warnings" if checks else "passed", "warning_count": len(checks), "checks": checks}
