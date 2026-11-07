from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.project_knowledge.models import (
    ApprovalRequest,
    DraftCreate,
    ProjectKnowledgeOperation,
    SimulationResultResolveRequest,
    ValidationRequest,
)
from app.modules.project_knowledge.service import (
    ProjectKnowledgeError,
    approve_draft,
    create_draft,
    evaluate_requirement,
    preview_impact,
    revalidation_status,
)
from app.modules.project_knowledge.sim_results import (
    ingest_validated_calc_v0_run,
    list_simulation_result_batches,
    list_simulation_result_notifications,
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


def _create_working_revision(workspace_id: str = "main", key: str = "rev-253") -> tuple[str, str]:
    draft = create_draft(
        DraftCreate(
            workspace_id=workspace_id,
            operations=[
                ProjectKnowledgeOperation(
                    owner_kind="requirement",
                    operation_kind="create",
                    fields={
                        "statement": "Thermal margin requirement",
                        "status": "active",
                        "basis_kind": "requirement",
                        "reconciliation_gate": "advisory",
                    },
                )
            ],
        )
    )
    preview = preview_impact(workspace_id, draft.id)
    approval = approve_draft(
        ApprovalRequest(
            workspace_id=workspace_id,
            approval_request_key=key,
            draft_id=draft.id,
            expected_draft_revision_token=draft.revision_token,
            expected_preview_digest=preview.digest,
        )
    )
    assert approval.working_revision_id is not None
    return approval.working_revision_id, preview.digest


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
    working_revision_id: str | None = None,
) -> dict[str, object]:
    spec_res = client.post(
        "/workspaces/main/model-specs",
        json={"title": "253 Calc Model", "engineering_question": "Verify simulation outputs."},
    )
    assert spec_res.status_code == 201
    impl = create_model_implementation(
        "main",
        ModelImplementationCreate(
            model_spec_id=spec_res.json()["id"],
            version_label="calc-v0-253",
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
    job_id = job_created.runner_job.id
    run_id = job_created.simulation_run.id
    if working_revision_id is not None:
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE simulation_runs SET project_knowledge_revision_id = ? WHERE id = ?",
                (working_revision_id, run_id),
            )
            connection.commit()
    run_res = run_runner_job(job_id)
    assert run_res.error is None
    return run_res.model_dump(mode="json")


def test_253_ac1_automatic_ingestion_is_idempotent_and_ignores_ineligible_runs(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 45.0, 'unit': 'kg/s', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    body = _create_calc_run(client, script)
    run_id = body["simulation_run"]["id"]

    with open_sqlite_connection() as connection:
        orig_payload = connection.execute(
            "SELECT output_payload FROM simulation_runs WHERE id = ?",
            (run_id,),
        ).fetchone()["output_payload"]

    batches = list_simulation_result_batches("main")
    assert len(batches) == 1
    batch = batches[0]
    assert batch.run_id == run_id
    assert batch.notification_state == "none"
    assert len(batch.values) == 1
    assert batch.values[0].output_name == "mass_flow"
    assert batch.values[0].status == "passed"
    assert batch.values[0].admitted_scalar_id is not None

    # Repeat ingestion across restart/retry creates no duplicate batch, value, or notification
    repeat_batch = ingest_validated_calc_v0_run("main", run_id)
    assert repeat_batch is not None
    assert repeat_batch.id == batch.id
    assert len(list_simulation_result_batches("main")) == 1

    # Source run payload and Project Basis parameters are unchanged
    with open_sqlite_connection() as connection:
        after_payload = connection.execute(
            "SELECT output_payload FROM simulation_runs WHERE id = ?",
            (run_id,),
        ).fetchone()["output_payload"]
        assert after_payload == orig_payload
        accepted_flow = connection.execute(
            "SELECT value, unit, status FROM parameters WHERE id = 'param-flow'",
        ).fetchone()
        assert accepted_flow["value"] == "45.0"
        assert accepted_flow["status"] == "accepted"

    # Failed or non-calc_v0 runs do not silently ingest
    now = "2026-11-06T01:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, run_label, status, output_payload, created_at
            ) VALUES ('failed-run', 'main', 'Failed', 'failed', '{"outputs":{"mass_flow":{"value":50,"unit":"kg/s"}}}', ?)
            """,
            (now,),
        )
        connection.commit()
    assert ingest_validated_calc_v0_run("main", "failed-run") is None
    assert len(list_simulation_result_batches("main")) == 1


def test_253_ac2_ac3_ac4_checks_exceptions_and_single_batch_notification(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    # Script produces:
    # 1. mass_flow: 45.0 kg/s -> passes all 5 checks
    # 2. heat_flux: 25.0 MW/m^2 -> out_of_plausible_range (bounds 1.0..10.0)
    # 3. coolant_pressure: 15.0 MPa -> basis_parameter_contradiction & basis_decision_contradiction (expected 12.0 MPa)
    # 4. unagbounded_metric: 3.5 m -> missing_plausible_bounds (no bounds in output or basis)
    # 5. duplicate_flow: 45.0 kg/s with quantity="mass_flow" and same conditions -> duplicate_quantity_and_conditions
    # 6. bad_unit_output: 12.0 Pa with quantity="coolant_pressure" -> incompatible_unit (MPa vs Pa)
    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 45.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'case': 'A'}},
            'heat_flux': {'value': 25.0, 'unit': 'MW/m^2', 'quantity': 'heat_flux', 'conditions': {'case': 'A'}},
            'coolant_pressure': {'value': 15.0, 'unit': 'MPa', 'quantity': 'coolant_pressure', 'conditions': {'case': 'A'}},
            'unbounded_metric': {'value': 3.5, 'unit': 'm', 'quantity': 'unbounded_metric', 'conditions': {'case': 'A'}},
            'z_duplicate_flow': {'value': 45.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'case': 'A'}, 'min': 10.0, 'max': 100.0},
            'bad_unit_output': {'value': 12.0, 'unit': 'Pa', 'quantity': 'coolant_pressure', 'conditions': {'case': 'A'}, 'min': 1.0, 'max': 100.0}
        }
    }, f)
""".strip()
    body = _create_calc_run(client, script)
    run_id = body["simulation_run"]["id"]

    batches = list_simulation_result_batches("main")
    assert len(batches) == 1
    batch = batches[0]
    assert batch.notification_state == "pending"
    assert batch.exception_count == 5

    by_name = {v.output_name: v for v in batch.values}
    assert by_name["mass_flow"].status == "passed"
    assert by_name["mass_flow"].conditions == {"case": "A"}
    assert by_name["mass_flow"].source_payload_digest == hashlib.sha256(
        (body["simulation_run"]["output_payload"] or "").encode("utf-8")
    ).hexdigest()

    hf_failed = {c.check_name: c.reason_code for c in by_name["heat_flux"].checks if c.outcome == "failed"}
    assert hf_failed["plausible_range"] == "out_of_plausible_range"

    cp_failed = {c.check_name: c.reason_code for c in by_name["coolant_pressure"].checks if c.outcome == "failed"}
    assert cp_failed["contradiction"] in {"basis_parameter_contradiction", "basis_decision_contradiction"}

    ub_failed = {c.check_name: c.reason_code for c in by_name["unbounded_metric"].checks if c.outcome == "failed"}
    assert ub_failed["plausible_range"] == "missing_plausible_bounds"

    dup_failed = {c.check_name: c.reason_code for c in by_name["z_duplicate_flow"].checks if c.outcome == "failed"}
    assert dup_failed["duplicate"] == "duplicate_quantity_and_conditions"

    unit_failed = {c.check_name: c.reason_code for c in by_name["bad_unit_output"].checks if c.outcome == "failed"}
    assert unit_failed["quantity_and_unit_consistency"] == "incompatible_unit"

    # Conflicting Project Basis decision remains unchanged
    with open_sqlite_connection() as connection:
        dec = connection.execute("SELECT decision_text, status FROM decisions WHERE id = 'dec-pressure'").fetchone()
        assert dec["decision_text"] == "coolant_pressure=12.0 MPa"
        assert dec["status"] == "accepted"

    # AC4: One batch notification for the whole batch, with exact target links for each failed value
    notices = list_simulation_result_notifications("main")
    assert len(notices) == 1
    notice = notices[0]
    assert notice.batch_id == batch.id
    assert notice.run_id == run_id
    assert notice.exception_count == 5
    assert len(notice.targets) == 5
    for target in notice.targets:
        assert f"runId={run_id}" in target.target_href
        assert f"valueId={target.value_id}" in target.target_href

    # Re-ingesting creates no duplicate notice
    ingest_validated_calc_v0_run("main", run_id)
    assert len(list_simulation_result_notifications("main")) == 1


def test_253_ac5_ac6_resolution_requirement_verdicts_conditions_and_unvalidated_markers(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    rev_id, basis_digest = _create_working_revision()

    now = "2026-11-06T02:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, status, schema_version, created_at, updated_at,
                basis_kind, reconciliation_gate, criterion_output_name, criterion_operator,
                criterion_expected_value, criterion_expected_unit, criterion_rule_version
            ) VALUES
                ('req-flow', 'main', 'Mass flow >= 40 kg/s', 'active', 1, ?, ?, 'acceptance_criterion', 'required', 'mass_flow', '>=', '40.0', 'kg/s', 'scalar-v1'),
                ('req-heat', 'main', 'Heat flux <= 10 MW/m^2', 'active', 1, ?, ?, 'acceptance_criterion', 'required', 'heat_flux', '<=', '10.0', 'MW/m^2', 'scalar-v1'),
                ('req-press', 'main', 'Pressure == 12 MPa', 'active', 1, ?, ?, 'acceptance_criterion', 'required', 'coolant_pressure', '==', '12.0', 'MPa', 'scalar-v1')
            """,
            (now, now, now, now, now, now),
        )
        for idx, req_id in enumerate(("req-flow", "req-heat", "req-press"), start=1):
            connection.execute(
                """
                INSERT INTO requirement_applicability (
                    id, workspace_id, requirement_id, target_kind, target_id, effect,
                    lifecycle_state, created_at, updated_at
                ) VALUES (?, 'main', ?, 'workspace', 'main', 'include', 'active', ?, ?)
                """,
                (f"app-{idx}", req_id, now, now),
            )
        connection.commit()

    script = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 45.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'regime': 'nominal'}},
            'heat_flux': {'value': 25.0, 'unit': 'MW/m^2', 'quantity': 'heat_flux', 'conditions': {'regime': 'nominal'}},
            'coolant_pressure': {'value': 15.0, 'unit': 'MPa', 'quantity': 'coolant_pressure', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    body = _create_calc_run(client, script, working_revision_id=rev_id)
    run_id = body["simulation_run"]["id"]

    batch = list_simulation_result_batches("main")[0]
    by_name = {v.output_name: v for v in batch.values}

    # Passing values never require confirmation or correction
    with pytest.raises(ProjectKnowledgeError) as exc_info:
        resolve_simulation_result_exception(
            by_name["mass_flow"].id,
            SimulationResultResolveRequest(
                workspace_id="main",
                action="confirm",
                reason="Should fail on passing value",
            ),
        )
    assert exc_info.value.code == "result_value_not_exception"

    reval_initial = revalidation_status("main", rev_id)
    app_digest = reval_initial.applicability_set_digest

    # AC3 & AC6: Passing value is usable immediately without a click and has unvalidated_source=False
    val_flow = evaluate_requirement(
        ValidationRequest(
            workspace_id="main",
            working_revision_id=rev_id,
            requirement_id="req-flow",
            expected_requirement_updated_at=now,
            validated_basis_digest=basis_digest,
            applicability_set_digest=app_digest,
            source_run_id=run_id,
            required_conditions={"regime": "nominal"},
        )
    )
    assert val_flow.outcome == "pass"
    assert val_flow.unvalidated_source is False
    assert val_flow.unvalidated_value_ids == []

    # AC6: Outside stated conditions -> not_evaluable with reason outside_stated_conditions
    val_outside = evaluate_requirement(
        ValidationRequest(
            workspace_id="main",
            working_revision_id=rev_id,
            requirement_id="req-flow",
            expected_requirement_updated_at=now,
            validated_basis_digest=basis_digest,
            applicability_set_digest=app_digest,
            source_run_id=run_id,
            expected_predecessor_validation_id=val_flow.id,
            required_conditions={"regime": "off-normal"},
        )
    )
    assert val_outside.outcome == "not_evaluable"
    assert val_outside.reason_code == "outside_stated_conditions"

    # AC5 & AC6: Confirming coolant_pressure records human decision without claiming automatic pass,
    # and requirement evaluation using it carries unvalidated_source=True
    confirmed_cp = resolve_simulation_result_exception(
        by_name["coolant_pressure"].id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="confirm",
            reason="Transient spike accepted for sensitivity study.",
        ),
    )
    assert confirmed_cp.status == "confirmed"
    assert confirmed_cp.unvalidated_source is True
    assert confirmed_cp.resolution is not None
    assert confirmed_cp.resolution.action == "confirm"
    assert confirmed_cp.resolution.original_value == "15.0"

    val_press = evaluate_requirement(
        ValidationRequest(
            workspace_id="main",
            working_revision_id=rev_id,
            requirement_id="req-press",
            expected_requirement_updated_at=now,
            validated_basis_digest=basis_digest,
            applicability_set_digest=app_digest,
            source_run_id=run_id,
        )
    )
    assert val_press.outcome == "fail"
    assert val_press.reason_code == "comparison_false"
    assert val_press.unvalidated_source is True
    assert by_name["coolant_pressure"].id in val_press.unvalidated_value_ids

    # Evaluate heat_flux before correction (currently 25.0 MW/m^2 -> fail, unvalidated_source=True)
    val_heat_before = evaluate_requirement(
        ValidationRequest(
            workspace_id="main",
            working_revision_id=rev_id,
            requirement_id="req-heat",
            expected_requirement_updated_at=now,
            validated_basis_digest=basis_digest,
            applicability_set_digest=app_digest,
            source_run_id=run_id,
        )
    )
    assert val_heat_before.outcome == "fail"
    assert val_heat_before.unvalidated_source is True

    # AC5: Correct heat_flux from 25.0 to 7.5 MW/m^2 -> preserves original source, re-runs checks and passes
    corrected_hf = resolve_simulation_result_exception(
        by_name["heat_flux"].id,
        SimulationResultResolveRequest(
            workspace_id="main",
            action="correct",
            reason="Sensor calibration factor 0.3 applied.",
            corrected_value="7.5",
            corrected_unit="MW/m^2",
        ),
    )
    assert corrected_hf.status == "corrected"
    assert corrected_hf.value == "7.5"
    assert corrected_hf.unvalidated_source is False
    assert corrected_hf.resolution is not None
    assert corrected_hf.resolution.original_value == "25.0"
    assert corrected_hf.resolution.corrected_value == "7.5"
    assert all(c.outcome == "passed" for c in corrected_hf.checks)

    # Prior validation row for req-heat is now stale because scalar value changed from 25.0 to 7.5!
    reval_after_correction = revalidation_status("main", rev_id)
    assert any(d.startswith(f"validation_stale:req-heat:{val_heat_before.id}") for d in reval_after_correction.diagnostics)

    # Re-evaluating req-heat after correction passes with unvalidated_source=False
    val_heat_after = evaluate_requirement(
        ValidationRequest(
            workspace_id="main",
            working_revision_id=rev_id,
            requirement_id="req-heat",
            expected_requirement_updated_at=now,
            validated_basis_digest=basis_digest,
            applicability_set_digest=app_digest,
            source_run_id=run_id,
            expected_predecessor_validation_id=val_heat_before.id,
        )
    )
    assert val_heat_after.outcome == "pass"
    assert val_heat_after.unvalidated_source is False


def test_253_routes_and_downstream_calculation_unvalidated_marker(client: TestClient) -> None:
    _seed_basis_bounds_and_decisions()
    script1 = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'coolant_pressure': {'value': 15.0, 'unit': 'MPa', 'quantity': 'coolant_pressure', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    body1 = _create_calc_run(client, script1)
    assert body1["simulation_run"]["id"]

    # Verify HTTP endpoints: /result-batches, /result-notifications, /result-values/{id}/resolve
    batches_res = client.get("/project-knowledge/workspaces/main/result-batches")
    assert batches_res.status_code == 200
    batches_json = batches_res.json()
    assert len(batches_json) == 1
    flagged_val_id = batches_json[0]["values"][0]["id"]
    assert batches_json[0]["values"][0]["status"] == "exception"

    notices_res = client.get("/project-knowledge/workspaces/main/result-notifications")
    assert notices_res.status_code == 200
    assert len(notices_res.json()) == 1
    assert notices_res.json()[0]["notification_state"] == "pending"

    # Confirm via HTTP endpoint
    resolve_res = client.post(
        f"/project-knowledge/result-values/{flagged_val_id}/resolve",
        json={
            "workspace_id": "main",
            "action": "confirm",
            "reason": "Confirmed via HTTP API for downstream sensitivity run.",
        },
    )
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "confirmed"

    # Notification state transitions to 'read' once all exceptions in the batch are resolved
    notices_after = client.get("/project-knowledge/workspaces/main/result-notifications").json()
    assert len(notices_after) == 1
    assert notices_after[0]["notification_state"] == "read"

    # Downstream calculation consuming the confirmed value via source_result_value_id
    # carries unvalidated_source=True and unvalidated_input_value_ids=[flagged_val_id]
    script2 = r"""
import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 45.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
""".strip()
    body2 = _create_calc_run(
        client,
        script2,
        input_set={
            "coolant_pressure": {
                "value": 15.0,
                "unit": "MPa",
            }
        },
    )
    run2_id = body2["simulation_run"]["id"]
    batches_after = client.get("/project-knowledge/workspaces/main/result-batches").json()
    batch2 = next(b for b in batches_after if b["run_id"] == run2_id)
    assert flagged_val_id in batch2["unvalidated_input_value_ids"]
    assert batch2["values"][0]["unvalidated_source"] is True

    # Verify cross-workspace revision access is rejected on /validations
    rev_id, _ = _create_working_revision(workspace_id="main", key="rev-ws-isolation")
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ('other-ws', 'Other', 'other-ws', '2026-11-06T00:00:00+00:00', '2026-11-06T00:00:00+00:00')"
        )
        connection.commit()
    cross_res = client.get(f"/project-knowledge/workspaces/other-ws/revisions/{rev_id}/validations")
    assert cross_res.status_code == 404
    assert cross_res.json()["detail"]["code"] == "working_revision_missing"


def test_253_ac7_revalidation_returns_computed_applicability_digest_and_changes_with_set(client: TestClient) -> None:
    assert client is not None
    rev_id, _ = _create_working_revision(workspace_id="main", key="rev-ac7")
    before = revalidation_status("main", rev_id)
    assert before.applicability_set_digest
    assert before.applicability_set_digest != before.selected_validation_set_digest

    now = "2026-11-06T03:00:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, status, schema_version, created_at, updated_at,
                basis_kind, reconciliation_gate
            ) VALUES ('req-ac7', 'main', 'AC7 criterion', 'active', 1, ?, ?, 'acceptance_criterion', 'required')
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO requirement_applicability (
                id, workspace_id, requirement_id, target_kind, target_id, effect,
                lifecycle_state, created_at, updated_at
            ) VALUES ('app-ac7', 'main', 'req-ac7', 'workspace', 'main', 'include', 'active', ?, ?)
            """,
            (now, now),
        )
        connection.commit()

    after = revalidation_status("main", rev_id)
    assert "req-ac7" in after.mandatory_requirement_ids
    assert after.applicability_set_digest != before.applicability_set_digest
    assert after.applicability_set_digest != after.selected_validation_set_digest
