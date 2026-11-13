from __future__ import annotations

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


def _seed_basis_bounds_and_decisions(workspace_id: str = "main") -> None:
    now = "2026-11-06T00:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, value_status, value_min, value_max,
                status, origin, created_at, updated_at, lifecycle_state
            ) VALUES
                ('param-pressure', ?, 'coolant_pressure', '12.0', 'MPa', 'accepted', 5.0, 20.0, 'accepted', 'human', ?, ?, 'active'),
                ('param-heat', ?, 'heat_flux', '8.0', 'MW/m^2', 'validated', 1.0, 10.0, 'accepted', 'human', ?, ?, 'active'),
                ('param-flow', ?, 'mass_flow', '45.0', 'kg/s', 'validated', 10.0, 100.0, 'accepted', 'human', ?, ?, 'active')
            """,
            (workspace_id, now, now, workspace_id, now, now, workspace_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO decisions (
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


def _create_calc_run(
    client: TestClient,
    script: str,
    input_set: dict[str, object] | None = None,
) -> dict[str, object]:
    spec_res = client.post(
        "/workspaces/main/model-specs",
        json={"title": "258 History Model", "engineering_question": "Verify version tracking."},
    )
    assert spec_res.status_code == 201
    impl = create_model_implementation(
        "main",
        ModelImplementationCreate(
            model_spec_id=spec_res.json()["id"],
            version_label="calc-v0-258",
            implementation_kind="calc_v0",
            script_text=script,
        ),
    )
    job_created = create_runner_job(
        "main",
        RunnerJobCreate(
            model_version_id=impl.id,
            input_set=input_set or {"inlet_temp": {"value": 300, "unit": "K"}},
        ),
    )
    run_res = run_runner_job(job_created.runner_job.id)
    assert run_res.error is None
    return run_res.model_dump(mode="json")


def test_ingest_creates_exactly_one_version_0_per_value_recorded(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 45.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'regime': 'nominal'}},
            'coolant_pressure': {'value': 25.0, 'unit': 'MPa', 'quantity': 'coolant_pressure', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    _create_calc_run(client, script)

    batches = list_simulation_result_batches("main")
    assert len(batches) == 1
    batch = batches[0]
    assert len(batch.values) == 2

    with open_sqlite_connection() as connection:
        for val in batch.values:
            rows = connection.execute(
                """
                SELECT * FROM simulation_result_value_versions
                WHERE value_id = ?
                ORDER BY version_index ASC
                """,
                (val.id,),
            ).fetchall()

            # Ingest creates exactly one version 0 per value, origin 'recorded'
            assert len(rows) == 1
            v0 = rows[0]
            assert v0["version_index"] == 0
            assert v0["value_text"] == val.value
            assert v0["unit"] == val.unit
            assert v0["status"] == val.status
            assert v0["action"] == "ingest"
            assert v0["reason"] is None
            assert v0["actor"] is None
            assert v0["origin"] == "recorded"
            assert v0["created_at"] == val.created_at


def test_two_successive_corrections_produce_versions_0_1_2_with_distinct_values(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    # heat_flux bounds are 1.0 to 10.0 MW/m^2; initial 25.0 is an exception
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'heat_flux': {'value': 25.0, 'unit': 'MW/m^2', 'quantity': 'heat_flux', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    _create_calc_run(client, script)

    batches = list_simulation_result_batches("main")
    val = batches[0].values[0]
    assert val.output_name == "heat_flux"
    assert val.status == "exception"
    value_id = val.id

    # First correction: 15.0 MW/m^2 still exceeds 10.0 max bound -> stays 'exception'
    res1 = resolve_simulation_result_exception(
        value_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="correct",
            reason="Initial reduction attempt to 15.0 MW/m^2.",
            corrected_value="15.0",
        ),
    )
    assert res1.status == "exception"

    # Second correction: 7.5 MW/m^2 is within [1.0, 10.0] -> becomes 'corrected'
    res2 = resolve_simulation_result_exception(
        value_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="correct",
            reason="Further reduction to 7.5 MW/m^2 within bounds.",
            corrected_value="7.5",
        ),
    )
    assert res2.status == "corrected"

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM simulation_result_value_versions
            WHERE value_id = ?
            ORDER BY version_index ASC
            """,
            (value_id,),
        ).fetchall()

        # Two successive corrections produce versions 0, 1, 2 in order
        assert len(rows) == 3
        v0, v1, v2 = rows[0], rows[1], rows[2]

        assert v0["version_index"] == 0
        assert v0["value_text"] == "25.0"
        assert v0["status"] == "exception"
        assert v0["action"] == "ingest"
        assert v0["origin"] == "recorded"

        assert v1["version_index"] == 1
        assert float(v1["value_text"]) == pytest.approx(15.0)
        assert v1["status"] == "exception"
        assert v1["action"] == "correct"
        assert v1["reason"] == "Initial reduction attempt to 15.0 MW/m^2."
        assert v1["actor"] == "operator"
        assert v1["origin"] == "recorded"

        assert v2["version_index"] == 2
        assert float(v2["value_text"]) == pytest.approx(7.5)
        assert v2["status"] == "corrected"
        assert v2["action"] == "correct"
        assert v2["reason"] == "Further reduction to 7.5 MW/m^2 within bounds."
        assert v2["actor"] == "operator"
        assert v2["origin"] == "recorded"

        # Distinct values across all versions, and version 0 keeps the ingested value
        values_set = {v0["value_text"], v1["value_text"], v2["value_text"]}
        assert len(values_set) == 3
        assert v0["value_text"] == "25.0"


def test_confirm_appends_version_with_status_confirmed(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'coolant_pressure': {'value': 25.0, 'unit': 'MPa', 'quantity': 'coolant_pressure', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    _create_calc_run(client, script)

    batches = list_simulation_result_batches("main")
    val = batches[0].values[0]
    value_id = val.id

    confirmed = resolve_simulation_result_exception(
        value_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="confirm",
            reason="Transient pressure accepted by operator.",
        ),
    )
    assert confirmed.status == "confirmed"

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM simulation_result_value_versions
            WHERE value_id = ?
            ORDER BY version_index ASC
            """,
            (value_id,),
        ).fetchall()

        assert len(rows) == 2
        v0, v1 = rows[0], rows[1]
        assert v0["version_index"] == 0
        assert v0["action"] == "ingest"

        assert v1["version_index"] == 1
        assert v1["status"] == "confirmed"
        assert v1["action"] == "confirm"
        assert v1["reason"] == "Transient pressure accepted by operator."
        assert v1["actor"] == "operator"
        assert v1["origin"] == "recorded"
        assert v1["value_text"] == "25.0"


def test_backfill_on_legacy_values_with_and_without_resolution_row(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 45.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'regime': 'nominal'}},
            'heat_flux': {'value': 25.0, 'unit': 'MW/m^2', 'quantity': 'heat_flux', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    _create_calc_run(client, script)

    batches = list_simulation_result_batches("main")
    by_name = {v.output_name: v for v in batches[0].values}
    flow_id = by_name["mass_flow"].id
    heat_id = by_name["heat_flux"].id

    # Resolve heat_flux to create a resolution row
    resolve_simulation_result_exception(
        heat_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="correct",
            reason="Pre-migration resolution test.",
            corrected_value="7.5",
        ),
    )

    # Simulate legacy pre-migration database by wiping all version rows
    with open_sqlite_connection() as connection:
        connection.execute("DELETE FROM simulation_result_value_versions")
        connection.commit()
        cnt_before = connection.execute("SELECT COUNT(*) AS c FROM simulation_result_value_versions").fetchone()["c"]
        assert cnt_before == 0

        # Execute backfill
        inserted = backfill_simulation_result_value_versions(connection)
        assert inserted == 3  # 1 for flow (no resolution) + 2 for heat_flux (with resolution)
        connection.commit()

        # Check flow (no resolution row): exactly version 0 with origin 'backfilled'
        flow_versions = connection.execute(
            "SELECT * FROM simulation_result_value_versions WHERE value_id = ? ORDER BY version_index ASC",
            (flow_id,),
        ).fetchall()
        assert len(flow_versions) == 1
        fv0 = flow_versions[0]
        assert fv0["version_index"] == 0
        assert fv0["value_text"] == "45.0"
        assert fv0["unit"] == "kg/s"
        assert fv0["status"] == "passed"
        assert fv0["action"] == "ingest"
        assert fv0["reason"] is None
        assert fv0["actor"] is None
        assert fv0["origin"] == "backfilled"

        # Check heat_flux (with resolution row): version 0 and version 1 with origin 'backfilled'
        heat_versions = connection.execute(
            "SELECT * FROM simulation_result_value_versions WHERE value_id = ? ORDER BY version_index ASC",
            (heat_id,),
        ).fetchall()
        assert len(heat_versions) == 2
        hv0, hv1 = heat_versions[0], heat_versions[1]

        # Version 0 comes from original_* in resolution, status exception, action ingest
        assert hv0["version_index"] == 0
        assert hv0["value_text"] == "25.0"
        assert hv0["unit"] == "MW/m^2"
        assert hv0["status"] == "exception"
        assert hv0["action"] == "ingest"
        assert hv0["reason"] is None
        assert hv0["actor"] is None
        assert hv0["origin"] == "backfilled"

        # Version 1 comes from current value row's value/unit/conditions/status, and resolution metadata
        assert hv1["version_index"] == 1
        assert float(hv1["value_text"]) == pytest.approx(7.5)
        assert hv1["unit"] == "MW/m^2"
        assert hv1["status"] == "corrected"
        assert hv1["action"] == "correct"
        assert hv1["reason"] == "Pre-migration resolution test."
        assert hv1["actor"] == "operator"
        assert hv1["origin"] == "backfilled"

        # A second backfill call inserts nothing (idempotence)
        repeat_inserted = backfill_simulation_result_value_versions(connection)
        assert repeat_inserted == 0
        total_after = connection.execute("SELECT COUNT(*) AS c FROM simulation_result_value_versions").fetchone()["c"]
        assert total_after == 3


def test_backfilled_version_1_equals_head_row_not_corrected_value_text_when_confirm(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'coolant_pressure': {'value': 25.0, 'unit': 'MPa', 'quantity': 'coolant_pressure', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    _create_calc_run(client, script)

    batches = list_simulation_result_batches("main")
    press_id = batches[0].values[0].id

    # Confirm action leaves corrected_value_text as NULL in simulation_result_resolutions
    resolve_simulation_result_exception(
        press_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="confirm",
            reason="Confirmed transient pressure.",
        ),
    )

    with open_sqlite_connection() as connection:
        res_row = connection.execute(
            "SELECT * FROM simulation_result_resolutions WHERE value_id = ?",
            (press_id,),
        ).fetchone()
        assert res_row["action"] == "confirm"
        assert res_row["corrected_value_text"] is None

        val_row = connection.execute(
            "SELECT * FROM simulation_result_values WHERE id = ?",
            (press_id,),
        ).fetchone()
        assert val_row["value_text"] == "25.0"
        assert val_row["status"] == "confirmed"

        # Delete version rows to simulate legacy pre-migration state
        connection.execute("DELETE FROM simulation_result_value_versions WHERE value_id = ?", (press_id,))
        connection.commit()

        # Run backfill
        inserted = backfill_simulation_result_value_versions(connection, value_id=press_id)
        assert inserted == 2
        connection.commit()

        press_versions = connection.execute(
            "SELECT * FROM simulation_result_value_versions WHERE value_id = ? ORDER BY version_index ASC",
            (press_id,),
        ).fetchall()
        assert len(press_versions) == 2
        pv1 = press_versions[1]

        # Version 1 equals the head row (25.0), NOT corrected_value_text (which is NULL)
        assert pv1["version_index"] == 1
        assert pv1["value_text"] == val_row["value_text"] == "25.0"
        assert pv1["value_text"] != res_row["corrected_value_text"]
        assert pv1["status"] == "confirmed"
        assert pv1["action"] == "confirm"
        assert pv1["origin"] == "backfilled"


def test_resolve_on_legacy_unversioned_value_backfills_then_appends(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'heat_flux': {'value': 25.0, 'unit': 'MW/m^2', 'quantity': 'heat_flux', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    _create_calc_run(client, script)

    batches = list_simulation_result_batches("main")
    heat_id = batches[0].values[0].id

    # Simulate a legacy unversioned exception value by deleting its version 0
    with open_sqlite_connection() as connection:
        connection.execute("DELETE FROM simulation_result_value_versions WHERE value_id = ?", (heat_id,))
        connection.commit()

    # Now resolve it: resolve should detect absence of version rows, run backfill (v0), then append (v1)
    resolved = resolve_simulation_result_exception(
        heat_id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="correct",
            reason="Corrected legacy exception value.",
            corrected_value="7.5",
        ),
    )
    assert resolved.status == "corrected"

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM simulation_result_value_versions WHERE value_id = ? ORDER BY version_index ASC",
            (heat_id,),
        ).fetchall()

        assert len(rows) == 2
        v0, v1 = rows[0], rows[1]

        # v0 was backfilled
        assert v0["version_index"] == 0
        assert v0["value_text"] == "25.0"
        assert v0["status"] == "exception"
        assert v0["action"] == "ingest"
        assert v0["origin"] == "backfilled"

        # v1 was appended as recorded
        assert v1["version_index"] == 1
        assert float(v1["value_text"]) == pytest.approx(7.5)
        assert v1["status"] == "corrected"
        assert v1["action"] == "correct"
        assert v1["reason"] == "Corrected legacy exception value."
        assert v1["actor"] == "operator"
        assert v1["origin"] == "recorded"
