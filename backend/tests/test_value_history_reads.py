from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.project_knowledge.models import (
    SimulationResultResolveRequest,
)
from app.modules.project_knowledge.sim_result_versions import (
    backfill_simulation_result_value_versions,
)
from app.modules.project_knowledge.sim_results import (
    list_simulation_result_batches,
    resolve_simulation_result_exception,
)
from app.modules.runner.models import ModelImplementationCreate, RunnerJobCreate
from app.modules.runner.service import (
    create_model_implementation,
    create_runner_job,
    run_runner_job,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    get_settings.cache_clear()
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _seed_parameters_and_decision(workspace_id: str = "main") -> None:
    now = "2026-11-06T00:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO parameters (
                id, workspace_id, name, value, unit, value_status, value_min, value_max,
                status, origin, created_at, updated_at, lifecycle_state
            ) VALUES
                ('param-pressure', ?, 'coolant_pressure', '12.0', 'MPa', 'accepted', 5.0, 20.0, 'accepted', 'human', ?, ?, 'active'),
                ('param-heat', ?, 'heat_flux', '8.0', 'MW/m^2', 'validated', 1.0, 10.0, 'accepted', 'human', ?, ?, 'active'),
                ('param-flow', ?, 'mass_flow', '45.0', 'kg/s', 'validated', 10.0, 100.0, 'accepted', 'human', ?, ?, 'active'),
                ('param-length-v0', ?, 'tube_length', '10.0', 'm', 'accepted', 1.0, 50.0, 'accepted', 'human', ?, ?, 'active')
            """,
            (workspace_id, now, now, workspace_id, now, now, workspace_id, now, now, workspace_id, now, now),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO decisions (
                id, workspace_id, title, decision_text, rationale, status, origin,
                created_at, updated_at, basis_lifecycle_state
            ) VALUES (
                'dec-pressure', ?, 'Coolant Operating Pressure Decision',
                'coolant_pressure=12.0 MPa',
                'Fix primary loop pressure at 12.0 MPa.', 'accepted', 'human', ?, ?, 'active'
            )
            """,
            (workspace_id, now, now),
        )
        connection.commit()


def _create_v2_model_and_run(
    client: TestClient,
    workspace_id: str = "main",
    parameter_id: str = "param-length-v0",
) -> tuple[str, str]:
    """Create a runner job with schema-v2 contract and linked parameter, then run it."""
    spec_res = client.post(
        f"/workspaces/{workspace_id}/model-specs",
        json={"title": "V2 Spec", "engineering_question": "Test manifest."},
    )
    assert spec_res.status_code == 201
    spec_id = spec_res.json()["id"]

    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({'outputs': {'output_metric': {'value': 42.0, 'unit': 'm', 'quantity': 'length'}}}, f)
""".strip()

    v2_contract = {
        "schema_version": 2,
        "evaluation_mode": "forward",
        "variables": [
            {
                "name": "tube_length",
                "label": "Tube Length",
                "unit": "m",
                "physical_dimension": "length",
                "required": True,
                "category": "design",
                "description": "Length of tube.",
            }
        ],
    }

    impl = create_model_implementation(
        workspace_id,
        ModelImplementationCreate(
            model_spec_id=spec_id,
            version_label="impl-v2",
            implementation_kind="calc_v0",
            script_text=script,
            input_contract=v2_contract,
        ),
    )

    job_created = create_runner_job(
        workspace_id,
        RunnerJobCreate(
            model_version_id=impl.id,
            input_set={
                "tube_length": {
                    "value": 10.0,
                    "unit": "m",
                    "source_parameter_id": parameter_id,
                    "source_parameter_updated_at": "2026-11-06T00:00:00+00:00",
                }
            },
        ),
    )

    run_res = run_runner_job(job_created.runner_job.id)
    assert run_res.error is None
    sim_run_id = job_created.runner_job.simulation_run_id
    return spec_id, sim_run_id


def test_run_manifest_canonical_linked_parameters_preserves_snapshot_on_supersede(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    _, sim_run_id = _create_v2_model_and_run(client, "main", "param-length-v0")

    # 1. Fetch manifest initially
    res = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "recorded"
    assert data["history_recorded"] is True
    assert data["history_not_recorded"] is False
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["id"] == "param-length-v0"
    assert item["name"] == "tube_length"
    assert item["value"] == 10.0
    assert item["unit"] == "m"
    assert item["status"] == "accepted"
    assert item["position"] == 0
    assert item["newer_version_exists"] is False

    # 2. Supersede param-length-v0 with param-length-v1
    now = "2026-11-07T00:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE parameters
            SET status = 'superseded', lifecycle_state = 'superseded', updated_at = ?
            WHERE id = 'param-length-v0'
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                supersedes_parameter_id, origin, created_at, updated_at
            ) VALUES (
                'param-length-v1', 'main', 'tube_length', '15.0', 'm', 'accepted', 'active',
                'param-length-v0', 'human', ?, ?
            )
            """,
            (now, now),
        )
        connection.commit()

    # 3. Fetch manifest again: run snapshot (10.0 m) MUST be preserved!
    # Status is updated and newer_version_exists is True.
    res2 = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "recorded"
    assert len(data2["items"]) == 1
    item2 = data2["items"][0]
    assert item2["id"] == "param-length-v0"
    assert item2["value"] == 10.0  # Preserved snapshot! Not mutated to 15.0
    assert item2["unit"] == "m"
    assert item2["status"] == "superseded"
    assert item2["position"] == 0
    assert item2["newer_version_exists"] is True


def test_run_manifest_historical_schema_v1_returns_history_not_recorded(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    spec_res = client.post(
        "/workspaces/main/model-specs",
        json={"title": "V1 Spec", "engineering_question": "Historical test."},
    )
    spec_id = spec_res.json()["id"]

    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({'outputs': {'output_metric': {'value': 42.0, 'unit': 'm', 'quantity': 'length'}}}, f)
""".strip()

    # Schema version 1 contract: historical, does not require canonical linked parameters
    v1_contract = {
        "schema_version": 1,
        "evaluation_mode": "forward",
        "variables": [
            {
                "name": "tube_length",
                "label": "Tube Length",
                "unit": "m",
                "required": True,
                "category": "design",
                "description": "Length of tube.",
            }
        ],
    }

    impl = create_model_implementation(
        "main",
        ModelImplementationCreate(
            model_spec_id=spec_id,
            version_label="impl-v1",
            implementation_kind="calc_v0",
            script_text=script,
            input_contract=v1_contract,
        ),
    )

    job_created = create_runner_job(
        "main",
        RunnerJobCreate(
            model_version_id=impl.id,
            input_set={
                "tube_length": {
                    "value": 10.0,
                    "unit": "m",
                    "source_parameter_id": "param-length-v0",
                }
            },
        ),
    )
    run_res = run_runner_job(job_created.runner_job.id)
    assert run_res.error is None
    sim_run_id = job_created.runner_job.simulation_run_id

    # Manifest read: MUST return history_not_recorded with reason and never guess!
    res = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "history_not_recorded"
    assert data["history_not_recorded"] is True
    assert data["history_recorded"] is False
    assert "schema v1" in data["reason"]
    assert data["items"] == []


def test_run_manifest_manual_inputs_only_returns_recorded_empty_items(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    spec_res = client.post(
        "/workspaces/main/model-specs",
        json={"title": "Manual Spec", "engineering_question": "Manual inputs."},
    )
    spec_id = spec_res.json()["id"]

    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({'outputs': {'output_metric': {'value': 42.0, 'unit': 'm', 'quantity': 'length'}}}, f)
""".strip()

    v2_contract = {
        "schema_version": 2,
        "evaluation_mode": "forward",
        "variables": [
            {
                "name": "tube_length",
                "label": "Tube Length",
                "unit": "m",
                "physical_dimension": "length",
                "required": True,
                "category": "design",
                "description": "Length of tube.",
            }
        ],
    }

    impl = create_model_implementation(
        "main",
        ModelImplementationCreate(
            model_spec_id=spec_id,
            version_label="impl-v2-manual",
            implementation_kind="calc_v0",
            script_text=script,
            input_contract=v2_contract,
        ),
    )

    # Manual input without source_parameter_id
    job_created = create_runner_job(
        "main",
        RunnerJobCreate(
            model_version_id=impl.id,
            input_set={"tube_length": {"value": 10.0, "unit": "m"}},
        ),
    )
    run_runner_job(job_created.runner_job.id)
    sim_run_id = job_created.runner_job.simulation_run_id

    res = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "recorded"
    assert data["history_recorded"] is True
    assert data["items"] == []


def test_parameter_version_chain_returns_ordered_lineage(client: TestClient) -> None:
    now0 = "2026-11-01T00:00:00+00:00"
    now1 = "2026-11-02T00:00:00+00:00"
    now2 = "2026-11-03T00:00:00+00:00"

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                origin, notes, source_ref, created_at, updated_at
            ) VALUES (
                'param-p0', 'main', 'density', '1000.0', 'kg/m^3', 'superseded', 'superseded',
                'human', 'Initial density', 'lab-handbook', ?, ?
            )
            """,
            (now0, now0),
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                supersedes_parameter_id, origin, notes, source_ref, created_at, updated_at
            ) VALUES (
                'param-p1', 'main', 'density', '1025.0', 'kg/m^3', 'superseded', 'superseded',
                'param-p0', 'human', 'Measured at 20C', 'exp-run-1', ?, ?
            )
            """,
            (now1, now1),
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                supersedes_parameter_id, origin, notes, source_ref, created_at, updated_at
            ) VALUES (
                'param-p2', 'main', 'density', '1027.5', 'kg/m^3', 'accepted', 'active',
                'param-p1', 'human', 'Validated final density', 'exp-run-2', ?, ?
            )
            """,
            (now2, now2),
        )
        connection.commit()

    # Query from any parameter in the chain (e.g. middle parameter param-p1)
    res = client.get("/project-knowledge/workspaces/main/parameters/param-p1/history")
    assert res.status_code == 200
    data = res.json()
    assert data["value_kind"] == "parameter"
    assert data["intermediate_versions_unknown"] is False
    assert len(data["versions"]) == 3

    v0, v1, v2 = data["versions"]
    assert v0["version_index"] == 0
    assert v0["id"] == "param-p0"
    assert v0["value"] == "1000.0"
    assert v0["status"] == "superseded"
    assert v0["action"] == "create"
    assert v0["source"] == "lab-handbook"

    assert v1["version_index"] == 1
    assert v1["id"] == "param-p1"
    assert v1["value"] == "1025.0"
    assert v1["status"] == "superseded"
    assert v1["action"] == "supersede"

    assert v2["version_index"] == 2
    assert v2["id"] == "param-p2"
    assert v2["value"] == "1027.5"
    assert v2["status"] == "accepted"
    assert v2["action"] == "supersede"


def test_sim_result_value_version_chain_and_intermediate_versions_unknown(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'heat_flux': {'value': 25.0, 'unit': 'MW/m^2', 'quantity': 'heat_flux', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    _create_v2_model_and_run(client, "main", "param-length-v0")

    # Ingest run to create simulation_result_values
    spec_res = client.post(
        "/workspaces/main/model-specs",
        json={"title": "Calc Run Spec", "engineering_question": "Test calc run."},
    )
    impl = create_model_implementation(
        "main",
        ModelImplementationCreate(
            model_spec_id=spec_res.json()["id"],
            version_label="impl-calc",
            implementation_kind="calc_v0",
            script_text=script,
        ),
    )
    job = create_runner_job(
        "main",
        RunnerJobCreate(
            model_version_id=impl.id,
            input_set={"inlet_temp": {"value": 300, "unit": "K"}},
        ),
    )
    run_runner_job(job.runner_job.id)

    batches = list_simulation_result_batches("main")
    val_id = batches[0].values[0].id

    # Version 0: exception (initial ingest)
    res_v0 = client.get(f"/project-knowledge/workspaces/main/result-values/{val_id}/history")
    assert res_v0.status_code == 200
    data_v0 = res_v0.json()
    assert data_v0["value_kind"] == "simulation_result_value"
    assert data_v0["intermediate_versions_unknown"] is False
    assert len(data_v0["versions"]) == 1
    assert data_v0["versions"][0]["version_index"] == 0
    assert data_v0["versions"][0]["status"] == "exception"
    assert data_v0["versions"][0]["validation_state"] == "exception"  # Red dot never shown as validated!

    # Correct it to 8.0 MW/m^2 (within bounds: 1.0 to 10.0 MW/m^2)
    resolved = resolve_simulation_result_exception(
        val_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="correct",
            reason="Corrected to nominal range.",
            corrected_value="8.0",
        ),
    )
    assert resolved.status == "corrected"

    # Version chain now has versions 0 and 1
    res_v1 = client.get(f"/project-knowledge/workspaces/main/result-values/{val_id}/history")
    assert res_v1.status_code == 200
    data_v1 = res_v1.json()
    assert data_v1["intermediate_versions_unknown"] is False
    assert len(data_v1["versions"]) == 2
    assert data_v1["versions"][1]["version_index"] == 1
    assert float(data_v1["versions"][1]["value"]) == pytest.approx(8.0)
    assert data_v1["versions"][1]["status"] == "corrected"
    assert data_v1["versions"][1]["validation_state"] == "corrected"
    assert data_v1["versions"][1]["action"] == "correct"
    assert data_v1["versions"][1]["reason"] == "Corrected to nominal range."


def test_backfilled_chain_reports_intermediate_versions_unknown_true_when_holding_resolution(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    # Simulate a legacy value that had an exception and resolution before migration
    now = "2026-10-01T00:00:00+00:00"
    val_id = "legacy-val-1"
    batch_id = "legacy-batch-1"
    run_id = "legacy-run-1"

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, run_label, status, created_at
            ) VALUES (?, 'main', 'Legacy Run', 'succeeded', ?)
            """,
            (run_id, now),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_batches (
                id, workspace_id, run_id, source_payload_digest, extractor_version, created_at
            ) VALUES (?, 'main', ?, 'digest123', 'v0', ?)
            """,
            (batch_id, run_id, now),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_values (
                id, batch_id, output_name, source_payload_digest, extractor_id,
                extractor_version, value_text, unit, conditions_json, status, created_at
            ) VALUES (?, ?, 'pressure', 'digest123', 'ext', 'v0', '15.0', 'MPa', '{}', 'corrected', ?)
            """,
            (val_id, batch_id, now),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_resolutions (
                id, value_id, action, reason, actor, created_at,
                original_value_text, original_unit, original_conditions_json
            ) VALUES (
                'res-1', ?, 'correct', 'Prior overwritten resolution', 'operator', ?,
                '25.0', 'MPa', '{}'
            )
            """,
            (val_id, now),
        )
        # Execute the backfill logic from Slice 1
        backfill_simulation_result_value_versions(connection, value_id=val_id)
        connection.commit()

    # Read chain: because it holds a backfilled resolution (origin='backfilled' and version_index > 0),
    # it must report intermediate_versions_unknown: True!
    res = client.get(f"/project-knowledge/workspaces/main/result-values/{val_id}/history")
    assert res.status_code == 200
    data = res.json()
    assert data["intermediate_versions_unknown"] is True
    assert len(data["versions"]) == 2
    assert data["versions"][0]["version_index"] == 0
    assert data["versions"][0]["origin"] == "backfilled"
    assert data["versions"][1]["version_index"] == 1
    assert data["versions"][1]["origin"] == "backfilled"


def test_compare_parameter_versions_returns_side_by_side_with_bound_runs(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    # P0 used by Run 1
    _, run1_id = _create_v2_model_and_run(client, "main", "param-length-v0")

    # Supersede P0 with P1
    now = "2026-11-08T00:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE parameters
            SET status = 'superseded', lifecycle_state = 'superseded', updated_at = ?
            WHERE id = 'param-length-v0'
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                supersedes_parameter_id, origin, created_at, updated_at
            ) VALUES (
                'param-length-v1', 'main', 'tube_length', '15.0', 'm', 'accepted', 'active',
                'param-length-v0', 'human', ?, ?
            )
            """,
            (now, now),
        )
        connection.commit()

    # Compare P0 and P1
    res = client.get("/project-knowledge/workspaces/main/values/compare?value_kind=parameter&version_a=param-length-v0&version_b=param-length-v1")
    assert res.status_code == 200
    data = res.json()
    assert data["value_kind"] == "parameter"

    va = data["version_a"]
    vb = data["version_b"]
    assert va["id"] == "param-length-v0"
    assert va["value"] == "10.0"
    assert va["status"] == "superseded"
    assert any(r["id"] == run1_id for r in va["runs"])

    assert vb["id"] == "param-length-v1"
    assert vb["value"] == "15.0"
    assert vb["status"] == "accepted"


def test_compare_simulation_result_value_versions_and_requirement_checks(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 150.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    spec_res = client.post(
        "/workspaces/main/model-specs",
        json={"title": "Flow Spec", "engineering_question": "Test flow requirement."},
    )
    impl = create_model_implementation(
        "main",
        ModelImplementationCreate(
            model_spec_id=spec_res.json()["id"],
            version_label="impl-flow",
            implementation_kind="calc_v0",
            script_text=script,
        ),
    )
    job = create_runner_job(
        "main",
        RunnerJobCreate(
            model_version_id=impl.id,
            input_set={"inlet_temp": {"value": 300, "unit": "K"}},
        ),
    )
    run_runner_job(job.runner_job.id)

    batches = list_simulation_result_batches("main")
    val = batches[0].values[0]
    val_id = val.id

    # Resolve/correct to 50.0 kg/s
    resolve_simulation_result_exception(
        val_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="correct",
            reason="Adjusted mass flow.",
            corrected_value="50.0",
        ),
    )

    # Insert a requirement check observing version 0 (45.0 kg/s)
    now = "2026-11-09T00:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, basis_kind, criterion_operator, criterion_expected_value,
                criterion_output_name, criterion_expected_unit, criterion_rule_version,
                status, created_at, updated_at
            ) VALUES (
                'req-flow', 'main', 'Flow Criterion', 'acceptance_criterion', '>=', '40.0',
                'mass_flow', 'kg/s', 'criterion_v0',
                'accepted', ?, ?
            )
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO project_knowledge_revisions (
                id, workspace_id, parent_kind, state, change_set_digest, change_set_json,
                projected_state_digest, origin, created_at, accepted_at
            ) VALUES (
                'rev-1', 'main', 'working', 'working', 'cs-digest', '{}',
                'digest-basis', 'human', ?, ?
            )
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO project_knowledge_validation (
                id, workspace_id, working_revision_id, requirement_id, requirement_updated_at,
                rule_version, validated_basis_digest, applicability_set_digest,
                source_run_id, source_scalar_id, validator_id, validator_version,
                outcome, reason_code, observed_json, created_at
            ) VALUES (
                'chk-v0', 'main', 'rev-1', 'req-flow', ?,
                'criterion_v0', 'digest-basis', 'app-digest',
                ?, ?, 'criterion_v0', '0.1.0',
                'pass', 'satisfied', ?, ?
            )
            """,
            (
                now,
                job.runner_job.simulation_run_id,
                val.admitted_scalar_id,
                json.dumps({"value": "150.0", "unit": "kg/s"}),
                now,
            ),
        )
        connection.commit()

    # Compare version 0 and version 1 of val_id
    res = client.get(f"/project-knowledge/workspaces/main/values/compare?value_kind=simulation_result_value&value_id={val_id}&version_a=0&version_b=1")
    assert res.status_code == 200
    data = res.json()
    assert data["value_kind"] == "simulation_result_value"
    assert data["value_id"] == val_id

    va = data["version_a"]
    vb = data["version_b"]
    assert va["version_index"] == 0
    assert va["value"] == "150.0"
    # chk-v0 observed 150.0, so it's bound to side A!
    assert any(c["id"] == "chk-v0" for c in va["requirement_checks"])

    assert vb["version_index"] == 1
    assert float(vb["value"]) == pytest.approx(50.0)
    # chk-v0 did NOT observe 50.0, so side B requirement checks should not have it
    assert not any(c["id"] == "chk-v0" for c in vb["requirement_checks"])


def test_compare_rejects_unrelated_values_with_not_same_value(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    now = "2026-11-06T00:00:00+00:00"
    with open_sqlite_connection() as connection:
        # Two unrelated parameters with distinct roots
        connection.execute(
            """
            INSERT OR REPLACE INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                origin, created_at, updated_at
            ) VALUES
                ('unrelated-1', 'main', 'param_a', '10.0', 'm', 'accepted', 'active', 'human', ?, ?),
                ('unrelated-2', 'main', 'param_b', '20.0', 'kg', 'accepted', 'active', 'human', ?, ?)
            """,
            (now, now, now, now),
        )
        connection.commit()

    # Compare unrelated parameters: MUST refuse with not_same_value (400)
    res = client.get("/project-knowledge/workspaces/main/values/compare?value_kind=parameter&version_a=unrelated-1&version_b=unrelated-2")
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "not_same_value"

    # Compare parameter with non-existent or cross-kind target
    res_kind = client.get("/project-knowledge/workspaces/main/values/compare?value_kind=parameter&version_a=unrelated-1&version_b=nonexistent")
    assert res_kind.status_code == 404
    assert res_kind.json()["detail"]["code"] == "parameter_not_found"


def test_no_writes_in_reads_discipline(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    _, sim_run_id = _create_v2_model_and_run(client, "main", "param-length-v0")
    now = "2026-11-06T00:00:00+00:00"

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                supersedes_parameter_id, origin, created_at, updated_at
            ) VALUES ('param-length-v1', 'main', 'tube_length', '12.0', 'm', 'accepted', 'active', 'param-length-v0', 'human', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_batches (
                id, workspace_id, run_id, source_payload_digest, extractor_version,
                notification_state, created_at
            ) VALUES ('batch-nw', 'main', ?, 'd1', '1.0', 'none', ?)
            """,
            (sim_run_id, now),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_values (
                id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version,
                quantity, value_text, unit, conditions_json, status, created_at
            ) VALUES ('val-nw', 'batch-nw', 'heat_flux', 'd1', 'ext', '1.0', 'heat_flux', '10.0', 'MW/m^2', '{}', 'passed', ?)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_value_versions (
                id, value_id, version_index, value_text, unit, conditions_json,
                status, action, reason, actor, created_at, origin
            ) VALUES
                ('ver-nw0', 'val-nw', 0, '10.0', 'MW/m^2', '{}', 'passed', 'ingest', NULL, NULL, ?, 'recorded'),
                ('ver-nw1', 'val-nw', 1, '8.0', 'MW/m^2', '{}', 'passed', 'correct', 'reason', 'user', ?, 'recorded')
            """,
            (now, now),
        )
        connection.commit()

    def get_table_counts() -> dict[str, int]:
        with open_sqlite_connection() as connection:
            tables = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            return {
                t: connection.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                for t in sorted(tables)
            }

    counts_before = get_table_counts()

    # Call every read endpoint added by value-history
    res_manifest = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res_manifest.status_code == 200

    res_param_hist = client.get("/project-knowledge/workspaces/main/parameters/param-length-v0/history")
    assert res_param_hist.status_code == 200

    res_res_hist = client.get("/project-knowledge/workspaces/main/result-values/val-nw/history")
    assert res_res_hist.status_code == 200

    res_param_cmp = client.get(
        "/project-knowledge/workspaces/main/values/compare?value_kind=parameter&parameter_a_id=param-length-v0&parameter_b_id=param-length-v1"
    )
    assert res_param_cmp.status_code == 200

    res_res_cmp = client.get(
        "/project-knowledge/workspaces/main/values/compare?value_kind=simulation_result_value&value_id=val-nw&version_a=0&version_b=1"
    )
    assert res_res_cmp.status_code == 200

    counts_after = get_table_counts()
    assert counts_before == counts_after, "Read operations must not write to any database table!"


def test_manifest_newer_version_exists_only_for_accepted_active(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    _, sim_run_id = _create_v2_model_and_run(client, "main", "param-length-v0")

    now = "2026-11-06T01:00:00+00:00"
    with open_sqlite_connection() as connection:
        # Add a proposed child and a rejected child
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                supersedes_parameter_id, origin, created_at, updated_at
            ) VALUES
                ('param-len-prop', 'main', 'tube_length', '12.0', 'm', 'proposed', 'active', 'param-length-v0', 'human', ?, ?),
                ('param-len-rej', 'main', 'tube_length', '14.0', 'm', 'rejected', 'archived', 'param-length-v0', 'human', ?, ?)
            """,
            (now, now, now, now),
        )
        connection.commit()

    # newer_version_exists must be False because proposed and rejected children do not count
    res = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res.status_code == 200
    data = res.json()
    assert data["items"][0]["newer_version_exists"] is False

    # Also parameter history forward chain must NOT include proposed or rejected children
    res_chain = client.get("/project-knowledge/workspaces/main/parameters/param-length-v0/history")
    assert res_chain.status_code == 200
    chain_data = res_chain.json()
    assert len(chain_data["versions"]) == 1
    assert chain_data["versions"][0]["id"] == "param-length-v0"

    # Now add an accepted active child
    now2 = "2026-11-06T02:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, status, lifecycle_state,
                supersedes_parameter_id, origin, created_at, updated_at
            ) VALUES ('param-len-acc', 'main', 'tube_length', '15.0', 'm', 'accepted', 'active', 'param-length-v0', 'human', ?, ?)
            """,
            (now2, now2),
        )
        connection.commit()

    # newer_version_exists must now be True
    res2 = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["items"][0]["newer_version_exists"] is True

    # Parameter history forward chain now includes accepted active child
    res_chain2 = client.get("/project-knowledge/workspaces/main/parameters/param-length-v0/history")
    assert res_chain2.status_code == 200
    chain_data2 = res_chain2.json()
    assert len(chain_data2["versions"]) == 2
    assert chain_data2["versions"][1]["id"] == "param-len-acc"


def test_manifest_malformed_input_payload_returns_history_not_recorded(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    _, sim_run_id = _create_v2_model_and_run(client, "main", "param-length-v0")

    # Case 1: Invalid JSON string
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE simulation_runs SET input_payload = '{not-valid-json' WHERE id = ?",
            (sim_run_id,),
        )
        connection.commit()

    res1 = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "history_not_recorded"
    assert data1["history_not_recorded"] is True
    assert data1["history_recorded"] is False
    assert data1["reason"] is not None
    assert data1["items"] == []

    # Case 2: Not an object (e.g. array)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE simulation_runs SET input_payload = '[1, 2, 3]' WHERE id = ?",
            (sim_run_id,),
        )
        connection.commit()

    res2 = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "history_not_recorded"
    assert data2["history_not_recorded"] is True
    assert data2["history_recorded"] is False
    assert data2["reason"] is not None
    assert data2["items"] == []

    # Case 3: Object with non-string source_parameter_id
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE simulation_runs
            SET input_payload = '{"tube_length": {"value": 10.0, "unit": "m", "source_parameter_id": 12345}}'
            WHERE id = ?
            """,
            (sim_run_id,),
        )
        connection.commit()

    res3 = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["status"] == "history_not_recorded"
    assert data3["history_not_recorded"] is True
    assert data3["history_recorded"] is False
    assert data3["reason"] is not None
    assert data3["items"] == []


def test_manifest_lifecycle_state_null_returns_null(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    _, sim_run_id = _create_v2_model_and_run(client, "main", "param-length-v0")

    with open_sqlite_connection() as connection:
        cols = [r[1] for r in connection.execute("PRAGMA table_info(parameters)").fetchall()]
        cols_str = ", ".join(cols)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("CREATE TABLE parameters_bak AS SELECT * FROM parameters")
        connection.execute("DROP TABLE parameters")
        connection.execute(
            """
            CREATE TABLE parameters (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
                symbol TEXT, value TEXT, unit TEXT NOT NULL DEFAULT 'unspecified',
                value_status TEXT NOT NULL DEFAULT 'candidate', value_min REAL, value_max REAL,
                source_ref TEXT, confidence REAL, status TEXT NOT NULL DEFAULT 'draft',
                origin TEXT NOT NULL DEFAULT 'user', source_ai_job_id TEXT, promoted_at TEXT,
                supersedes_parameter_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                notes TEXT, lifecycle_state TEXT
            )
            """
        )
        connection.execute(f"INSERT INTO parameters ({cols_str}) SELECT {cols_str} FROM parameters_bak")
        connection.execute("DROP TABLE parameters_bak")
        connection.execute("UPDATE parameters SET lifecycle_state = NULL WHERE id = 'param-length-v0'")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.commit()

    res = client.get(f"/project-knowledge/workspaces/main/simulation-runs/{sim_run_id}/values-used")
    assert res.status_code == 200
    item = res.json()["items"][0]
    assert item["lifecycle_state"] is None


def test_conditions_strips_internal_unvalidated_key_on_read_and_compare(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    now0 = "2026-11-06T00:00:00+00:00"
    now1 = "2026-11-06T01:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO simulation_runs (id, workspace_id, run_label, status, created_at) VALUES ('run-c1', 'main', 'run-c1', 'completed', ?)",
            (now0,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_batches (
                id, workspace_id, run_id, source_payload_digest, extractor_version,
                notification_state, created_at
            ) VALUES ('batch-c1', 'main', 'run-c1', 'digest1', '1.0', 'none', ?)
            """,
            (now0,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_values (
                id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version,
                quantity, value_text, unit, conditions_json, status, created_at
            ) VALUES (
                'val-c1', 'batch-c1', 'outlet_temp', 'digest1', 'ext1', '1.0',
                'temperature', '350.0', 'K',
                '{"pressure": 10.0, "_unvalidated_input_value_ids": ["val-upstream-1"]}',
                'passed', ?
            )
            """,
            (now0,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_value_versions (
                id, value_id, version_index, value_text, unit, conditions_json,
                status, action, reason, actor, created_at, origin
            ) VALUES
                (
                    'ver-c0', 'val-c1', 0, '350.0', 'K',
                    '{"pressure": 10.0, "_unvalidated_input_value_ids": ["val-upstream-1"]}',
                    'passed', 'ingest', NULL, NULL, ?, 'recorded'
                ),
                (
                    'ver-c1', 'val-c1', 1, '350.0', 'K',
                    '{"pressure": 10.0}',
                    'passed', 'confirm', 'Confirmed', 'user', ?, 'recorded'
                )
            """,
            (now0, now1),
        )
        connection.commit()

    # Query history: _unvalidated_input_value_ids must not appear in conditions
    res_hist = client.get("/project-knowledge/workspaces/main/result-values/val-c1/history")
    assert res_hist.status_code == 200
    versions = res_hist.json()["versions"]
    assert versions[0]["conditions"] == {"pressure": 10.0}
    assert "_unvalidated_input_value_ids" not in versions[0]["conditions"]
    assert versions[1]["conditions"] == {"pressure": 10.0}

    # Compare versions: differing ONLY in _unvalidated_input_value_ids must NOT report conditions as different
    res_cmp = client.get(
        "/project-knowledge/workspaces/main/values/compare?value_kind=simulation_result_value&value_id=val-c1&version_a=0&version_b=1"
    )
    assert res_cmp.status_code == 200
    cmp_data = res_cmp.json()
    assert cmp_data["version_a"]["conditions"] == {"pressure": 10.0}
    assert cmp_data["version_b"]["conditions"] == {"pressure": 10.0}
    assert cmp_data["version_a"]["conditions"] == cmp_data["version_b"]["conditions"]


def test_compare_sim_result_values_foreign_workspace_scoped(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    now = "2026-11-06T00:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, slug, created_at, updated_at) VALUES ('foreign_ws', 'Foreign', 'foreign-ws', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO simulation_runs (id, workspace_id, run_label, status, created_at) VALUES ('run-m', 'main', 'run-m', 'completed', ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO simulation_runs (id, workspace_id, run_label, status, created_at) VALUES ('run-f', 'foreign_ws', 'run-f', 'completed', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_batches (
                id, workspace_id, run_id, source_payload_digest, extractor_version,
                notification_state, created_at
            ) VALUES
                ('batch-m', 'main', 'run-m', 'd1', '1.0', 'none', ?),
                ('batch-f', 'foreign_ws', 'run-f', 'd2', '1.0', 'none', ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_values (
                id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version,
                quantity, value_text, unit, conditions_json, status, created_at
            ) VALUES
                ('val-main', 'batch-m', 'out_m', 'd1', 'ext', '1.0', 'temp', '300', 'K', '{}', 'passed', ?),
                ('val-foreign', 'batch-f', 'out_f', 'd2', 'ext', '1.0', 'temp', '400', 'K', '{}', 'passed', ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_value_versions (
                id, value_id, version_index, value_text, unit, conditions_json,
                status, action, reason, actor, created_at, origin
            ) VALUES
                ('ver-m0', 'val-main', 0, '300', 'K', '{}', 'passed', 'ingest', NULL, NULL, ?, 'recorded'),
                ('ver-m1', 'val-main', 1, '305', 'K', '{}', 'passed', 'correct', 'reason', 'user', ?, 'recorded'),
                ('ver-f0', 'val-foreign', 0, '400', 'K', '{}', 'passed', 'ingest', NULL, NULL, ?, 'recorded')
            """,
            (now, now, now),
        )
        connection.commit()

    # In workspace 'main', looking up version from 'foreign_ws' must return 404 result_value_version_not_found, NOT 400 not_same_value
    res_foreign = client.get(
        "/project-knowledge/workspaces/main/values/compare?value_kind=simulation_result_value&value_id=val-main&version_a=ver-m0&version_b=ver-f0"
    )
    assert res_foreign.status_code == 404
    assert res_foreign.json()["detail"]["code"] == "result_value_version_not_found"

    # In workspace 'main', looking up a version from 'main' but belonging to a different value returns 400 not_same_value
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO simulation_result_values (
                id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version,
                quantity, value_text, unit, conditions_json, status, created_at
            ) VALUES ('val-main-2', 'batch-m', 'out_m2', 'd1', 'ext', '1.0', 'temp', '500', 'K', '{}', 'passed', ?)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_value_versions (
                id, value_id, version_index, value_text, unit, conditions_json,
                status, action, reason, actor, created_at, origin
            ) VALUES ('ver-m2-0', 'val-main-2', 0, '500', 'K', '{}', 'passed', 'ingest', NULL, NULL, ?, 'recorded')
            """,
            (now,),
        )
        connection.commit()

    res_diff = client.get(
        "/project-knowledge/workspaces/main/values/compare?value_kind=simulation_result_value&value_id=val-main&version_a=ver-m0&version_b=ver-m2-0"
    )
    assert res_diff.status_code == 400
    assert res_diff.json()["detail"]["code"] == "not_same_value"


def test_compare_downstream_runs_attribution_and_decimal_checks(client: TestClient) -> None:
    _seed_parameters_and_decision("main")
    t0 = "2026-11-06T10:00:00+00:00"
    t1 = "2026-11-06T11:00:00+00:00"
    t2 = "2026-11-06T12:00:00+00:00"
    t3 = "2026-11-06T13:00:00+00:00"

    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO simulation_runs (id, workspace_id, run_label, status, created_at) VALUES ('run-producer', 'main', 'producer', 'completed', ?)",
            (t0,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_batches (
                id, workspace_id, run_id, source_payload_digest, extractor_version,
                notification_state, created_at
            ) VALUES ('batch-timing', 'main', 'run-producer', 'd1', '1.0', 'none', ?)
            """,
            (t0,),
        )
        connection.execute(
            """
            INSERT INTO simulation_result_values (
                id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version,
                quantity, value_text, unit, conditions_json, status, created_at
            ) VALUES ('val-timing', 'batch-timing', 'pressure', 'd1', 'ext', '1.0', 'pressure', '1.0', 'MPa', '{}', 'passed', ?)
            """,
            (t0,),
        )
        # Version 0 created at t0, Version 1 created at t2
        connection.execute(
            """
            INSERT INTO simulation_result_value_versions (
                id, value_id, version_index, value_text, unit, conditions_json,
                status, action, reason, actor, created_at, origin
            ) VALUES
                ('ver-t0', 'val-timing', 0, '1.0', 'MPa', '{}', 'passed', 'ingest', NULL, NULL, ?, 'recorded'),
                ('ver-t1', 'val-timing', 1, '2.0', 'MPa', '{}', 'passed', 'correct', 'adjust', 'user', ?, 'recorded')
            """,
            (t0, t2),
        )
        # Run 1 created at t1 (t0 <= t1 < t2) -> attributed to Version 0
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, run_label, status, input_payload, created_at
            ) VALUES (
                'run-downstream-1', 'main', 'run-1', 'completed',
                '{"p": {"value": 1.0, "unit": "MPa", "source_result_value_id": "val-timing"}}',
                ?
            )
            """,
            (t1,),
        )
        # Run 2 created at t3 (t3 >= t2) -> attributed to Version 1
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, run_label, status, input_payload, created_at
            ) VALUES (
                'run-downstream-2', 'main', 'run-2', 'completed',
                '{"p": {"value": 2.0, "unit": "MPa", "source_result_value_id": "val-timing"}}',
                ?
            )
            """,
            (t3,),
        )
        # Insert requirement and revision for foreign keys
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, basis_kind, criterion_operator, criterion_expected_value,
                criterion_output_name, criterion_expected_unit, criterion_rule_version,
                status, created_at, updated_at
            ) VALUES (
                'req-1', 'main', 'Pressure Requirement', 'acceptance_criterion', '>=', '1.0',
                'pressure', 'MPa', 'criterion_v0',
                'accepted', ?, ?
            )
            """,
            (t0, t0),
        )
        connection.execute(
            """
            INSERT INTO project_knowledge_revisions (
                id, workspace_id, parent_kind, state, change_set_digest, change_set_json,
                projected_state_digest, origin, created_at, accepted_at
            ) VALUES (
                'rev-1', 'main', 'working', 'working', 'cs-digest', '{}',
                'digest-basis', 'human', ?, ?
            )
            """,
            (t0, t0),
        )

        # Requirement check: observed_json has value 1 (numeric Decimal matches "1.0"), unit "MPa"
        connection.execute(
            """
            INSERT INTO project_knowledge_validation (
                id, workspace_id, working_revision_id, requirement_id, requirement_updated_at,
                rule_version, validated_basis_digest, applicability_set_digest,
                source_run_id, source_scalar_id, validator_id, validator_version,
                outcome, reason_code, observed_json, created_at
            ) VALUES (
                'val-chk-1', 'main', 'rev-1', 'req-1', ?,
                'criterion_v0', 'digest-basis', 'app-digest',
                'run-producer', NULL, 'criterion_v0', '0.1.0',
                'pass', 'within_bounds',
                '{"value": 1, "unit": "MPa"}',
                ?
            )
            """,
            (t1, t1),
        )
        connection.commit()

    res = client.get(
        "/project-knowledge/workspaces/main/values/compare?value_kind=simulation_result_value&value_id=val-timing&version_a=0&version_b=1"
    )
    assert res.status_code == 200
    data = res.json()

    v_a = data["version_a"]
    v_b = data["version_b"]

    # Downstream run attribution:
    # Version 0 has run-downstream-1 (and not run-downstream-2)
    run_ids_a = {r["id"] for r in v_a["runs"]}
    assert "run-downstream-1" in run_ids_a
    assert "run-downstream-2" not in run_ids_a

    # Version 1 has run-downstream-2 (and not run-downstream-1)
    run_ids_b = {r["id"] for r in v_b["runs"]}
    assert "run-downstream-2" in run_ids_b
    assert "run-downstream-1" not in run_ids_b

    # Requirement check: 1 matches "1.0" numerically via Decimal
    check_ids_a = {c["id"] for c in v_a["requirement_checks"]}
    assert "val-chk-1" in check_ids_a
