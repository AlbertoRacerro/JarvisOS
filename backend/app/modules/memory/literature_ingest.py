"""Deterministic checks for values a local model proposes from one stored PDF (spec 297).

A proposal is evidence about the source text only. A value passes only when its quote is on the cited page, the
number and unit are re-derived from that page text, and the quote matches one narrow label-value-unit shape with the
conditions its label requires. Everything else is flagged with a reason; nothing here grants calculation use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.memory.literature_checks import _NUMBER_TOKEN, number_tokens
from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import parse_unit_token, unit_registry

EXTRACTOR_ID = "literature_ingest"
EXTRACTOR_VERSION = "1"

IngestionStatus = Literal["source_checked", "flagged"]


@dataclass(frozen=True)
class QuantityLabel:
    name: str
    aliases: tuple[str, ...]
    reference_units: tuple[str, ...]
    requires_conditions: bool


@dataclass(frozen=True)
class ConditionLabel:
    name: str
    aliases: tuple[str, ...]
    reference_unit: str


# Generic physical quantities only. Project, organism or apparatus terms never enter the core table.
QUANTITY_LABELS: tuple[QuantityLabel, ...] = (
    QuantityLabel("density", ("density",), ("kg/m^3",), True),
    QuantityLabel("dynamic viscosity", ("dynamic viscosity", "viscosity"), ("Pa*s",), True),
    QuantityLabel("solubility", ("solubility",), ("g/L", "mol/L"), True),
    QuantityLabel("diffusivity", ("diffusivity", "diffusion coefficient"), ("m^2/s",), True),
    QuantityLabel("surface tension", ("surface tension",), ("N/m",), True),
    QuantityLabel("thermal conductivity", ("thermal conductivity",), ("W/(m*K)",), True),
    QuantityLabel("specific heat capacity", ("specific heat capacity", "specific heat"), ("J/(kg*K)",), True),
    QuantityLabel("specific growth rate", ("specific growth rate", "growth rate"), ("1/h",), True),
    QuantityLabel("molar mass", ("molar mass", "molecular weight"), ("g/mol",), False),
    QuantityLabel("diameter", ("inner diameter", "outer diameter", "diameter"), ("m",), False),
    QuantityLabel("length", ("length",), ("m",), False),
)

CONDITION_LABELS: tuple[ConditionLabel, ...] = (
    ConditionLabel("temperature", ("temperature", "T"), "K"),
    ConditionLabel("pressure", ("pressure", "p", "P"), "Pa"),
    ConditionLabel("photon flux density", ("photon flux density", "light intensity", "irradiance"), "umol/m^2/s"),
)

_SEPARATOR = r"(?:is|=|:|of|was|were)"
_QUALIFIER = r"of\s+(?P<qualifier>[A-Za-z][\w₀-₉+\-]*)"
_UNIT = r"(?P<unit>[^\s,;()]+(?:\s(?:s|h|K))?)"


class ProposedCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    value_text: str = Field(min_length=1, max_length=40)
    unit: str = Field(min_length=1, max_length=40)


class Proposal(BaseModel):
    """What the model proposes; never stored as a value without the checks below."""

    model_config = ConfigDict(extra="forbid")
    quantity: str = Field(min_length=1, max_length=120)
    value_text: str = Field(min_length=1, max_length=40)
    unit: str = Field(min_length=1, max_length=40)
    conditions: list[ProposedCondition] = Field(default_factory=list, max_length=4)
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=600)


@dataclass
class CheckOutcome:
    check: str
    passed: bool | None  # None: not applicable
    reason: str | None = None


@dataclass
class ValidatedValue:
    status: IngestionStatus
    quantity: str
    value_number: float | None
    unit: str
    conditions: list[dict[str, object]]
    page: int
    quote: str
    checks: list[CheckOutcome] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [item.reason for item in self.checks if item.passed is False and item.reason]


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _dimensionality(unit: str) -> object:
    registry = unit_registry()
    return registry.get_dimensionality(parse_unit_token(unit))


def _same_dimension(unit: str, reference: str) -> bool:
    try:
        return _dimensionality(unit) == _dimensionality(reference)
    except ProcessKernelError:
        return False


def _label_for(quantity: str) -> QuantityLabel | None:
    wanted = _normalise(quantity).lower()
    for label in QUANTITY_LABELS:
        for alias in label.aliases:
            if wanted == alias or wanted.startswith(f"{alias} of "):
                return label
    return None


def _condition_label(name: str) -> ConditionLabel | None:
    wanted = _normalise(name)
    for label in CONDITION_LABELS:
        if wanted in label.aliases or wanted.lower() in label.aliases:
            return label
    return None


def _number(text: str) -> float | None:
    found = number_tokens(text)
    return found[0] if len(found) == 1 else None


_RANGE = re.compile(r"\d\s*(?:[-–—]|to)\s*[−\-+]?\d|\d\s*/\s*\d|±|\+/-")
_NEGATION = re.compile(r"\b(?:not|no|never|neither|nor)\b", re.IGNORECASE)
_CONDITION_AT = re.compile(r"\s*(?:,\s*|\band\s+)?(?:at|under|@)\s+(?P<num>\S+)\s*" + _UNIT)
_CONDITION_PAREN = re.compile(r"\s*\(\s*(?P<name>[A-Za-z][\w ]{0,40}?)\s*=\s*(?P<num>\S+)\s*" + _UNIT + r"\s*\)")


def _shape(quote: str, label: QuantityLabel) -> tuple[re.Match[str] | None, str | None]:
    """Find the single label-value-unit shape in the quote; return a reason code when there is none."""
    aliases = "|".join(re.escape(alias) for alias in sorted(label.aliases, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![\w])(?P<label>{aliases})(?:\s+{_QUALIFIER})?\s*(?:{_SEPARATOR})?\s*"
        rf"(?P<num>[^\s]+)\s*{_UNIT}",
        re.IGNORECASE,
    )
    matches = [match for match in pattern.finditer(quote) if _NUMBER_TOKEN.fullmatch(match["num"])]
    if not matches:
        return None, "label_value_unit_shape_missing"
    if len(matches) > 1:
        return None, "multiple_numbers_after_separator"
    return matches[0], None


def _conditions_after(quote: str, start: int) -> tuple[list[tuple[str, float, str]], int]:
    found: list[tuple[str, float, str]] = []
    position = start
    while True:
        rest = quote[position:]
        match = _CONDITION_AT.match(rest)
        name: str | None = None
        if match is None:
            match = _CONDITION_PAREN.match(rest)
            if match is None:
                break
            label = _condition_label(match["name"])
            name = label.name if label else None
        number = _number(match["num"]) if _NUMBER_TOKEN.fullmatch(match["num"]) else None
        if number is None:
            break
        unit = match["unit"].rstrip(".")
        if name is None:
            name = next((item.name for item in CONDITION_LABELS if _same_dimension(unit, item.reference_unit)), None)
        if name is None:
            break
        found.append((name, number, unit))
        position += match.end()
    return found, position


def validate_proposal(proposal: Proposal, pages: list[str]) -> ValidatedValue:
    """Run the source, unit and meaning checks on one proposal against the cited page text."""
    checks: list[CheckOutcome] = []
    page_text = _normalise(pages[proposal.page - 1]) if proposal.page <= len(pages) else ""
    quote = _normalise(proposal.quote)
    label = _label_for(proposal.quantity)
    result = ValidatedValue(status="flagged", quantity=_normalise(proposal.quantity), value_number=None,
                            unit=proposal.unit.strip(), conditions=[], page=proposal.page, quote=quote, checks=checks)

    if not page_text or quote not in page_text:
        checks.append(CheckOutcome("source_occurrence", False, "quote_not_on_page"))
        return result
    proposed_number = _number(proposal.value_text)
    if proposed_number is None or proposed_number not in number_tokens(quote):
        checks.append(CheckOutcome("source_occurrence", False, "number_not_in_quote"))
        return result
    checks.append(CheckOutcome("source_occurrence", True))

    if label is None:
        checks.append(CheckOutcome("meaning_and_conditions", False, "label_not_in_table"))
        return result
    try:
        parse_unit_token(proposal.unit.strip())
    except ProcessKernelError:
        checks.append(CheckOutcome("unit_dimension", False, "unit_unknown"))
        return result
    if not any(_same_dimension(proposal.unit.strip(), reference) for reference in label.reference_units):
        checks.append(CheckOutcome("unit_dimension", False, "unit_dimension_mismatch"))
        return result
    checks.append(CheckOutcome("unit_dimension", True))

    reason = _meaning_reason(proposal, quote, label, result)
    checks.append(CheckOutcome("meaning_and_conditions", reason is None, reason))
    if reason is None:
        result.status = "source_checked"
    return result


def _meaning_reason(proposal: Proposal, quote: str, label: QuantityLabel, result: ValidatedValue) -> str | None:
    if _NEGATION.search(quote):
        return "negation"
    if _RANGE.search(quote):
        return "range_or_ratio"
    match, reason = _shape(quote, label)
    if match is None:
        if reason == "label_value_unit_shape_missing" and len(number_tokens(quote)) >= 3:
            return "table_meaning_unverified"
        return reason
    number = _number(match["num"])
    unit = match["unit"].rstrip(".")
    if number is None or number != _number(proposal.value_text):
        return "number_differs_from_page"
    if unit != proposal.unit.strip():
        return "unit_differs_from_page"
    conditions, end = _conditions_after(quote, match.end())
    for proposed in proposal.conditions:
        condition_label = _condition_label(proposed.name)
        proposed_number = _number(proposed.value_text)
        if condition_label is None or not any(
            name == condition_label.name and value == proposed_number and unit == proposed.unit.strip()
            for name, value, unit in conditions
        ):
            return "condition_not_in_source"
    # Numbers outside the matched value and condition clauses (unit exponents are inside them).
    outside = number_tokens(f"{quote[:match.start()]} {quote[end:]}")
    if len(outside) >= 2:
        return "table_meaning_unverified"
    if outside:
        return "extra_numbers_in_quote"
    if label.requires_conditions and not conditions:
        return "conditions_missing"
    qualifier = match["qualifier"]
    result.quantity = f"{label.name} of {qualifier}" if qualifier else label.name
    result.value_number = number
    result.unit = unit
    result.conditions = [{"name": name, "value": value, "unit": unit} for name, value, unit in sorted(conditions)]
    return None
