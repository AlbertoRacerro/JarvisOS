from __future__ import annotations

import math
import re
from collections.abc import Sequence

from app.modules.engineering.evaluator_contracts import CheckFamily, CheckStatus, ResultCheck, Verdict
from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import parse_unit_token

CHECK_SOURCE = "policy:literature_datum_checks"
REQUIRED_CHECK_IDS = frozenset({"LIT_LOCATOR_PRESENT", "LIT_VALUE_AT_LOCATOR"})
RELATIVE_TOLERANCE = 1e-9
ZERO_ABSOLUTE_TOLERANCE = 1e-12

_SUPERSCRIPTS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
}
_SIGNS = "-+−"
_SUPER_CLASS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_NUMBER_TOKEN = re.compile(
    rf"""
    (?<![\w.,])
    (?P<sign>[{_SIGNS}])?
    (?P<num>\d+(?:[.,]\d+)?|\.\d+)
    (?:
        \s*[×x·]\s*10
        (?:
            \^\s*(?P<csign>[{_SIGNS}]?)(?P<cexp>\d+)
            | (?P<dsign>[{_SIGNS}])(?P<dexp>\d+)
            | (?P<ssign>⁻?)(?P<sexp>[{_SUPER_CLASS}]+)
        )
        | (?P<e>[eE][{_SIGNS}]?\d+)
    )?
    (?!\d)
    """,
    re.VERBOSE | re.ASCII,
)


def _signed(sign: str | None) -> str:
    return "-" if sign in {"-", "−", "⁻"} else ""


def number_tokens(text: str) -> list[float]:
    """Every number the text states, in the forms the datum check accepts."""
    values: list[float] = []
    for match in _NUMBER_TOKEN.finditer(text):
        mantissa = _signed(match["sign"]) + match["num"].replace(",", ".")
        exponent = ""
        if match["cexp"] is not None:
            exponent = _signed(match["csign"]) + match["cexp"]
        elif match["dexp"] is not None:
            exponent = _signed(match["dsign"]) + match["dexp"]
        elif match["sexp"] is not None:
            exponent = _signed(match["ssign"]) + "".join(_SUPERSCRIPTS[ch] for ch in match["sexp"])
        elif match["e"] is not None:
            exponent = match["e"][1:].replace("−", "-")
        try:
            values.append(float(f"{mantissa}e{exponent}" if exponent else mantissa))
        except (ValueError, OverflowError):
            continue
    return values


def _same_number(found: float, wanted: float) -> bool:
    if wanted == 0:
        return abs(found) <= ZERO_ABSOLUTE_TOLERANCE
    return math.isclose(found, wanted, rel_tol=RELATIVE_TOLERANCE, abs_tol=0.0)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _check(check_id: str, family: CheckFamily, status: CheckStatus, hint: str) -> ResultCheck:
    return ResultCheck(id=check_id, family=family, status=status, hint=hint, source=CHECK_SOURCE)


def _locator_check(locator_kind: str | None, locator_start: int | None) -> ResultCheck:
    if locator_kind is not None and locator_start is not None:
        return _check("LIT_LOCATOR_PRESENT", "input", "pass", "The datum names where it was taken from.")
    return _check("LIT_LOCATOR_PRESENT", "input", "not_evaluated", "Add the line range where this value appears.")


def _value_check(
    *,
    value_number: float | None,
    value_text: str | None,
    locator_kind: str | None,
    locator_start: int | None,
    locator_end: int | None,
    lines: Sequence[str] | None,
    unavailable: str,
) -> ResultCheck:
    if locator_kind is None or locator_start is None:
        return _check("LIT_VALUE_AT_LOCATOR", "reference", "not_evaluated", "Add a line locator so the value can be checked.")
    if locator_kind != "line":
        return _check(
            "LIT_VALUE_AT_LOCATOR", "reference", "not_evaluated",
            f"A {locator_kind} locator cannot be checked yet; only line locators on text or markdown are.",
        )
    if lines is None:
        reason = unavailable or "The backing text is not readable."
        return _check("LIT_VALUE_AT_LOCATOR", "reference", "not_evaluated", f"{reason} Line locators need a readable text backing.")
    end = locator_end if locator_end is not None else locator_start
    if locator_start < 1 or end > len(lines):
        return _check(
            "LIT_VALUE_AT_LOCATOR", "reference", "fail",
            "The locator is outside the current text; the backing changed, so fix the line range.",
        )
    located = "\n".join(lines[locator_start - 1 : end])
    if value_number is not None:
        found = any(_same_number(token, value_number) for token in number_tokens(located))
        label = "number"
    else:
        found = _normalise(value_text or "") in _normalise(located)
        label = "value text"
    if found:
        return _check("LIT_VALUE_AT_LOCATOR", "reference", "pass", f"The {label} appears in the located lines.")
    return _check(
        "LIT_VALUE_AT_LOCATOR", "reference", "fail",
        f"The {label} is not in the located lines; correct the value or the locator.",
    )


def _unit_check(value_number: float | None, unit: str | None) -> ResultCheck:
    if unit is None or not unit.strip():
        if value_number is None:
            return _check("LIT_UNIT_KNOWN", "input", "not_evaluated", "No unit is stated for this text value.")
        return _check("LIT_UNIT_KNOWN", "input", "warn", "Add the unit of this number.")
    try:
        parse_unit_token(unit)
    except ProcessKernelError:
        return _check("LIT_UNIT_KNOWN", "input", "warn", "The unit is not a known unit token; correct its spelling.")
    return _check("LIT_UNIT_KNOWN", "input", "pass", "The unit is a known unit.")


def derive_verdict(checks: Sequence[ResultCheck]) -> Verdict:
    required = [item for item in checks if item.id in REQUIRED_CHECK_IDS]
    if any(item.status == "fail" for item in required):
        return "not_usable"
    if any(item.status == "not_evaluated" for item in required):
        return "unchecked"
    if any(item.status in {"warn", "fail"} for item in checks if item.id not in REQUIRED_CHECK_IDS):
        return "usable_with_warnings"
    return "usable"


def check_datum(
    *,
    value_number: float | None,
    value_text: str | None,
    unit: str | None,
    locator_kind: str | None,
    locator_start: int | None,
    locator_end: int | None,
    lines: Sequence[str] | None,
    unavailable: str = "",
) -> tuple[list[ResultCheck], Verdict]:
    """Deterministic checks and verdict for one literature datum; ``lines`` is None when the backing is unreadable."""
    checks = [
        _locator_check(locator_kind, locator_start),
        _value_check(
            value_number=value_number,
            value_text=value_text,
            locator_kind=locator_kind,
            locator_start=locator_start,
            locator_end=locator_end,
            lines=lines,
            unavailable=unavailable,
        ),
        _unit_check(value_number, unit),
    ]
    return checks, derive_verdict(checks)
