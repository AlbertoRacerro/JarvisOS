"""Spec 338: bench card for dissolved oxygen build-up along a plug-flow tube."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_process_stack_104 import _failure, _request, _run

from app.core.bootstrap import initialize_storage
from app.main import create_app
from app.modules.engineering import bench_cards
from app.modules.engineering.evaluator_contracts import result_verdict
from app.modules.process_stack.oxygen_tube import ASSUMPTIONS_HINT, EVALUATOR_ID, TubeOxygenBuildupEvaluator
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace

EVALUATOR = TubeOxygenBuildupEvaluator()
DEFAULT_INPUTS: dict[str, tuple[float, str]] = {
    "velocity": (0.5, "m/s"),
    "oxygen_production_rate": (0.003, "mol/(m**3*s)"),
    "saturation_concentration": (0.25, "mol/m**3"),
    "inlet_saturation_fraction": (1.0, "1"),
    "max_saturation_fraction": (3.0, "1"),
    "length": (50.0, "m"),
}


def workspace_id() -> str:
    initialize_storage(seed_default=True)
    workspace = create_workspace(WorkspaceCreate(name="Schede 338", slug="cards-338", status="active"))
    return workspace.id


def test_card_catalog_lists_the_tube_oxygen_card() -> None:
    ws = workspace_id()
    catalog = bench_cards.list_cards(ws)
    card = next(item for item in catalog["cards"] if item["card_id"] == EVALUATOR_ID)
    assert card["evaluator_id"] == EVALUATOR_ID
    assert card["title"] == "Accumulo di ossigeno in tubo"
    assert card["formula"] and card["validity"] and card["source"]
    assert [field["name"] for field in card["fields"]] == [
        "velocity", "oxygen_production_rate", "saturation_concentration",
        "inlet_saturation_fraction", "max_saturation_fraction", "length",
    ]
    assert all(field["label"] and field["unit"] for field in card["fields"])
    assert {field["name"]: field["default"] for field in card["fields"]} == {
        name: value for name, (value, _unit) in DEFAULT_INPUTS.items()
    }


def test_default_run_matches_the_tubular_design_rule() -> None:
    descriptor = EVALUATOR.descriptor()
    assert descriptor.evaluator_id == EVALUATOR_ID
    assert descriptor.backend_kind == "specialist" and descriptor.backend_name == "oxygen_tube"
    assert descriptor.fidelity == "screening"
    assert descriptor.required_checks == ("VALIDITY_RANGE", "FINITE_OUTPUTS")
    assert EVALUATOR.availability().state == "available"

    result = _run(EVALUATOR, _request(EVALUATOR, DEFAULT_INPUTS))
    out = {item.name: (item.value.value, item.value.unit) for item in result.outputs}
    assert out["outlet_saturation_fraction"] == (pytest.approx(2.2, rel=1e-9), "1")
    assert out["outlet_concentration"] == (pytest.approx(0.55, rel=1e-9), "mol/m**3")
    assert out["residence_time"] == (pytest.approx(100.0, rel=1e-9), "s")
    assert out["max_length"] == (pytest.approx(250.0 / 3.0, rel=1e-9), "m")
    checks = {check.id: check for check in result.checks}
    assert set(checks) == {"VALIDITY_RANGE", "FINITE_OUTPUTS"}
    assert all(check.status == "pass" for check in checks.values())
    assert checks["VALIDITY_RANGE"].actual == pytest.approx(2.2, rel=1e-9)
    assert checks["VALIDITY_RANGE"].limit == pytest.approx(3.0)
    assert result_verdict(descriptor, result) == "usable"


def test_length_beyond_max_length_is_a_design_outcome_not_a_refusal() -> None:
    result = _run(EVALUATOR, _request(EVALUATOR, {**DEFAULT_INPUTS, "length": (100.0, "m")}))
    assert result.status == "succeeded"
    out = {item.name: item.value.value for item in result.outputs}
    assert out["outlet_saturation_fraction"] == pytest.approx(3.4, rel=1e-9)
    assert out["outlet_saturation_fraction"] > 3.0
    validity = next(check for check in result.checks if check.id == "VALIDITY_RANGE")
    assert validity.status == "pass"
    assert validity.actual == pytest.approx(out["outlet_saturation_fraction"])
    assert validity.limit == pytest.approx(3.0)
    assert validity.unit == "1"
    assert validity.hint == ASSUMPTIONS_HINT
    assert validity.source == f"constant:{EVALUATOR_ID}"
    assert result_verdict(EVALUATOR.descriptor(), result) == "usable"


@pytest.mark.parametrize(("overrides", "code"), [
    ({"velocity": (0.0, "m/s")}, "input_non_positive"),
    ({"velocity": (-0.5, "m/s")}, "input_non_positive"),
    ({"oxygen_production_rate": (0.0, "mol/(m**3*s)")}, "input_non_positive"),
    ({"saturation_concentration": (-0.25, "mol/m**3")}, "input_non_positive"),
    ({"length": (0.0, "m")}, "input_non_positive"),
    ({"inlet_saturation_fraction": (-0.1, "1")}, "input_negative"),
    ({"max_saturation_fraction": (1.0, "1")}, "saturation_fractions_invalid"),
    ({"max_saturation_fraction": (0.5, "1")}, "saturation_fractions_invalid"),
])
def test_invalid_inputs_are_refused_without_outputs(overrides: dict[str, tuple[float, str]], code: str) -> None:
    result = _run(EVALUATOR, _request(EVALUATOR, {**DEFAULT_INPUTS, **overrides}))
    assert _failure(result) == ("refused", "invalid_input", code)
    assert result.checks == ()
    assert result_verdict(EVALUATOR.descriptor(), result) == "not_usable"


def test_bench_run_route_stores_a_tube_oxygen_run() -> None:
    ws = workspace_id()
    with TestClient(create_app()) as client:
        base = f"/workspaces/{ws}/engineering/bench"
        catalog = client.get(f"{base}/cards")
        assert catalog.status_code == 200, catalog.text
        assert EVALUATOR_ID in {card["card_id"] for card in catalog.json()["cards"]}

        inputs: dict[str, Any] = {name: value for name, (value, _unit) in DEFAULT_INPUTS.items()}
        response = client.post(f"{base}/runs", json={"card_id": EVALUATOR_ID, "inputs": inputs, "options": {}})
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["run_id"].startswith("run_")
        assert detail["evaluator_id"] == EVALUATOR_ID
        assert detail["status"] == "succeeded"
        assert detail["verdict"] == "usable"
        assert "/tmp/" not in response.text and "bench" not in response.text

        listed = client.get(f"{base}/runs")
        assert listed.status_code == 200, listed.text
        assert any(
            row["run_id"] == detail["run_id"] and row["evaluator_id"] == EVALUATOR_ID
            for row in listed.json()["runs"]
        )
        shown = client.get(f"{base}/runs/{detail['run_id']}")
        assert shown.status_code == 200, shown.text
        assert shown.json()["evaluator_id"] == EVALUATOR_ID

        refusal = client.post(
            f"{base}/runs",
            json={"card_id": EVALUATOR_ID, "inputs": {**inputs, "velocity": 0.0}, "options": {}},
        )
        assert refusal.status_code == 200, refusal.text
        refused = refusal.json()
        assert refused["status"] == "refused"
        assert refused["failure"] and (
            refused["failure"].get("backend_code") or refused["failure"].get("code") or refused["failure"].get("message")
        )
