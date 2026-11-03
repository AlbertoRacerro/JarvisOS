from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.modules.bluecad.spec import (
    SpecValidationError,
    canonical_json,
    canonicalize_geometry_spec,
    geometry_spec_id,
    load_geometry_spec,
    validate_geometry_spec,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixture_names() -> list[str]:
    return sorted(path.stem for path in FIXTURE_DIR.glob("*.json") if not path.name.endswith(".expected.json"))


def _load_fixture(name: str) -> dict:
    with (FIXTURE_DIR / f"{name}.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.mark.parametrize("name", _fixture_names())
def test_golden_fixtures_validate_and_have_stable_spec_id(name: str) -> None:
    spec = _load_fixture(name)

    validate_geometry_spec(spec)
    canonical = canonicalize_geometry_spec(spec)

    assert canonical["spec_id"].startswith("sha256:")
    assert geometry_spec_id(canonical) == canonical["spec_id"]
    assert load_geometry_spec(FIXTURE_DIR / f"{name}.json") == canonical


@pytest.mark.parametrize("name", _fixture_names())
def test_golden_fixtures_include_analytic_volume_values(name: str) -> None:
    expected_path = FIXTURE_DIR / f"{name}.expected.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    assert expected["analytic_total_volume_mm3"] > 0
    assert sum(expected["analytic_parts_mm3"].values()) == pytest.approx(expected["analytic_total_volume_mm3"])


def test_canonicalization_is_stable_across_key_order_permutations() -> None:
    spec = _load_fixture("chain_tube_bend_joint")
    permuted = {
        "declared": deepcopy(spec["declared"]),
        "connections": deepcopy(spec["connections"]),
        "parts": deepcopy(spec["parts"]),
        "name": spec["name"],
        "spec_version": spec["spec_version"],
    }
    permuted["parts"][0] = {
        "frame": permuted["parts"][0]["frame"],
        "params": permuted["parts"][0]["params"],
        "kind": permuted["parts"][0]["kind"],
        "part_id": permuted["parts"][0]["part_id"],
    }

    assert canonical_json(canonicalize_geometry_spec(spec)) == canonical_json(canonicalize_geometry_spec(permuted))
    assert geometry_spec_id(spec) == geometry_spec_id(permuted)


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (lambda spec: spec["parts"][0].__setitem__("kind", "unsupported_kind"), "$.parts[0].kind"),
        (lambda spec: spec["parts"][0]["params"].__setitem__("outer_d", float("nan")), "$.parts[0].params.outer_d"),
        (lambda spec: spec["parts"][0]["params"].__setitem__("wall_t", 999.0), "$.parts[0].params.wall_t"),
    ],
)
def test_invalid_specs_raise_structured_spec_invalid(mutation, path: str) -> None:
    spec = _load_fixture("minimal_single_tube")
    mutation(spec)

    with pytest.raises(SpecValidationError) as exc_info:
        canonicalize_geometry_spec(spec)

    assert exc_info.value.code == "SPEC_INVALID"
    assert exc_info.value.detail["path"] == path
