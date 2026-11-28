"""Offline tests for deterministic techno-economic analysis and cost hotspots (spec 393)."""
from __future__ import annotations

import math
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.agents.hermes import knowledge_read_tool
from app.modules.agents.hermes.knowledge_read_tool import dispatch_knowledge_read_tool
from app.modules.ai.agent_contracts import StructuredToolCall
from app.modules.economics.models import (
    CapexItem,
    EconomicsAssumptions,
    EconomicsModelInput,
    OpexItem,
)
from app.modules.economics.service import evaluate_economics


@pytest.fixture
def isolated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data-root"))
    monkeypatch.delenv("JARVISOS_PACK_PATH", raising=False)
    initialize_storage()
    app = create_app()
    with TestClient(app) as client:
        yield client


def test_straight_line_tea_arithmetic_and_hotspots() -> None:
    model = EconomicsModelInput(
        assumptions=EconomicsAssumptions(
            currency="EUR",
            default_lifetime_years=10.0,
            depreciation_method="straight_line",
            discount_rate=0.0,
            contingency_pct=15.0,
            periodic_maintenance_pct=3.0,
        ),
        capex_items=[
            CapexItem(
                id="p101",
                name="Centrifugal Pump P-101",
                category="equipment",
                quantity=2,
                unit_cost=5000.0,
                installation_factor=1.5,  # purchased=10000, installed=15000, ann=1500
                lifetime_years=10.0,
            ),
            CapexItem(
                id="hx101",
                name="Plate Heat Exchanger HX-101",
                category="equipment",
                quantity=1,
                unit_cost=25000.0,
                installation_factor=2.0,  # purchased=25000, installed=50000, ann=10000 (life=5)
                lifetime_years=5.0,
            ),
        ],
        opex_items=[
            OpexItem(
                id="elec",
                name="Pumping electricity",
                subcategory="utilities_energy",
                unit_rate=0.20,
                annual_usage=100000.0,  # 20000/yr
                unit_label="kWh",
            ),
            OpexItem(
                id="chem",
                name="Process consumables",
                subcategory="consumables",
                annual_cost=4000.0,
            ),
        ],
    )

    ev = evaluate_economics(model)

    # Hand-verified totals:
    # purchased = 10000 + 25000 = 35000
    # installed = 15000 + 50000 = 65000
    # contingency = 65000 * 0.15 = 9750 -> total_capex_with_contingency = 74750
    # annualized_capex = (15000/10) + (50000/5) + (9750/10) = 1500 + 10000 + 975 = 12475
    # periodic_maintenance = 65000 * 0.03 = 1950
    # total_annual_opex = 20000 + 4000 + 1950 = 25950
    # total_annualized_cost = 12475 + 25950 = 38425
    assert ev.is_complete is True
    assert ev.missing_items == []
    assert ev.total_capex_purchased == 35000.0
    assert ev.total_capex_installed == 65000.0
    assert ev.contingency_amount == 9750.0
    assert ev.total_capex_with_contingency == 74750.0
    assert ev.annualized_capex == 12475.0
    assert ev.periodic_maintenance_annual == 1950.0
    assert ev.opex_by_subcategory == {
        "consumables": 4000.0,
        "maintenance": 1950.0,
        "utilities_energy": 20000.0,
    }
    assert ev.total_annual_opex == 25950.0
    assert ev.total_annualized_cost == 38425.0
    assert ev.headline == "This version costs 38,425 EUR/yr (complete)."

    # Hotspots ranked descending by share_pct
    assert ev.capex_hotspots[0].item_id == "hx101"
    assert ev.capex_hotspots[0].share_pct == round((50000.0 / 74750.0) * 100.0, 2)
    assert math.isclose(sum(h.share_pct for h in ev.capex_hotspots), 100.0, abs_tol=0.1)

    assert ev.opex_subcategory_hotspots[0].name == "utilities_energy"
    assert ev.opex_subcategory_hotspots[0].share_pct == round((20000.0 / 25950.0) * 100.0, 2)
    assert ev.opex_hotspots[0].item_id == "elec"
    assert ev.annualized_hotspots[0].item_id == "opex:elec"
    assert "Plate Heat Exchanger HX-101" in ev.hotspot_summary
    assert "utilities_energy" in ev.hotspot_summary


def test_crf_depreciation_exact_formula() -> None:
    r = 0.08
    n = 10.0
    expected_crf = (r * (1.0 + r) ** n) / ((1.0 + r) ** n - 1.0)
    model = EconomicsModelInput(
        assumptions=EconomicsAssumptions(
            currency="USD",
            default_lifetime_years=n,
            depreciation_method="crf",
            discount_rate=r,
            contingency_pct=0.0,
            periodic_maintenance_pct=0.0,
        ),
        capex_items=[
            CapexItem(id="tk1", name="Storage Tank TK-101", unit_cost=100000.0),
        ],
    )

    ev = evaluate_economics(model)
    expected_ann = round(100000.0 * expected_crf, 2)
    assert ev.annualized_capex == expected_ann
    assert ev.total_annualized_cost == expected_ann
    assert ev.is_complete is True


def test_incomplete_model_and_uncosted_components_flagged() -> None:
    model = EconomicsModelInput(
        assumptions=EconomicsAssumptions(contingency_pct=0.0, periodic_maintenance_pct=0.0),
        capex_items=[
            CapexItem(id="c1", name="Pump P-101", unit_cost=None, component_id="comp-1"),
            CapexItem(id="c2", name="Vessel V-101", unit_cost=10000.0, component_id="comp-2"),
        ],
        opex_items=[
            OpexItem(id="o1", name="Cooling water", subcategory="utilities_energy", annual_cost=None),
        ],
    )
    components = [
        {"id": "comp-1", "name": "Pump P-101"},
        {"id": "comp-2", "name": "Vessel V-101"},
        {"id": "comp-3", "name": "Filter F-101"},
    ]

    ev = evaluate_economics(model, design_components=components)
    assert ev.is_complete is False
    kinds = {m.kind for m in ev.missing_items}
    assert kinds == {"missing_capex_unit_cost", "missing_opex_cost", "uncosted_component"}
    assert any(m.name == "Filter F-101" for m in ev.missing_items)
    assert "incomplete: 3 missing items" in ev.headline
    assert ev.total_capex_installed == 10000.0


def test_http_endpoints_persistence_and_404(isolated_client: TestClient) -> None:
    # Create a workspace
    ws_resp = isolated_client.post("/workspaces", json={"name": "TEA Test Plant", "slug": "tea-test-plant"})
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Initial GET returns clean empty incomplete model
    get0 = isolated_client.get(f"/workspaces/{ws_id}/economics")
    assert get0.status_code == 200
    body0 = get0.json()
    assert body0["workspace_id"] == ws_id
    assert body0["updated_at"] is None
    assert body0["evaluation"]["is_complete"] is False
    assert body0["evaluation"]["missing_items"][0]["kind"] == "empty_model"

    # Unknown workspace returns 404
    assert isolated_client.get("/workspaces/does-not-exist/economics").status_code == 404
    assert isolated_client.post(
        "/workspaces/does-not-exist/economics/evaluate", json={}
    ).status_code == 404

    payload = {
        "assumptions": {
            "currency": "EUR",
            "default_lifetime_years": 10.0,
            "depreciation_method": "straight_line",
            "discount_rate": 0.0,
            "contingency_pct": 10.0,
            "periodic_maintenance_pct": 2.0,
        },
        "capex_items": [
            {"id": "p1", "name": "Feed Pump", "category": "equipment", "quantity": 1, "unit_cost": 20000.0}
        ],
        "opex_items": [
            {"id": "e1", "name": "Power", "subcategory": "utilities_energy", "annual_cost": 5000.0}
        ],
    }

    # Stateless evaluate does not persist
    eval_resp = isolated_client.post(f"/workspaces/{ws_id}/economics/evaluate", json=payload)
    assert eval_resp.status_code == 200
    assert eval_resp.json()["is_complete"] is True
    assert isolated_client.get(f"/workspaces/{ws_id}/economics").json()["updated_at"] is None

    # PUT persists and logs event
    put_resp = isolated_client.put(f"/workspaces/{ws_id}/economics", json=payload)
    assert put_resp.status_code == 200
    put_body = put_resp.json()
    assert put_body["updated_at"] is not None
    assert put_body["evaluation"]["total_capex_installed"] == 20000.0
    assert put_body["evaluation"]["contingency_amount"] == 2000.0
    assert put_body["evaluation"]["periodic_maintenance_annual"] == 400.0
    assert put_body["evaluation"]["total_annualized_cost"] == 7600.0

    # Subsequent GET reads persisted model
    get1 = isolated_client.get(f"/workspaces/{ws_id}/economics")
    assert get1.status_code == 200
    assert get1.json()["evaluation"]["total_annualized_cost"] == 7600.0


def test_hermes_knowledge_read_economics_summary(
    isolated_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_resp = isolated_client.post("/workspaces", json={"name": "Hermes TEA Plant", "slug": "hermes-tea-plant"})
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Insert an uncosted design component into the active design version
    with open_sqlite_connection() as conn:
        ver_row = conn.execute(
            "SELECT id FROM design_project_versions WHERE project_id = ? AND active = 1",
            (ws_id,),
        ).fetchone()
        if ver_row is not None:
            conn.execute(
                "INSERT INTO design_components (id, project_id, version_id, parent_id, name, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, 1, '2026-11-28T00:00:00Z', '2026-11-28T00:00:00Z')",
                (str(uuid4()), ws_id, ver_row["id"], "Compressor C-101"),
            )
            conn.commit()

    put_resp = isolated_client.put(
        f"/workspaces/{ws_id}/economics",
        json={
            "capex_items": [{"id": "c1", "name": "Pump P-101", "unit_cost": 10000.0}],
            "opex_items": [{"id": "o1", "name": "Electricity", "subcategory": "utilities_energy", "annual_cost": 3000.0}],
        },
    )
    assert put_resp.status_code == 200

    # Local Hermes call
    monkeypatch.setattr(knowledge_read_tool, "is_local_route", lambda grant=None: True)
    call = StructuredToolCall(
        call_id="call-econ-1",
        grant_id="grant-econ-1",
        capability_id="jarvis.knowledge_read",
        correlation_id="corr-econ-1",
        requested_at="2026-11-28T00:00:00Z",
        deadline_at="2026-11-28T01:00:00Z",
        arguments={"view": "economics_summary", "workspace_id": ws_id},
    )
    data, err = dispatch_knowledge_read_tool(call, ws_id, "tool_error")
    assert data is not None
    assert data["view"] == "economics_summary"
    assert data["workspace_id"] == ws_id
    assert data["total_capex_installed"] == 10000.0
    assert data["opex_subcategory_hotspots"][0]["name"] == "utilities_energy"
    assert data["truncated"] is False

    # Cloud Hermes call executes cloud IP boundary path cleanly
    monkeypatch.setattr(knowledge_read_tool, "is_local_route", lambda grant=None: False)
    cloud_data, _ = dispatch_knowledge_read_tool(call, ws_id, "tool_error")
    assert cloud_data is not None
    assert "egress_mode" in cloud_data
    assert cloud_data["withheld_count"] == 0
