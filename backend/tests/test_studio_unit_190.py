"""Studio process-unit authoring evidence (spec 190) over the real spec-187 runtime."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.studio import service
from app.modules.studio.symbol import SymbolError, sanitize_symbol
from app.modules.studio.unit_checks import UnitCheckError, check_result
from app.modules.studio_runtime.runtime import status
from tests.studio_unit_190_support import DOSING_CONTRACT, MIX_BODY, PLACEHOLDER, stream, unit_case


@pytest.fixture
def client(monkeypatch):
    if os.getenv("JARVISOS_STUDIO_CI_SANDBOX_UNAVAILABLE") == "1":
        pytest.skip("CI runner does not permit bubblewrap user namespaces; WSL2 qualification is required")
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    assert status()["available"], "qualification host must run bubblewrap"
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _create(client, **changes):
    return client.post("/workspaces/main/studio/models", json={**DOSING_CONTRACT, **changes})


def test_unit_scaffold_is_deterministic_and_placeholder_fails_in_the_sandbox(client):
    first, second = _create(client).json(), _create(client).json()
    assert first["category"] == "process_unit" and first["source"] == second["source"]
    assert "def evaluate(inlets: dict, params: dict) -> dict:" in first["source"]
    assert "dose_split [percent]" in first["source"] and "culture: Culture" in first["source"]
    report = client.post(f"/workspaces/main/studio/models/{first['id']}/test", json={"cases": [unit_case()]}).json()
    assert report["passed"] is False and report["cases"][0]["codes"] == ["STUDIO_UNIT_FAILED"]
    assert "NotImplementedError" in report["cases"][0]["errors"][0]
    categories = client.get("/studio/categories").json()
    assert {"id": "process_unit", "label": "Process unit", "available": True, "reason": None} in categories


@pytest.mark.parametrize("changes", [
    {"inlets": []}, {"inlets": [{"name": f"i{n}", "label": "x"} for n in range(5)]},
    {"outlets": [{"name": "a", "label": "x"}, {"name": "a", "label": "y"}]},
    {"inlets": [{"name": "not valid", "label": "x"}]}, {"inlets": [{"name": "class", "label": "x"}]},
    {"inlets": [{"name": "a", "label": 'x"""'}]},
    {"culture_generation": ["unknown"]}, {"culture_generation": ["nitrogen", "nitrogen"]},
    {"parameters": [{**DOSING_CONTRACT["parameters"][0], "unit": "no_such_unit_190"}]},
    {"symbol": "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"},
])
def test_unit_contract_is_validated(client, changes):
    assert _create(client, **changes).status_code == 422


@pytest.mark.parametrize("hostile", [
    "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg' onload='x()'><rect width='1' height='1'/></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><rect width='1' height='1' onclick='x()'/></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><image href='http://evil/x.png'/></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'><use xlink:href='#a'/></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><foreignObject><div/></foreignObject></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><rect style='fill:url(http://evil)' width='1'/></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><rect fill='url(#a)' width='1'/></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0 javascript:alert(1)'/></svg>",
    "<!DOCTYPE svg [<!ENTITY a 'b'>]><svg xmlns='http://www.w3.org/2000/svg'/>",
    "<svg xmlns='http://www.w3.org/2000/svg'><!-- c --></svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><a href='javascript:x'/></svg>",
    "<html/>", "not xml", "<svg xmlns='http://www.w3.org/2000/svg'>" + "<g>" * 20 + "</g>" * 20 + "</svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><title>" + "a" * 40000 + "</title></svg>",
    "<svg xmlns='http://example.com/other'><rect width='1'/></svg>",
])
def test_symbol_sanitizer_refuses_hostile_input(hostile):
    with pytest.raises(SymbolError):
        sanitize_symbol(hostile)


def test_symbol_sanitizer_keeps_a_safe_symbol_canonical_and_the_route_refuses_the_rest(client):
    safe = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'><rect x='2' y='2' width='36' height='36' "
            "fill='none' stroke='#123456'/><text x='8' y='24'>Dose &amp; mix</text></svg>")
    clean = sanitize_symbol(safe)
    assert sanitize_symbol(clean) == clean and "Dose &amp; mix" in clean and "script" not in clean
    model = _create(client, symbol=safe).json()
    assert model["symbol"] == clean
    path = f"/workspaces/main/studio/models/{model['id']}"
    assert client.put(path, json={"symbol": "<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"}).status_code == 422
    assert client.put(path, json={"name": "Renamed"}).json()["symbol"] == clean
    assert client.put(path, json={"symbol": None}).json()["symbol"] is None


def _inlets(**flows):
    return {"culture": stream(flows.get("a", 1.0)), "dose": stream(flows.get("b", 0.5))}


def _outlet(**changes):
    base = {"mass_flow_kg_s": 1.5, "temperature_K": 300.0, "pressure_Pa": 200000.0,
            "mass_fractions": {"Water": 1.0}, "culture": {"biomass": 0.0, "nitrogen": 0.001}}
    return {"outlets": {"outlet": {**base, **changes}}}


def _check(result, generation=()):
    return check_result(result, inlets=_inlets(), outlet_names=["outlet"], compounds=["Water"],
                        generation=list(generation))


@pytest.mark.parametrize("result,code", [
    (_outlet(), None),
    ({"outlets": {"other": _outlet()["outlets"]["outlet"]}}, "STUDIO_UNIT_OUTLETS_MISMATCH"),
    ({"outlets": {}}, "STUDIO_UNIT_OUTLETS_MISMATCH"),
    ([1], "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(temperature_K=0.0), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(pressure_Pa=-1.0), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(mass_flow_kg_s=float("nan")), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(mass_flow_kg_s=True), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(mass_fractions={"Water": 0.9}), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(mass_fractions={"Water": 1.5, "Salt": -0.5}), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(mass_fractions={"Water": 0.5, "Ethanol": 0.5}), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(extra=1), "STUDIO_UNIT_STATE_INVALID"),
    (_outlet(mass_flow_kg_s=1.6), "STUDIO_UNIT_MASS_IMBALANCE"),
    (_outlet(culture={"biomass": 0.0, "nitrogen": 0.002}), "STUDIO_UNIT_CULTURE_IMBALANCE"),
    ({**_outlet(), "culture_generation": {"nitrogen": 1e-6}}, "STUDIO_UNIT_CULTURE_IMBALANCE"),
])
def test_host_checks_name_each_violation(result, code):
    if code is None:
        assert _check(result).outlets["outlet"]["mass_flow_kg_s"] == 1.5
        return
    with pytest.raises(UnitCheckError) as caught:
        _check(result)
    assert caught.value.code == code


def test_host_checks_component_imbalance_and_declared_generation():
    inlets = {"a": {**stream(1.0), "mass_fractions": {"Water": 0.5, "Salt": 0.5}}}
    swapped = {"outlets": {"o": {**_outlet()["outlets"]["outlet"], "mass_flow_kg_s": 1.0,
                                 "mass_fractions": {"Water": 0.6, "Salt": 0.4}}}}
    with pytest.raises(UnitCheckError) as caught:
        check_result(swapped, inlets=inlets, outlet_names=["o"], compounds=["Water", "Salt"], generation=[])
    assert caught.value.code == "STUDIO_UNIT_COMPONENT_IMBALANCE"
    trace_inlet = {**stream(1.0), "mass_fractions": {"Water": 1.0 - 1e-6, "Ethanol": 1e-6}}
    trace_outlet = {**trace_inlet, "mass_fractions": {"Water": 1.0 - 1.0001e-6, "Ethanol": 1.0001e-6}}
    with pytest.raises(UnitCheckError) as caught:
        check_result({"outlets": {"o": trace_outlet}}, inlets={"a": trace_inlet}, outlet_names=["o"],
                     compounds=["Water", "Ethanol"], generation=[])
    assert caught.value.code == "STUDIO_UNIT_COMPONENT_IMBALANCE"
    gained = _outlet(culture={"biomass": 0.0, "nitrogen": 0.001 + 1e-6 / 1.5})
    gained["culture_generation"] = {"nitrogen": 1e-6}
    checked = _check(gained, generation=["nitrogen"])
    assert checked.culture_generation == {"nitrogen": 1e-6}
    with pytest.raises(UnitCheckError) as caught:
        _check(_outlet(culture={"biomass": 0.0, "nitrogen": None}))
    assert caught.value.code == "STUDIO_UNIT_CULTURE_IMBALANCE"
    with pytest.raises(UnitCheckError) as caught:
        _check(_outlet(culture={"biomass": 0.0}))
    assert caught.value.code == "STUDIO_UNIT_CULTURE_IMBALANCE"
    with pytest.raises(UnitCheckError) as caught:
        _check(gained | {"culture_generation": {}}, generation=["nitrogen"])
    assert caught.value.code == "STUDIO_UNIT_CULTURE_IMBALANCE"
    with pytest.raises(UnitCheckError) as caught:
        _check(gained | {"culture_generation": {"nitrogen": 2e-6}}, generation=["nitrogen"])
    assert caught.value.code == "STUDIO_UNIT_CULTURE_IMBALANCE"
    unknown_inlets = {name: {**inlet, "culture": {"nitrogen": None}} for name, inlet in _inlets().items()}
    unknown_outlet = _outlet(culture={"nitrogen": None})
    assert check_result(unknown_outlet, inlets=unknown_inlets, outlet_names=["outlet"],
                        compounds=["Water"], generation=[]).outlets["outlet"]["culture"]["nitrogen"] is None
    mixed_unknown = {"known": stream(1.0, nitrogen=0.1),
                     "unknown": {**stream(1.0), "culture": {"nitrogen": None}}}
    propagated = {"outlets": {"o": {**stream(2.0), "culture": {"nitrogen": None}}}}
    assert check_result(propagated, inlets=mixed_unknown, outlet_names=["o"],
                        compounds=["Water"], generation=[]).outlets["o"]["culture"]["nitrogen"] is None
    with pytest.raises(UnitCheckError) as caught:
        check_result({"outlets": {"o": stream(2.0)}}, inlets=mixed_unknown, outlet_names=["o"],
                     compounds=["Water"], generation=[])
    assert caught.value.code == "STUDIO_UNIT_CULTURE_IMBALANCE"
    zero_unknown = {"zero": {**stream(0.0), "culture": {"nitrogen": None}}, "live": stream(1.0)}
    assert check_result({"outlets": {"o": stream(1.0)}}, inlets=zero_unknown, outlet_names=["o"],
                        compounds=["Water"], generation=[]).outlets["o"]["mass_flow_kg_s"] == 1.0
    zero = check_result({"outlets": {"o": {**_outlet(mass_flow_kg_s=0.0)["outlets"]["outlet"]}}},
                        inlets={"a": stream(0.0)}, outlet_names=["o"], compounds=["Water"], generation=[])
    assert zero.outlets["o"]["mass_flow_kg_s"] == 0.0


def test_unit_test_report_publish_gate_immutability_and_hash_check(client):
    model = _create(client).json()
    path = f"/workspaces/main/studio/models/{model['id']}"
    assert client.post(path + "/publish").status_code == 409
    source = model["source"].replace(PLACEHOLDER, MIX_BODY)
    assert client.put(path, json={"source": source}).status_code == 200
    wrong_kind = client.post(path + "/test", json={"cases": [{"inputs": {"x": 1.0}}]})
    assert wrong_kind.status_code == 422
    expected = {"outlet": {"mass_flow_kg_s": 1.5, "temperature_K": 296.6666666666667}}
    good = client.post(path + "/test", json={"cases": [unit_case(expected_outlets=expected, tolerance=1e-9)]}).json()
    assert good["passed"], good
    assert good["cases"][0]["culture_generation"] == {"nitrogen": pytest.approx(5e-7)}
    off = client.post(path + "/test", json={"cases": [unit_case(expected_outlets={"outlet": {"temperature_K": 1.0}})]})
    assert off.json()["passed"] is False
    assert client.post(path + "/publish").status_code == 409
    assert client.post(path + "/test", json={"cases": [unit_case()]}).json()["passed"]
    published = client.post(path + "/publish").json()
    assert published["category"] == "process_unit" and published["version_label"] == "Nutrient dosing v1"
    assert client.put(path, json={"source": source + "\n#"}).status_code == 409  # revision required
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT implementation_kind, input_contract_payload FROM model_versions WHERE id = ?",
                                 (published["id"],)).fetchone()
    assert row["implementation_kind"] == "studio_unit_v1" and row["input_contract_payload"] is None
    loaded = service.published_unit("main", published["id"])
    assert loaded["source_sha256"] == published["source_sha256"] and loaded["source"] == source
    assert client.get("/workspaces/main/studio/library").json()[0]["category"] == "process_unit"
    from pathlib import Path

    with open_sqlite_connection() as connection:
        stored = Path(connection.execute("""SELECT a.stored_path FROM artifacts a JOIN model_versions mv
            ON mv.implementation_artifact_id = a.id WHERE mv.id = ?""", (published["id"],)).fetchone()[0])
    stored.write_text(source + "\n# tampered", encoding="utf-8")
    with pytest.raises(service.StudioError) as caught:
        service.published_unit("main", published["id"])
    assert caught.value.code == "studio_artifact_hash_mismatch"
    assert client.post(path + "/revise").status_code == 409


def test_unit_test_reports_mass_imbalance_and_bad_cases(client):
    model = _create(client).json()
    path = f"/workspaces/main/studio/models/{model['id']}"
    leaky = MIX_BODY.replace('"mass_flow_kg_s": m,', '"mass_flow_kg_s": m * 0.5,')
    assert client.put(path, json={"source": model["source"].replace(PLACEHOLDER, leaky)}).status_code == 200
    report = client.post(path + "/test", json={"cases": [unit_case()]}).json()
    assert report["cases"][0]["codes"] == ["STUDIO_UNIT_MASS_IMBALANCE"] and not report["passed"]
    out_of_bounds = unit_case(params={"dose_split": 500.0})
    bad = client.post(path + "/test", json={"cases": [out_of_bounds]}).json()
    assert bad["cases"][0]["classification"] == "invalid_case"
