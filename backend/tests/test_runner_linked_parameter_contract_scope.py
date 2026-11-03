from __future__ import annotations

import pytest

from app.modules.runner.input_contracts import canonicalize_input_contract
from app.modules.runner.linked_parameters import contract_requires_canonical_linked_parameters
from app.modules.runner.safety import RunnerSafetyError


def _canonical(contract: dict[str, object]) -> tuple[str, str]:
    payload, digest, _ = canonicalize_input_contract(contract)
    return payload, digest


def test_schema_v1_does_not_grant_canonical_linked_parameter_authority() -> None:
    payload, digest = _canonical(
        {
            "schema_version": 1,
            "evaluation_mode": "forward",
            "variables": [
                {
                    "name": "tube_length",
                    "label": "Tube length",
                    "unit": "m",
                    "required": True,
                    "category": "design",
                    "description": "Tube length.",
                }
            ],
        }
    )

    assert contract_requires_canonical_linked_parameters(payload, digest) is False


def test_schema_v2_grants_canonical_linked_parameter_authority() -> None:
    payload, digest = _canonical(
        {
            "schema_version": 2,
            "evaluation_mode": "forward",
            "variables": [
                {
                    "name": "tube_length",
                    "label": "Tube length",
                    "unit": "m",
                    "required": True,
                    "category": "design",
                    "description": "Tube length.",
                    "physical_dimension": "length",
                }
            ],
        }
    )

    assert contract_requires_canonical_linked_parameters(payload, digest) is True


def test_schema_v3_grants_canonical_linked_parameter_authority() -> None:
    payload, digest = _canonical(
        {
            "schema_version": 3,
            "evaluation_mode": "forward",
            "semantic_context": {
                "applicable_part_kinds": ["tube_run"],
                "model_family_key": "geometry_hydraulics",
                "model_family_label": "Geometry and hydraulics model",
                "model_option_label": "Reviewed 047 tubular-loop V0",
            },
            "variables": [
                {
                    "name": "tube_length",
                    "label": "Tube length",
                    "unit": "m",
                    "required": True,
                    "category": "design",
                    "description": "Tube length.",
                    "physical_dimension": "length",
                    "property_group": "Geometry",
                    "applicable_part_kinds": ["tube_run"],
                }
            ],
        }
    )

    assert contract_requires_canonical_linked_parameters(payload, digest) is True


def test_contract_scope_fails_closed_on_digest_mismatch() -> None:
    payload, _ = _canonical(
        {
            "schema_version": 2,
            "evaluation_mode": "forward",
            "variables": [
                {
                    "name": "tube_length",
                    "label": "Tube length",
                    "unit": "m",
                    "required": True,
                    "category": "design",
                    "description": "Tube length.",
                    "physical_dimension": "length",
                }
            ],
        }
    )

    with pytest.raises(RunnerSafetyError) as exc_info:
        contract_requires_canonical_linked_parameters(payload, "0" * 64)

    assert exc_info.value.code == "runner_input_contract_hash_mismatch"
