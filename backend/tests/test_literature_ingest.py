from __future__ import annotations

import pytest

from app.modules.memory.literature_ingest import QUANTITY_LABELS, Proposal, validate_proposal

PAGE_2 = (
    "Results\nThe solubility of CO2 = 0.8 g/L at 25 °C in fresh water.\n"
    "Table 2: run A 1.2 3.4 5.6\n"
)


def _proposal(**overrides: object) -> Proposal:
    data: dict[str, object] = {
        "quantity": "solubility of CO2", "value_text": "0.8", "unit": "g/L",
        "conditions": [{"name": "temperature", "value_text": "25", "unit": "°C"}],
        "page": 2, "quote": "The solubility of CO2 = 0.8 g/L at 25 °C in fresh water.",
    }
    data.update(overrides)
    return Proposal.model_validate(data)


def _check(quote: str, **overrides: object):
    return validate_proposal(_proposal(quote=quote, **overrides), ["", quote])


def test_value_in_the_accepted_pattern_is_source_checked_with_page_number_and_conditions() -> None:
    value = validate_proposal(_proposal(), ["Introduction", PAGE_2])
    assert value.status == "source_checked", value.reasons
    assert value.quantity == "solubility of CO2"
    assert value.value_number == 0.8 and value.unit == "g/L"
    assert value.conditions == [{"name": "temperature", "value": 25.0, "unit": "°C"}]


def test_table_row_value_is_flagged_table_meaning_unverified() -> None:
    value = validate_proposal(
        _proposal(quantity="solubility", value_text="3.4", conditions=[], quote="Table 2: run A 1.2 3.4 5.6"),
        ["", PAGE_2],
    )
    assert value.reasons == ["table_meaning_unverified"]


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"quote": "The solubility of CO2 = 0.9 g/L at 25 °C."}, "quote_not_on_page"),
    ({"page": 1}, "quote_not_on_page"),
    ({"value_text": "0.9"}, "number_not_in_quote"),
    ({"unit": "g/zorkmid"}, "unit_unknown"),
    ({"unit": "m"}, "unit_dimension_mismatch"),
])
def test_source_and_unit_failures_are_flagged_with_their_reason(overrides: dict[str, object], reason: str) -> None:
    value = validate_proposal(_proposal(**overrides), ["Introduction", PAGE_2])
    assert value.status == "flagged"
    assert reason in value.reasons


@pytest.mark.parametrize(("quote", "overrides", "reason"), [
    ("Here viscosity rose while the sample, measured at 20 °C, read 1.1 mPa*s.",
     {"quantity": "viscosity", "value_text": "1.1", "unit": "mPa*s", "conditions": []}, "label_value_unit_shape_missing"),
    ("The turbidity is 4 g/L at 25 °C.", {"quantity": "turbidity", "value_text": "4"}, "label_not_in_table"),
    ("The density is 0.8 1/h at 25 °C.", {"quantity": "density", "unit": "1/h"}, "unit_dimension_mismatch"),
    ("The solubility is 0.8 g/L or solubility is 0.7 g/L at 25 °C.", {"quantity": "solubility"},
     "multiple_numbers_after_separator"),
    ("The solubility is 0.6-0.8 g/L at 25 °C.", {"quantity": "solubility"}, "range_or_ratio"),
    ("The solubility is not 0.8 g/L at 25 °C.", {"quantity": "solubility"}, "negation"),
    ("The solubility is 0.8 g/L, measured near 25 °C.", {"quantity": "solubility"}, "condition_not_in_source"),
    ("The solubility is 0.8 g/L.", {"quantity": "solubility", "conditions": []}, "conditions_missing"),
])
def test_ambiguity_stays_flagged_with_a_distinct_reason(quote: str, overrides: dict[str, object], reason: str) -> None:
    value = _check(quote, **overrides)
    assert value.status == "flagged"
    assert value.reasons == [reason]


def test_label_without_required_conditions_passes_without_a_condition() -> None:
    value = _check("The inner diameter is 0.05 m.", quantity="inner diameter", value_text="0.05", unit="m",
                   conditions=[])
    assert value.status == "source_checked", value.reasons
    assert value.quantity == "diameter" and value.value_number == 0.05


def test_parenthesised_condition_and_pressure_clause_pass() -> None:
    value = _check("The density is 998 kg/m^3 (T = 293 K).", quantity="density", value_text="998", unit="kg/m^3",
                   conditions=[{"name": "T", "value_text": "293", "unit": "K"}])
    assert value.status == "source_checked", value.reasons
    assert value.conditions == [{"name": "temperature", "value": 293.0, "unit": "K"}]


def test_number_is_re_derived_from_the_page_not_the_model() -> None:
    value = _check("The solubility is 8e-1 g/L at 25 °C.", quantity="solubility", value_text="0.8")
    assert value.status == "source_checked", value.reasons
    assert value.value_number == pytest.approx(0.8)


def test_core_label_table_is_generic_and_every_reference_unit_parses() -> None:
    from app.modules.process_kernel.units import parse_unit_token

    for label in QUANTITY_LABELS:
        for unit in label.reference_units:
            parse_unit_token(unit)
