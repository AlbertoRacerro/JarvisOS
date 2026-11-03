"""Shared builders for the spec 170 plumbing tests (not a test module)."""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.main import app
from app.modules.bio_models import service as bio_models
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import (
    AddStream,
    AddUnit,
    Connect,
    DraftQuantity,
    SetStreamCulture,
    SetStreamSpec,
    SetThermo,
    SetUnitParams,
)

PIN = {"card_id": "card-1", "card_revision": "r-" + "a" * 16, "card_digest": "b" * 64}
OTHER_PIN_FOR_TESTS = {"card_id": "card-2", "card_revision": "r-" + "c" * 16, "card_digest": "d" * 64}

# (value, unit) per PBR parameter; deliberately synthetic and inside every domain.
PBR_VALUES: dict[str, tuple[float, str]] = {
    "tube_inner_diameter": (0.05, "m"), "tube_length": (100.0, "m"), "tube_count": (10.0, "dimensionless"),
    "liquid_velocity": (0.5, "m/s"), "pump_efficiency": (60.0, "percent"),
    "baffle_friction_multiplier": (1.5, "dimensionless"), "oxygen_kla": (20.0, "1/h"),
    "oxygen_saturation": (8.0, "mg/L"), "peak_par": (1500.0, "umol/(m2.s)"), "photoperiod": (12.0, "h"),
    "diffuse_fraction": (0.2, "dimensionless"), "temperature_mean": (25.0, "degC"),
    "temperature_amplitude": (5.0, "K"),
}


def pbr_quantities(**overrides: tuple[float, str]) -> dict[str, DraftQuantity]:
    merged = {**PBR_VALUES, **overrides}
    return {key: DraftQuantity(value=value, unit=unit) for key, (value, unit) in merged.items()}


def pbr_ops(*, culture: dict[str, tuple[float, str]] | None = None, flow_kg_s: float = 1.0,
            feed_temperature_k: float = 298.15, model: bool = False, params: bool = True) -> list[Any]:
    """Feed -> PBR -> product with a complete, valid culture feed unless overridden."""
    culture = culture if culture is not None else {
        "biomass": (0.0, "kg/m3"), "nitrogen": (0.05, "kg/m3"), "oxygen": (0.0, "kg/m3"), "salinity": (35.0, "g/kg")}
    ops: list[Any] = [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=0),
        AddUnit(op="add_unit", id="pbr", type="PhotobioreactorT1", tag="PBR", x=100, y=0),
        AddStream(op="add_stream", id="product", tag="Product", x=200, y=0),
        Connect(op="connect", stream="feed", end="target", unit="pbr", port=0),
        Connect(op="connect", stream="product", end="source", unit="pbr", port=0),
        SetStreamSpec(op="set_stream_spec", stream="feed", pressure=DraftQuantity(value=1.0, unit="bar"),
                      temperature=DraftQuantity(value=feed_temperature_k, unit="K"),
                      mass_flow=DraftQuantity(value=flow_kg_s, unit="kg/s"), composition={"Water": 1.0},
                      composition_basis="mass"),
    ]
    if culture:
        ops.append(SetStreamCulture(op="set_stream_culture", stream="feed", culture={
            name: DraftQuantity(value=value, unit=unit) for name, (value, unit) in culture.items()}))
    if params:
        ops.append(SetUnitParams(op="set_unit_params", unit="pbr", values=pbr_quantities()))
    if model:
        from app.modules.process_stack.draft_models import SetUnitModel

        ops.append(SetUnitModel(op="set_unit_model", unit="pbr", model=PIN))  # type: ignore[arg-type]
    return ops


def pbr_document(**kwargs: Any) -> dict[str, Any]:
    return draft.apply_ops(draft.empty_document("pbr"), pbr_ops(**kwargs))


def codes(findings: list[dict[str, Any]]) -> set[str]:
    return {item["code"] for item in findings}


def find(findings: list[dict[str, Any]], code: str) -> list[dict[str, Any]]:
    return [item for item in findings if item["code"] == code]


def new_workspace() -> str:
    initialize_database()
    with TestClient(app) as client:
        response = client.post("/workspaces", json={"name": "PBR plumbing", "slug": f"pbr-{uuid4().hex[:10]}"})
        assert response.status_code == 201, response.text
        return str(response.json()["id"])


def make_card(workspace_id: str, name: str, n_source: str = "NH3") -> dict[str, Any]:
    parameter_set = bio_models.create_set(workspace_id, f"Set for {name}")
    parameter_set = bio_models.edit_set_value(
        workspace_id, parameter_set["id"], "k_d", {"value": 0.003, "unit": "1/hour", "expected_unit": "1/hour"},
        parameter_set["revision"], parameter_set["digest"])
    return bio_models.create_card(
        workspace_id, name, parameter_set["id"],
        {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.ctmi",
         "nutrients": ["nutrient.monod"], "combination": "combine.liebig", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"},
        {"value": 0.08, "unit": "1/hour"}, n_source=n_source)


def pin_of(card: dict[str, Any]) -> dict[str, str]:
    return {"card_id": card["id"], "card_revision": card["revision"], "card_digest": card["digest"]}


def deepcopy(value: Any) -> Any:
    return copy.deepcopy(value)
