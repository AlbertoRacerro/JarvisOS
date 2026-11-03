"""Workshop AI drafts (#920 item 3): proposal-only, deterministic fallback, validated, no Studio writes."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.studio import workshop_draft

URL = "/workspaces/main/studio/workshop-draft"
VARIABLES = {
    "inputs": [{"name": "flow", "label": "Flow", "unit": "m**3/s", "description": "Volume flow", "min": None}],
    "outputs": [{"name": "loss", "label": "Pressure loss", "unit": "Pa", "description": "Pressure drop"}],
}
BODY = {"category": "calculation", "step": 3, "name": "Pressure loss", "engineering_question": "How much?",
        "approach": "Darcy-Weisbach", **VARIABLES}
GOOD = ("def evaluate(flow: float) -> dict[str, float]:\n    return {'loss': flow * 2.0}\n\n\n"
        "def main(inputs: dict[str, float]) -> dict[str, float]:\n    return evaluate(flow=inputs['flow'])\n")


@pytest.fixture
def client():
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _fake(monkeypatch, text, status="success"):
    response = SimpleNamespace(text=text) if text is not None else None
    outcome = SimpleNamespace(status=status, ledger_id="job1", response=response, error_type=None,
                              decision=SimpleNamespace(blocked_reason=None))
    monkeypatch.setattr(workshop_draft, "run_ai_task", lambda **kwargs: outcome)


def _count(table):
    with open_sqlite_connection() as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_default_offline_route_falls_back_to_deterministic_scaffold(client):
    body = client.post(URL, json=BODY).json()
    assert body["origin"] == "deterministic" and body["acceptable"] and body["contract_ready"]
    assert "AI output refused" in body["reason"] or "unavailable" in body["reason"]
    assert "def main" in body["source"] and "Approach: Darcy-Weisbach" in body["explanation"]


def test_valid_ai_output_is_shown_as_ai_draft_and_nothing_is_persisted(client, monkeypatch):
    _fake(monkeypatch, json.dumps({"source": GOOD, "explanation": "Linear placeholder."}))
    before = (_count("studio_drafts"), _count("model_specs"))
    body = client.post(URL, json=BODY).json()
    assert body["origin"] == "ai" and body["reason"] is None and body["source"] == GOOD
    assert body["explanation"] == "Linear placeholder." and body["contract"]["inputs"][0]["name"] == "flow"
    assert (_count("studio_drafts"), _count("model_specs")) == before


@pytest.mark.parametrize("text", ["not json", json.dumps({"source": "def nope(:", "explanation": "x"}),
                                  json.dumps({"source": "x = 1\n", "explanation": "no main"}),
                                  json.dumps({"source": 3, "explanation": "x"})])
def test_invalid_ai_output_is_refused_and_falls_back(client, monkeypatch, text):
    _fake(monkeypatch, text)
    body = client.post(URL, json=BODY).json()
    assert body["origin"] == "deterministic" and body["reason"].startswith("AI output refused")
    assert body["source"] != GOOD and "def main" in body["source"]


def test_refused_or_over_budget_route_falls_back_with_reason(client, monkeypatch):
    _fake(monkeypatch, None, status="route_unavailable")
    body = client.post(URL, json=BODY).json()
    assert body["origin"] == "deterministic" and body["reason"].startswith("AI route unavailable")


def test_incomplete_contract_and_early_steps_do_not_call_ai(client, monkeypatch):
    monkeypatch.setattr(workshop_draft, "run_ai_task", lambda **kwargs: pytest.fail("AI must not be called"))
    early = client.post(URL, json={"category": "calculation", "step": 0}).json()
    assert early["origin"] == "deterministic" and not early["contract_ready"] and early["source"] == ""
    no_approach = client.post(URL, json={**BODY, "approach": ""}).json()
    assert no_approach["contract_ready"] and "def main" in no_approach["source"]


def test_other_categories(client, monkeypatch):
    monkeypatch.setattr(workshop_draft, "run_ai_task", lambda **kwargs: pytest.fail("AI must not be called"))
    procedure = client.post(URL, json={"category": "procedure", "step": 3, "name": "p"}).json()
    assert procedure["source"] == "" and not procedure["acceptable"]
    unit = client.post(URL, json={"category": "process_unit", "name": "u", "engineering_question": "q",
                                  "inlets": [{"name": "feed", "label": "Feed"}],
                                  "outlets": [{"name": "out", "label": "Out"}]}).json()
    assert unit["contract_ready"] and "def evaluate(inlets" in unit["source"]
    geometry = client.post(URL, json={"category": "geometry", "name": "g", "engineering_question": "q",
                                      "inputs": VARIABLES["inputs"]}).json()
    assert geometry["contract_ready"] and "def build(" in geometry["source"]
