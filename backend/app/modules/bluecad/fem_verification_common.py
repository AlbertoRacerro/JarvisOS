"""Shared deterministic helpers for analytic FEM verification."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


class FemVerificationError(ValueError):
    """Structured fail-closed verification error."""

    def __init__(self, code: str, detail: dict[str, Any]) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def require_positive_finite(values: Mapping[str, float]) -> None:
    """Reject non-positive or non-finite analytic inputs."""

    invalid = {
        key: value
        for key, value in values.items()
        if not math.isfinite(value) or value <= 0.0
    }
    if invalid:
        raise FemVerificationError("NONPOSITIVE_OR_NONFINITE_INPUT", invalid)


def relative_error(actual: float, expected: float) -> float:
    """Return absolute relative error, rejecting a zero/non-finite reference."""

    if not math.isfinite(actual) or not math.isfinite(expected) or expected == 0.0:
        raise FemVerificationError(
            "INVALID_COMPARISON_VALUES",
            {"actual": actual, "expected": expected},
        )
    return abs(actual - expected) / abs(expected)


def comparison_record(
    *, name: str, actual: float, expected: float, tolerance: float
) -> dict[str, Any]:
    """Build a deterministic benchmark comparison record."""

    if not name:
        raise FemVerificationError("INVALID_COMPARISON_NAME", {})
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise FemVerificationError("INVALID_TOLERANCE", {"tolerance": tolerance})
    error = relative_error(actual, expected)
    return {
        "name": name,
        "actual": actual,
        "expected": expected,
        "relative_error": error,
        "tolerance": tolerance,
        "verdict": "pass" if error <= tolerance else "fail",
    }


def deterministic_mean(values: Iterable[float]) -> float:
    """Return a stable arithmetic mean using ``math.fsum``."""

    materialized = [float(value) for value in values]
    if not materialized or not all(math.isfinite(value) for value in materialized):
        raise FemVerificationError("MEAN_VALUES_INVALID", {"count": len(materialized)})
    return math.fsum(materialized) / len(materialized)
