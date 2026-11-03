"""Spec 209 acceptance: the AI-prepared Engineering Studio example workspace.

Expected values are recomputed here independently of the demonstrator content: a bracketing Colebrook-White root
solve for the pipe, the analytic annulus volume for the spool, mass/biomass arithmetic for the settler and the
closed-form mixing arithmetic of the dark, loss-free harvest cycle.
"""

from __future__ import annotations

import math
import os

import pytest
from fastapi.testclient import TestClient
from scipy.optimize import brentq

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.main import create_app
from app.modules.studio import demonstrator
from app.modules.studio_runtime.runtime import status


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    if os.getenv("JARVISOS_STUDIO_CI_SANDBOX_UNAVAILABLE") == "1":
        pytest.skip("CI runner does not permit bubblewrap user namespaces; WSL2 qualification is required")
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    assert status()["available"], "qualification host must run bubblewrap"
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _example(client: TestClient) -> dict:
    response = client.post("/studio/example-workspace")
    assert response.status_code == 200, response.text
    return response.json()


def _models(client: TestClient, wid: str) -> dict[str, dict]:
    response = client.get(f"/workspaces/{wid}/studio/models")
    assert response.status_code == 200, response.text
    return {model["name"]: model for model in response.json()}


def _pipe_reference(flow_rate: float, diameter: float, length: float, roughness: float, density: float,
                    viscosity: float) -> dict[str, float]:
    q, d, mu = flow_rate / 3600, diameter / 1000, viscosity / 1000
    velocity = q / (math.pi * d * d / 4)
    reynolds = density * velocity * d / mu
    friction = 64 / reynolds if reynolds < 2300 else brentq(
        lambda f: 1 / math.sqrt(f) + 2 * math.log10(roughness / 1000 / d / 3.7 + 2.51 / (reynolds * math.sqrt(f))),
        1e-4, 1.0, xtol=1e-15)
    drop = friction * length / d * density * velocity ** 2 / 2
    return {"velocity": velocity, "reynolds": reynolds, "friction_factor": friction,
            "pressure_drop": drop / 1000, "head_loss": drop / (density * 9.80665)}


def test_example_workspace_holds_four_verified_published_artifacts(client: TestClient) -> None:
    payload = _example(client)
    wid = payload["workspace"]["id"]
    assert payload["workspace"]["slug"] == demonstrator.SLUG
    models = _models(client, wid)
    assert {name: model["category"] for name, model in models.items()} == {
        demonstrator.PIPE_NAME: "calculation", demonstrator.SPOOL_NAME: "geometry",
        demonstrator.SETTLER_NAME: "process_unit", demonstrator.PROCEDURE_NAME: "procedure"}
    for model in models.values():
        assert model["status"] == "published" and len(model["versions"]) == 1, model["name"]
        report = model["last_test_report"]
        assert report["passed"] and report["cases"]
        assert report["source_sha256"] == model["source_sha256"] == model["versions"][0]["source_sha256"]
        assert report["contract_sha256"] == model["contract_sha256"]
        assert "AI-prepared" in model["assumptions"] and "unqualified" in model["assumptions"]
    library = client.get(f"/workspaces/{wid}/studio/library").json()
    assert sorted(item["category"] for item in library) == ["calculation", "geometry", "procedure", "process_unit"]

    # Pipe: laminar and two turbulent cases agree with an independent Colebrook root solve.
    pipe = models[demonstrator.PIPE_NAME]
    regimes = set()
    for case, result in zip(demonstrator.PIPE_CASES, pipe["last_test_report"]["cases"], strict=True):
        reference = _pipe_reference(**case["inputs"])
        regimes.add(reference["reynolds"] < 2300)
        for name, value in reference.items():
            assert result["outputs"][name] == pytest.approx(value, rel=1e-9), name
            assert case["expected_outputs"][name] == pytest.approx(value, rel=1e-9), name
    assert regimes == {True, False}

    # Spool: the built solid matches the analytic annulus volume and the bounding box.
    spool = models[demonstrator.SPOOL_NAME]
    for values, result in zip(demonstrator.SPOOL_INPUTS, spool["last_test_report"]["cases"], strict=True):
        bore = values["tube_od"] - 2 * values["wall"]
        volume = (math.pi / 4 * (values["tube_od"] ** 2 - bore ** 2) * (values["length"] - 2 * values["flange_thickness"])
                  + math.pi / 2 * (values["flange_od"] ** 2 - bore ** 2) * values["flange_thickness"])
        facts = result["facts"]
        assert facts["valid"] and facts["volume"] == pytest.approx(volume, rel=1e-6)
        extents = sorted(high - low for low, high in zip(facts["bbox_min"], facts["bbox_max"], strict=True))
        assert extents == pytest.approx(sorted([values["flange_od"], values["flange_od"], values["length"]]), rel=1e-6)
        assert {item["name"] for item in result["artifacts"]} >= {"model.step", "model.stl", "model.glb"}

    # Settler: total mass and biomass close, biomass partitions by the recovery.
    settler = models[demonstrator.SETTLER_NAME]
    for case, result in zip(demonstrator.SETTLER_CASES, settler["last_test_report"]["cases"], strict=True):
        feed = case["inlets"]["feed"]
        share = case["params"]["concentrate_share"] / 100
        recovery = case["params"]["biomass_recovery"] / 100
        conc, clar = result["outputs"]["concentrate"], result["outputs"]["clarified"]
        assert conc["mass_flow_kg_s"] == pytest.approx(share * feed["mass_flow_kg_s"], rel=1e-12)
        assert conc["mass_flow_kg_s"] + clar["mass_flow_kg_s"] == pytest.approx(feed["mass_flow_kg_s"], rel=1e-12)
        biomass = feed["mass_flow_kg_s"] * feed["culture"]["biomass"]
        assert conc["mass_flow_kg_s"] * conc["culture"]["biomass"] == pytest.approx(recovery * biomass, abs=1e-15)
        assert clar["culture"]["nitrogen"] == feed["culture"]["nitrogen"]

    # Procedure: the post-event tank states equal the closed-form mixing arithmetic of a 20 % draw.
    tank, x0, n0 = demonstrator.TANK_M3, demonstrator.INITIAL["X"], demonstrator.INITIAL["N"]
    v_pbr = demonstrator.TUBE_COUNT * math.pi * demonstrator.TUBE_DIAMETER_M ** 2 / 4 * demonstrator.TUBE_LENGTH_M
    v_draw = 0.2 * (v_pbr + tank)
    v_conc = v_draw * 0.95 / 20
    v1 = tank - v_draw + (v_draw - v_conc)
    x1 = (x0 * (tank - v_draw) + 0.05 * v_draw * x0) / v1
    n2 = (n0 * v1 + 0.5 * v_conc) / tank
    hand = [{"V": v1, "X": x1, "N": n0}, {"V": tank, "X": x1 * v1 / tank, "N": n2},
            {"V": tank, "X": x1 * v1 / tank, "N": n2 + 0.004 / tank}]
    for want, listed in zip(hand, demonstrator.PROCEDURE_EXPECTED, strict=True):
        for key, value in want.items():
            assert listed[key] == pytest.approx(value, rel=1e-13), key
    (case,) = models[demonstrator.PROCEDURE_NAME]["last_test_report"]["cases"]
    assert case["passed"] and len(case["expectations"]) == 14
    assert all(item["passed"] for item in case["expectations"])


def test_repeated_request_is_idempotent_and_never_overwrites_an_edited_artifact(client: TestClient) -> None:
    first = _example(client)
    wid = first["workspace"]["id"]
    pipe = _models(client, wid)[demonstrator.PIPE_NAME]
    path = f"/workspaces/{wid}/studio/models/{pipe['id']}"
    assert client.post(path + "/revise").status_code == 200
    edited = client.put(path, json={"source": pipe["source"] + "\n# operator edit\n"})
    assert edited.status_code == 200, edited.text

    again = _example(client)
    assert again["workspace"]["id"] == wid and again["created"] is False and again["added"] == []
    models = _models(client, wid)
    assert len(models) == 4
    assert models[demonstrator.PIPE_NAME]["source"].endswith("# operator edit\n")
    assert models[demonstrator.PIPE_NAME]["status"] == "draft"


def test_a_missing_artifact_is_completed_without_touching_the_others(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection

    wid = _example(client)["workspace"]["id"]
    before = _models(client, wid)
    with open_sqlite_connection() as connection:
        connection.execute("DELETE FROM studio_drafts WHERE model_spec_id = ?",
                           (before[demonstrator.SETTLER_NAME]["id"],))
        connection.commit()
    again = _example(client)
    assert again["added"] == [demonstrator.SETTLER_NAME]
    after = _models(client, wid)
    for name in (demonstrator.PIPE_NAME, demonstrator.SPOOL_NAME, demonstrator.PROCEDURE_NAME):
        assert after[name]["id"] == before[name]["id"]
        assert after[name]["updated_at"] == before[name]["updated_at"]


def test_runtime_unavailable_is_refused_before_anything_is_created(client: TestClient,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.workspaces.service import get_workspace_by_slug

    existing = get_workspace_by_slug(demonstrator.SLUG)
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "0")
    get_settings.cache_clear()
    refused = client.post("/studio/example-workspace")
    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "studio_runtime_unavailable"
    assert (get_workspace_by_slug(demonstrator.SLUG) is None) == (existing is None)


def test_browser_origin_outside_the_allowlist_is_refused(client: TestClient) -> None:
    refused = client.post("/studio/example-workspace", headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
