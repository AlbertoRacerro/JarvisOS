import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "tests/fixtures/loop_hydraulics_calc.py"
CONTRACT_PATH = ROOT / "tests/fixtures/loop_hydraulics.contract.json"


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.setenv("DATABASE_URL", "must-not-enter-runner")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def contract() -> dict[str, object]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def baseline() -> dict[str, dict[str, object]]:
    return {
        "tube_length": {"value": 20.0, "unit": "m"},
        "tube_inner_diameter": {"value": 30.0, "unit": "mm"},
        "tube_outer_diameter": {"value": 36.0, "unit": "mm"},
        "reservoir_liquid_volume": {"value": 5.0, "unit": "L"},
        "target_liquid_velocity": {"value": 0.25, "unit": "m/s"},
        "liquid_density": {"value": 1025.0, "unit": "kg/m3"},
        "dynamic_viscosity": {"value": 0.0011, "unit": "Pa*s"},
        "minor_loss_coefficient": {"value": 8.0, "unit": "1"},
        "pump_efficiency": {"value": 0.35, "unit": "1"},
    }


def create_implementation(client: TestClient, *, with_contract: bool = True) -> dict[str, object]:
    spec = client.post(
        "/workspaces/main/model-specs",
        json={
            "title": "Loop geometry and hydraulics V0",
            "engineering_question": "Screen geometry, hydraulics, and pump power.",
        },
    )
    assert spec.status_code == 201, spec.text
    payload: dict[str, object] = {
        "model_spec_id": spec.json()["id"],
        "version_label": "loop-hydraulics-v0",
        "implementation_kind": "calc_v0",
        "script_text": SCRIPT_PATH.read_text(encoding="utf-8"),
    }
    if with_contract:
        payload["input_contract"] = contract()
    response = client.post("/workspaces/main/model-implementations", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def preview(client: TestClient, implementation_id: str, bindings: dict[str, object]):
    return client.post(
        f"/workspaces/main/model-implementations/{implementation_id}/binding-preview",
        json={"bindings": bindings},
    )


def table_counts() -> dict[str, int]:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
            for table in ("runner_jobs", "simulation_runs", "artifacts", "parameters", "events")
        }


def test_contract_is_value_free_canonical_and_migrated(client: TestClient) -> None:
    implementation = create_implementation(client)
    stored = implementation["input_contract"]
    assert stored == contract()
    assert len(stored["variables"]) == 9
    encoded = json.dumps(stored, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert implementation["input_contract_sha256"] == hashlib.sha256(encoded.encode()).hexdigest()
    forbidden = {"value", "default", "recommended_value", "initial_guess"}
    assert all(not forbidden.intersection(variable) for variable in stored["variables"])

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(model_versions)")}
    assert {"input_contract_payload", "input_contract_sha256"}.issubset(columns)


def test_invalid_contract_and_legacy_implementation_behavior(client: TestClient) -> None:
    invalid = contract()
    invalid["variables"][0]["default"] = 20.0
    spec = client.post(
        "/workspaces/main/model-specs",
        json={"title": "Invalid contract", "engineering_question": "Reject hidden defaults."},
    )
    response = client.post(
        "/workspaces/main/model-implementations",
        json={
            "model_spec_id": spec.json()["id"],
            "implementation_kind": "calc_v0",
            "script_text": SCRIPT_PATH.read_text(encoding="utf-8"),
            "input_contract": invalid,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "runner_input_contract_invalid"

    legacy = create_implementation(client, with_contract=False)
    response = preview(client, legacy["id"], {})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "runner_input_contract_missing"


def test_preview_reports_honest_dof_without_side_effects(client: TestClient) -> None:
    implementation = create_implementation(client)
    before = table_counts()
    empty = preview(client, implementation["id"], {})
    assert empty.status_code == 200, empty.text
    assert empty.json()["state"] == "incomplete"
    assert empty.json()["structural_input_dof"] == 9
    assert empty.json()["bound_input_dof"] == 0
    assert empty.json()["unresolved_input_dof"] == 9
    assert empty.json()["normalized_input_set"] is None

    one = preview(client, implementation["id"], {"tube_length": baseline()["tube_length"]})
    assert one.status_code == 200
    assert one.json()["bound_input_dof"] == 1
    assert one.json()["unresolved_input_dof"] == 8

    ready = preview(client, implementation["id"], baseline())
    assert ready.status_code == 200
    assert ready.json()["state"] == "ready"
    assert ready.json()["bound_input_dof"] == 9
    assert ready.json()["unresolved_input_dof"] == 0
    assert ready.json()["normalized_input_set"] == baseline()
    assert table_counts() == before


@pytest.mark.parametrize(
    ("bindings", "error"),
    [
        ({"unknown": {"value": 1.0, "unit": "1"}}, "binding_unknown_variable:unknown"),
        ({"tube_length": {"value": True, "unit": "m"}}, "binding_value_invalid"),
        ({"tube_length": {"value": 20.0, "unit": "mm"}}, "binding_unit_invalid"),
        ({"tube_length": {"value": 0.0, "unit": "m"}}, "binding_domain_violation"),
    ],
)
def test_invalid_bindings_are_reported_not_executed(
    client: TestClient,
    bindings: dict[str, object],
    error: str,
) -> None:
    implementation = create_implementation(client)
    before = table_counts()
    response = preview(client, implementation["id"], bindings)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "invalid"
    combined = body["errors"] + [item for variable in body["variables"] for item in variable["errors"]]
    assert error in combined
    assert table_counts() == before


def test_parameter_binding_is_exact_and_workspace_scoped(client: TestClient) -> None:
    implementation = create_implementation(client)
    parameter = client.post(
        "/workspaces/main/parameters",
        json={"name": "Tube length", "value": "20", "unit": "m"},
    )
    assert parameter.status_code == 201
    bindings = baseline()
    bindings["tube_length"]["source_parameter_id"] = parameter.json()["id"]
    response = preview(client, implementation["id"], bindings)
    assert response.status_code == 200
    assert response.json()["state"] == "ready"
    tube = next(item for item in response.json()["variables"] if item["name"] == "tube_length")
    assert tube["binding_state"] == "parameter"

    bindings["tube_length"]["value"] = 21.0
    mismatch = preview(client, implementation["id"], bindings)
    assert mismatch.json()["state"] == "invalid"
    tube = next(item for item in mismatch.json()["variables"] if item["name"] == "tube_length")
    assert "binding_parameter_value_mismatch" in tube["errors"]


def test_two_ready_scenarios_run_through_existing_runner(client: TestClient) -> None:
    implementation = create_implementation(client)
    first_preview = preview(client, implementation["id"], baseline()).json()
    first_job = client.post(
        "/workspaces/main/runner-jobs",
        json={
            "model_version_id": implementation["id"],
            "run_label": "scenario-a",
            "input_set": first_preview["normalized_input_set"],
        },
    )
    assert first_job.status_code == 201, first_job.text
    first = client.post(f"/runner-jobs/{first_job.json()['runner_job']['id']}/run")
    assert first.status_code == 200
    assert first.json()["runner_job"]["status"] == "succeeded"

    second_bindings = baseline()
    second_bindings["tube_length"]["value"] = 24.0
    second_preview = preview(client, implementation["id"], second_bindings).json()
    second_job = client.post(
        "/workspaces/main/runner-jobs",
        json={
            "model_version_id": implementation["id"],
            "run_label": "scenario-b",
            "input_set": second_preview["normalized_input_set"],
        },
    )
    second = client.post(f"/runner-jobs/{second_job.json()['runner_job']['id']}/run")
    assert second.status_code == 200
    assert second.json()["simulation_run"]["id"] != first.json()["simulation_run"]["id"]
    first_volume = first.json()["output"]["outputs"]["tube_liquid_volume"]["value"]
    second_volume = second.json()["output"]["outputs"]["tube_liquid_volume"]["value"]
    assert second_volume > first_volume

    proposed = client.get("/workspaces/main/parameters").json()
    assert len(proposed) == 34
    assert all(item["status"] == "proposed" for item in proposed)


def test_contract_ready_can_still_fail_script_correlation(client: TestClient) -> None:
    implementation = create_implementation(client)
    bindings = baseline()
    bindings["target_liquid_velocity"]["value"] = 0.11
    preview_body = preview(client, implementation["id"], bindings).json()
    assert preview_body["state"] == "ready"
    reynolds = 1025.0 * 0.11 * 0.03 / 0.0011
    assert 2300.0 < reynolds < 4000.0
    job = client.post(
        "/workspaces/main/runner-jobs",
        json={
            "model_version_id": implementation["id"],
            "run_label": "transition-re",
            "input_set": preview_body["normalized_input_set"],
        },
    )
    result = client.post(f"/runner-jobs/{job.json()['runner_job']['id']}/run")
    assert result.status_code == 200
    assert result.json()["runner_job"]["status"] == "failed"
    assert result.json()["output"] is None
