from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.main import create_app
from app.modules.engineering import bench_cards
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def workspace_id() -> str:
    initialize_storage(seed_default=True)
    workspace = create_workspace(WorkspaceCreate(name="Schede 315", slug="cards-315", status="active"))
    return workspace.id


def test_card_catalog_and_workspace_validation() -> None:
    ws = workspace_id()
    catalog = bench_cards.list_cards(ws)
    ids = {card["card_id"] for card in catalog["cards"]}
    assert ids == {
        "fluids.pipe_pressure_drop",
        "ht.internal_convection",
        "coolprop.pure_fluid_properties",
        "marine.rigid_body_waves",
        "bioprocess.tube_oxygen_buildup",
    }
    for card in catalog["cards"]:
        assert card["title"] and card["formula"] and card["validity"] and card["source"]
        assert card["fields"]

    with TestClient(create_app()) as client:
        assert client.get("/workspaces/missing/engineering/bench/cards").status_code == 404


def test_invalid_cards_inputs_and_options() -> None:
    ws = workspace_id()
    with TestClient(create_app()) as client:
        base = f"/workspaces/{ws}/engineering/bench"
        assert client.post(f"{base}/runs", json={"card_id": "unknown", "inputs": {}, "options": {}}).status_code == 422
        assert client.post(
            f"{base}/runs",
            json={"card_id": "ht.internal_convection", "inputs": {}, "options": {"method": "unknown"}},
        ).status_code == 422
        assert client.post(
            f"{base}/runs",
            json={
                "card_id": "coolprop.pure_fluid_properties",
                "inputs": {"temperature": "nan", "pressure": 101325},
                "options": {"fluid": "Water"},
            },
        ).status_code == 422
        try:
            bench_cards.CreateBenchRunRequest.model_validate({
                "card_id": "coolprop.pure_fluid_properties",
                "inputs": {"temperature": float("nan"), "pressure": 101325},
                "options": {"fluid": "Water"},
            })
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for NaN input")


def test_successful_cards_and_transitional_refusal_are_path_free() -> None:
    ws = workspace_id()
    with TestClient(create_app()) as client:
        base = f"/workspaces/{ws}/engineering/bench"
        requests = [
            (
                "fluids.pipe_pressure_drop",
                {"density": 997.0, "dynamic_viscosity": 0.00089, "velocity": 1.0,
                 "diameter": 0.05, "length": 10.0, "roughness": 0.000045},
                {},
            ),
            (
                "ht.internal_convection",
                {"reynolds_number": 50000.0, "prandtl_number": 7.0,
                 "thermal_conductivity": 0.6, "diameter": 0.05},
                {"method": "gnielinski"},
            ),
            (
                "coolprop.pure_fluid_properties",
                {"temperature": 298.15, "pressure": 101325.0},
                {"fluid": "Water"},
            ),
        ]
        for card_id, inputs, options in requests:
            response = client.post(f"{base}/runs", json={"card_id": card_id, "inputs": inputs, "options": options})
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["run_id"].startswith("run_")
            assert detail["evaluator_id"] == card_id
            text = response.text
            assert "/tmp/" not in text and "bench" not in text

        refusal = client.post(
            f"{base}/runs",
            json={
                "card_id": "fluids.pipe_pressure_drop",
                "inputs": {"density": 997.0, "dynamic_viscosity": 0.00089, "velocity": 0.05,
                           "diameter": 0.05, "length": 10.0, "roughness": 0.0},
                "options": {},
            },
        )
        assert refusal.status_code == 200, refusal.text
        detail = refusal.json()
        assert detail["status"] == "refused"
        assert detail["failure"] and (detail["failure"].get("backend_code") or detail["failure"].get("code") or detail["failure"].get("message"))
