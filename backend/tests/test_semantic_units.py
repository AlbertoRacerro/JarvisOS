from __future__ import annotations

import hashlib

import pytest

from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import (
    SEMANTIC_UNITS,
    normalize_magnitude,
    semantic_registry_bytes,
    semantic_registry_sha256,
)


def _assert_kernel_error(code: str, operation) -> None:
    with pytest.raises(ProcessKernelError) as exc_info:
        operation()
    assert exc_info.value.code == code


def test_semantic_registry_has_exact_closed_scales_and_digest() -> None:
    expected = {
        "gDW": ("gram", 0.001, "mass", "dry_biomass"),
        "kgDW": ("kilogram", 1.0, "mass", "dry_biomass"),
        "mgN": ("milligram", 0.000001, "mass", "nitrogen"),
        "gN": ("gram", 0.001, "mass", "nitrogen"),
        "mgP": ("milligram", 0.000001, "mass", "phosphorus"),
        "gP": ("gram", 0.001, "mass", "phosphorus"),
        "gC": ("gram", 0.001, "mass", "carbon"),
        "kgC": ("kilogram", 1.0, "mass", "carbon"),
        "mLCO2": ("milliliter", 0.000001, "volume", "carbon_dioxide"),
        "LCO2": ("liter", 0.001, "volume", "carbon_dioxide"),
        "EUR": ("EUR", 1.0, "currency", "EUR"),
    }

    assert set(SEMANTIC_UNITS) == set(expected)
    for token, values in expected.items():
        definition = SEMANTIC_UNITS[token]
        assert (
            definition.pint_unit,
            definition.scale_to_si,
            definition.physical_dimension,
            definition.semantic_basis,
        ) == values
    payload = semantic_registry_bytes()
    assert payload == semantic_registry_bytes()
    assert semantic_registry_sha256() == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("value", "source", "target", "dimension", "basis", "expected"),
    [
        (1000.0, "gDW", "kgDW", "mass", "dry_biomass", 1.0),
        (1000.0, "mgN", "gN", "mass", "nitrogen", 1.0),
        (1000.0, "mLCO2", "LCO2", "volume", "carbon_dioxide", 1.0),
    ],
)
def test_same_basis_semantic_conversions_are_exact(
    value: float,
    source: str,
    target: str,
    dimension: str,
    basis: str,
    expected: float,
) -> None:
    assert normalize_magnitude(
        value,
        source_unit=source,
        target_unit=target,
        physical_dimension=dimension,
        semantic_basis=basis,
    ) == pytest.approx(expected, rel=0.0, abs=1e-15)


@pytest.mark.parametrize(
    ("source", "target", "dimension", "basis", "expected_code"),
    [
        ("mgN", "mgP", "mass", "phosphorus", "quantity_semantic_basis_mismatch"),
        ("kg", "kgDW", "mass", "dry_biomass", "quantity_semantic_basis_mismatch"),
        ("mL", "mLCO2", "volume", "carbon_dioxide", "quantity_semantic_basis_mismatch"),
        ("gDw", "kgDW", "mass", "dry_biomass", "unit_unknown"),
        ("cm", "kg", "mass", None, "quantity_dimension_mismatch"),
    ],
)
def test_cross_basis_lookalike_and_dimension_mismatch_fail_closed(
    source: str,
    target: str,
    dimension: str,
    basis: str | None,
    expected_code: str,
) -> None:
    _assert_kernel_error(
        expected_code,
        lambda: normalize_magnitude(
            1.0,
            source_unit=source,
            target_unit=target,
            physical_dimension=dimension,
            semantic_basis=basis,
        ),
    )
