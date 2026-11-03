"""Studio authoring evidence over the real spec-187 runtime."""

import hashlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.studio.derive import derive_documentation
from app.modules.studio.service import scaffold
from app.modules.studio_runtime.runtime import RunRequest, run, status


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


CONTRACT = {
    "name": "Pressure loss", "engineering_question": "How much pressure is lost?",
    "inputs": [{"name": "flow", "label": "Flow", "unit": "m**3/s", "description": "Volume flow"}],
    "outputs": [{"name": "loss", "label": "Pressure loss", "unit": "Pa", "description": "Pressure drop"}],
}


def _new(client):
    response = client.post("/workspaces/main/studio/models", json=CONTRACT)
    assert response.status_code == 201, response.text
    return response.json()


def _model_path(model):
    return f"/workspaces/main/studio/models/{model['id']}"


def test_scaffold_is_deterministic_and_placeholder_fails_in_real_sandbox(client):
    model = _new(client)
    source = model["source"]
    assert source == scaffold({"inputs": CONTRACT["inputs"], "outputs": CONTRACT["outputs"]})
    assert source == _new(client)["source"]
    record = run(RunRequest(source=source, inputs={"flow": 0.2}))
    assert record.classification == "error"
    assert "NotImplementedError" in record.stderr
    assert model["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()


def test_test_publish_revise_and_immutable_artifact(client):
    model = _new(client)
    path = _model_path(model)
    refused = client.post(path + "/publish")
    assert refused.status_code == 409
    source = model["source"].replace('    raise NotImplementedError("Complete evaluate before testing")',
                                     '    loss = flow * 10\n    return {"loss": loss}')
    changed = client.put(path, json={"source": source, "annotations": "Correlation assumption"})
    assert changed.status_code == 200, changed.text
    derived = changed.json()["documentation"]["derived"]
    assert derived["status"] == "ok" and derived["unrendered_count"] == 0
    assert [step["kind"] for step in derived["functions"][0]["steps"]] == ["equation"]
    case = {"inputs": {"flow": 0.2}, "expected_outputs": {"loss": 2.0}, "tolerance": 1e-9}
    report = client.post(path + "/test", json={"cases": [case]})
    assert report.status_code == 200 and report.json()["passed"], report.text
    published = client.post(path + "/publish")
    assert published.status_code == 200, published.text
    version = published.json()
    assert version["version_label"] == "Pressure loss v1"
    with open_sqlite_connection() as connection:
        row = connection.execute("""SELECT mv.*, a.stored_path, a.sha256 FROM model_versions mv
            JOIN artifacts a ON a.id = mv.implementation_artifact_id WHERE mv.id = ?""", (version["id"],)).fetchone()
        assert row["implementation_kind"] == "studio_calc_v1"
        assert row["status"] == "published"
        assert row["input_contract_payload"]
        assert Path(row["stored_path"]).read_text() == source
        assert row["sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert client.put(path, json={"source": source + "\n# edit"}).status_code == 409
    assert client.post(path + "/test", json={"cases": [case]}).status_code == 409
    revised = client.post(path + "/revise")
    assert revised.status_code == 200
    assert revised.json()["last_test_report"] is None
    assert client.post(path + "/revise").status_code == 409
    stale = client.put(path, json={"source": source + "\n# edit"})
    assert stale.status_code == 200 and stale.json()["annotations_stale"]
    assert client.post(path + "/publish").status_code == 409
    assert Path(row["stored_path"]).read_text() == source


def test_invalid_unit_documentation_safety_and_categories(client, tmp_path):
    bad = {**CONTRACT, "inputs": [{**CONTRACT["inputs"][0], "unit": "no_such_unit_688"}]}
    assert client.post("/workspaces/main/studio/models", json=bad).status_code == 422
    canary = tmp_path / "canary"
    source = (f"from pathlib import Path\nPath({str(canary)!r}).write_text('executed')\n"
              "def evaluate(x):\n    for item in x:\n        pass\n    return {'y': x}\n")
    docs = derive_documentation(source)
    assert not canary.exists()
    steps = docs["functions"][0]["steps"]
    assert steps[0]["kind"] == "code" and steps[0]["reason"] and docs["unrendered_count"] == 1
    quoted = {**CONTRACT, "inputs": [{**CONTRACT["inputs"][0], "unit": 'm"""'}]}
    assert client.post("/workspaces/main/studio/models", json=quoted).status_code == 422
    assert client.post("/workspaces/missing/studio/models", json=CONTRACT).status_code == 404
    categories = client.get("/studio/categories").json()
    assert categories[0]["available"] is True
    assert [item["id"] for item in categories if item["available"]] == [
        "calculation", "process_unit", "procedure", "geometry"]
    assert all(category["available"] or category["reason"] for category in categories)


def test_report_is_hash_bound_and_outputs_are_checked(client):
    model = _new(client)
    path = _model_path(model)
    source = model["source"].replace('    raise NotImplementedError("Complete evaluate before testing")',
                                     '    return {"wrong": flow}')
    assert client.put(path, json={"source": source}).status_code == 200
    case = {"inputs": {"flow": 0.2}}
    report = client.post(path + "/test", json={"cases": [case]}).json()
    assert report["passed"] is False
    assert "Every declared output" in report["cases"][0]["errors"][0]
    assert client.post(path + "/publish").status_code == 409
    fixed = source.replace('"wrong"', '"loss"')
    assert client.put(path, json={"source": fixed}).json()["last_test_report"] is None
    assert client.post(path + "/test", json={"cases": [case]}).json()["passed"]
    assert client.put(path, json={"source": fixed + "\n# changed"}).json()["last_test_report"] is None
    assert client.post(path + "/publish").status_code == 409
