"""Published Studio calculations run through the existing runner flow behind the real sandbox."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.modules.studio_runtime import runtime

PLACEHOLDER = '    raise NotImplementedError("Complete evaluate before testing")'


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    if os.getenv("JARVISOS_STUDIO_CI_SANDBOX_UNAVAILABLE") == "1":
        pytest.skip("CI runner does not permit bubblewrap user namespaces; WSL2 qualification is required")
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    assert runtime.status()["available"], "qualification host must run bubblewrap"
    initialize_storage(seed_default=True)
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _draft(client: TestClient, unit: str) -> str:
    created = client.post("/workspaces/main/studio/models", json={
        "name": "Studio pressure", "engineering_question": "Compute pressure loss.",
        "inputs": [{"name": "driver", "label": "Driver", "unit": unit, "description": "Driving quantity"}],
        "outputs": [{"name": "pressure_loss", "label": "Pressure loss", "unit": "Pa", "description": "Loss"}],
    })
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _publish(client: TestClient, model_id: str, factor: int = 2) -> str:
    path = f"/workspaces/main/studio/models/{model_id}"
    source = client.get(path).json()["source"]
    body = f'    return {{"pressure_loss": driver * {factor}}}'
    source = source.replace(PLACEHOLDER, body) if PLACEHOLDER in source else source.replace("driver * 2", f"driver * {factor}")
    assert client.put(path, json={"source": source}).status_code == 200
    report = client.post(path + "/test", json={"cases": [{"inputs": {"driver": 1.0}}]})
    assert report.status_code == 200 and report.json()["passed"], report.text
    published = client.post(path + "/publish")
    assert published.status_code == 200, published.text
    return published.json()["id"]


def _script_path(version_id: str) -> Path:
    with open_sqlite_connection() as connection:
        row = connection.execute("""SELECT a.stored_path FROM artifacts a JOIN model_versions mv
            ON mv.implementation_artifact_id = a.id WHERE mv.id = ?""", (version_id,)).fetchone()
    return Path(row["stored_path"])


def _job(client: TestClient, version_id: str, binding: dict, **extra):
    return client.post("/workspaces/main/runner-jobs", json={
        "model_version_id": version_id, "input_set": {"driver": binding}, **extra,
    })


def test_published_version_is_listed_and_runs_only_in_the_isolated_runtime(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_id = _publish(client, _draft(client, "m"))
    listed = client.get("/workspaces/main/model-implementations").json()
    assert any(item["id"] == version_id and item["implementation_kind"] == "studio_calc_v1" for item in listed)
    monkeypatch.setattr(
        "app.modules.runner.service.execute_python_script",
        lambda **_: pytest.fail("Unisolated runner must not execute Studio source"),
    )
    created = _job(client, version_id, {"value": 3, "unit": "m"})
    assert created.status_code == 201, created.text
    job_id = created.json()["runner_job"]["id"]
    result = client.post(f"/runner-jobs/{job_id}/run")
    assert result.status_code == 200, result.text
    assert result.json()["runner_job"]["status"] == "succeeded"
    assert result.json()["output"]["outputs"]["pressure_loss"] == {"value": 6.0, "unit": "Pa"}
    with open_sqlite_connection() as connection:
        proposal = connection.execute(
            "SELECT unit FROM parameters WHERE source_ref = ?", (f"runner_job:{job_id}",)
        ).fetchone()
    assert proposal is not None and proposal["unit"] == "Pa"


def test_hash_mismatch_disabled_runtime_and_other_kinds_fail_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_id = _publish(client, _draft(client, "m"))
    path = _script_path(version_id)
    original = path.read_text(encoding="utf-8")
    path.write_text("def main(inputs): return {'pressure_loss': 0}\n", encoding="utf-8")
    denied = _job(client, version_id, {"value": 3, "unit": "m"})
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "runner_script_hash_mismatch"
    path.write_text(original, encoding="utf-8")

    with open_sqlite_connection() as connection:
        version = dict(connection.execute("SELECT * FROM model_versions WHERE id = ?", (version_id,)).fetchone())
        other_id = str(uuid4())
        version.update(id=other_id, implementation_kind="studio_calc_v2", version_label="other")
        connection.execute(
            f"INSERT INTO model_versions ({', '.join(version)}) VALUES ({', '.join('?' for _ in version)})",
            tuple(version.values()),
        )
        connection.commit()
    denied = _job(client, other_id, {"value": 3, "unit": "m"})
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "RUNNER_SCRIPT_POLICY_VIOLATION"

    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "0")
    get_settings.cache_clear()
    denied = _job(client, version_id, {"value": 3, "unit": "m"})
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "studio_runtime_unavailable"


def test_parameter_binding_dimension_stale_and_superseded(client: TestClient) -> None:
    model_id = _draft(client, "K")
    version_id = _publish(client, model_id)
    wrong = client.post("/workspaces/main/parameters", json={"name": "Length", "value": "10", "unit": "m"})
    assert wrong.status_code == 201, wrong.text
    denied = _job(client, version_id, {"value": 10, "unit": "m", "source_parameter_id": wrong.json()["id"]})
    assert denied.status_code == 400, denied.text

    parameter = client.post("/workspaces/main/parameters", json={
        "name": "Temperature", "value": "10", "unit": "degC",
    })
    assert parameter.status_code == 201, parameter.text
    parameter_id = parameter.json()["id"]
    binding = {"value": 10, "unit": "degC", "source_parameter_id": parameter_id}
    created = _job(client, version_id, binding, request_key="studio-celsius-1")
    assert created.status_code == 201, created.text
    replayed = _job(client, version_id, binding, request_key="studio-celsius-1")
    assert replayed.status_code == 201
    assert replayed.json()["runner_job"]["id"] == created.json()["runner_job"]["id"]
    assert json.loads(created.json()["simulation_run"]["input_payload"])["driver"]["value"] == pytest.approx(283.15)
    result = client.post(f"/runner-jobs/{created.json()['runner_job']['id']}/run")
    assert result.status_code == 200, result.text
    assert result.json()["output"]["outputs"]["pressure_loss"]["value"] == pytest.approx(566.3)

    run_path = f"/workspaces/main/simulation-runs/{created.json()['simulation_run']['id']}"
    before = client.get(run_path).json()
    assert before["stale"] is False and before["superseded"] is False
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE parameters SET value = '12' WHERE id = ?", (parameter_id,))
        connection.commit()
    assert client.post(f"/workspaces/main/studio/models/{model_id}/revise").status_code == 200
    _publish(client, model_id, factor=3)
    after = client.get(run_path).json()
    assert after["stale"] is True and after["superseded"] is True
    assert {key: after[key] for key in ("input_payload", "output_payload", "model_version_id", "status")} == {
        key: before[key] for key in ("input_payload", "output_payload", "model_version_id", "status")
    }
