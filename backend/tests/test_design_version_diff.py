"""Offline comprehensive tests for Spec 392 — DESIGN-VERSION-DIFF-1."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.agents.hermes.knowledge_read_tool import dispatch_knowledge_read_tool
from app.modules.ai.agent_contracts import AgentSessionRef, StructuredToolCall
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddUnit, DraftQuantity, SetUnitParams
from app.modules.workspaces.version_diff import diff_design_versions


def _make_hermes_call(
    arguments: dict[str, Any],
    workspace_id: str,
    capability_id: str = "jarvis.knowledge_read",
) -> StructuredToolCall:
    now = datetime.now(UTC)
    session = AgentSessionRef(
        jarvis_thread_id="thread-diff-test",
        hermes_session_id="hermes-session-test",
        profile_id="isaac",
        workspace_id=workspace_id,
        generation=1,
        upstream_revision="dummy-upstream-rev",
    )
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id=capability_id,
        grant_id="grant-test",
        correlation_id="corr-diff-1",
        session_ref=session,
        arguments=arguments,
        requested_at=now,
        deadline_at=now + timedelta(seconds=60),
    )


@pytest.fixture
def diff_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up an isolated SQLite database and test data for cross-version diff."""
    data_root = tmp_path / "jarvis_data"
    data_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(data_root))
    initialize_database()

    now = "2026-11-28T10:00:00Z"
    later = "2026-11-28T11:00:00Z"

    project_id = "proj-1"
    v1_id = "ver-1"
    v1_ws_id = "ws-data-1"
    v2_id = "ver-2"
    v2_ws_id = "ws-data-2"

    proj2_id = "proj-2"
    v_cross_id = "ver-cross"
    v_cross_ws_id = "ws-data-cross"

    with open_sqlite_connection() as conn:
        # Projects
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (project_id, "Project Alpha", "proj-alpha", "First project", now, now),
        )
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'design_version', ?, ?)",
            (v1_ws_id, "Project Alpha · V1", f"design-{v1_ws_id}", "V1 data ws", now, now),
        )
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'design_version', ?, ?)",
            (v2_ws_id, "Project Alpha · V2", f"design-{v2_ws_id}", "V2 data ws", later, later),
        )

        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (proj2_id, "Project Beta", "proj-beta", "Second project", now, now),
        )
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'design_version', ?, ?)",
            (v_cross_ws_id, "Project Beta · VCross", f"design-{v_cross_ws_id}", "VCross data ws", now, now),
        )

        # Versions
        conn.execute(
            "INSERT INTO design_project_versions (id, project_id, data_workspace_id, parent_version_id, label, active, created_at, source_mode) "
            "VALUES (?, ?, ?, NULL, 'Variant A', 1, ?, 'empty')",
            (v1_id, project_id, v1_ws_id, now),
        )
        conn.execute(
            "INSERT INTO design_project_versions (id, project_id, data_workspace_id, parent_version_id, label, active, created_at, source_mode) "
            "VALUES (?, ?, ?, ?, 'Variant B', 0, ?, 'fork')",
            (v2_id, project_id, v2_ws_id, v1_id, later),
        )
        conn.execute(
            "INSERT INTO design_project_versions (id, project_id, data_workspace_id, parent_version_id, label, active, created_at, source_mode) "
            "VALUES (?, ?, ?, NULL, 'Cross Variant', 1, ?, 'empty')",
            (v_cross_id, proj2_id, v_cross_ws_id, now),
        )

        # Seed bluecad candidates and model versions for foreign keys
        for c_id in ("cand-geom-1", "cand-geom-2", "cand-geom-2-alt", "cand-geom-3", "cand-geom-4"):
            conn.execute(
                "INSERT INTO bluecad_candidates (id, workspace_id, brief_text, brief_digest, status, loop_config_json, created_at, updated_at) "
                "VALUES (?, ?, 'brief', 'dig', 'completed', '{}', ?, ?)",
                (c_id, project_id, now, now),
            )

        conn.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
            "VALUES ('ms-1', ?, 'Physics Spec', 'Question', ?, ?)",
            (project_id, now, now),
        )
        for mv_id in ("model-phys-1", "model-phys-2", "model-phys-3"):
            conn.execute(
                "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, created_at) "
                "VALUES (?, ?, 'ms-1', ?, ?)",
                (mv_id, project_id, mv_id, now),
            )

        # ---------------- Layer 1: Parameters ----------------
        # In V1: Unchanged param, Changed param, Removed param
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?)",
            ("p-1-same", v1_ws_id, "Operating Pressure", "P_op", "10.0", "bar", now, now),
        )
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
            ("p-1-chg", v1_ws_id, "Biomass Yield", "Y_x", "0.40", "g/g", now, now),
        )
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
            ("p-1-rem", v1_ws_id, "Cooling Temperature", "T_c", "20.0", "degC", now, now),
        )

        # In V2: Unchanged param, Changed param (0.40 -> 0.50), Added param
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?)",
            ("p-2-same", v2_ws_id, "Operating Pressure", "P_op", "10.0", "bar", later, later),
        )
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?)",
            ("p-2-chg", v2_ws_id, "Biomass Yield", "Y_x", "0.50", "g/g", later, later),
        )
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
            ("p-2-add", v2_ws_id, "Agitation Speed", "N_agit", "300.0", "rpm", later, later),
        )

        # ---------------- Layer 2: Components ----------------
        # In V1: Unchanged component, Changed component, Removed component
        conn.execute(
            "INSERT INTO design_components (id, project_id, version_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Reactor Shell', 1, ?, ?)",
            ("c-1-same", project_id, v1_id, now, now),
        )
        conn.execute(
            "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, updated_at) "
            "VALUES (?, 'cand-geom-1', 'rev-1', ?)",
            ("c-1-same", now),
        )
        conn.execute(
            "INSERT INTO design_component_models (component_id, model_version_id, node, category, source_sha256, contract_sha256, attached_at) "
            "VALUES (?, 'model-phys-1', 'Physics', 'calculation', 'sha-s1', 'sha-c1', ?)",
            ("c-1-same", now),
        )

        conn.execute(
            "INSERT INTO design_components (id, project_id, version_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Impeller', 1, ?, ?)",
            ("c-1-chg", project_id, v1_id, now, now),
        )
        conn.execute(
            "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, updated_at) "
            "VALUES (?, 'cand-geom-2', 'rev-1', ?)",
            ("c-1-chg", now),
        )
        conn.execute(
            "INSERT INTO design_component_models (component_id, model_version_id, node, category, source_sha256, contract_sha256, attached_at) "
            "VALUES (?, 'model-phys-2', 'Physics', 'calculation', 'sha-s2', 'sha-c2', ?)",
            ("c-1-chg", now),
        )

        conn.execute(
            "INSERT INTO design_components (id, project_id, version_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Baffle A', 1, ?, ?)",
            ("c-1-rem", project_id, v1_id, now, now),
        )
        conn.execute(
            "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, updated_at) "
            "VALUES (?, 'cand-geom-3', 'rev-1', ?)",
            ("c-1-rem", now),
        )

        # In V2:
        conn.execute(
            "INSERT INTO design_components (id, project_id, version_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Reactor Shell', 1, ?, ?)",
            ("c-2-same", project_id, v2_id, later, later),
        )
        conn.execute(
            "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, updated_at) "
            "VALUES (?, 'cand-geom-1', 'rev-2', ?)",
            ("c-2-same", later),
        )
        conn.execute(
            "INSERT INTO design_component_models (component_id, model_version_id, node, category, source_sha256, contract_sha256, attached_at) "
            "VALUES (?, 'model-phys-1', 'Physics', 'calculation', 'sha-s1', 'sha-c1', ?)",
            ("c-2-same", later),
        )

        conn.execute(
            "INSERT INTO design_components (id, project_id, version_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Impeller', 2, ?, ?)",
            ("c-2-chg", project_id, v2_id, later, later),
        )
        conn.execute(
            "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, updated_at) "
            "VALUES (?, 'cand-geom-2-alt', 'rev-2', ?)",
            ("c-2-chg", later),
        )
        conn.execute(
            "INSERT INTO design_component_models (component_id, model_version_id, node, category, source_sha256, contract_sha256, attached_at) "
            "VALUES (?, 'model-phys-3', 'Physics', 'calculation', 'sha-s3', 'sha-c3', ?)",
            ("c-2-chg", later),
        )

        conn.execute(
            "INSERT INTO design_components (id, project_id, version_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Sparger', 1, ?, ?)",
            ("c-2-add", project_id, v2_id, later, later),
        )
        conn.execute(
            "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, updated_at) "
            "VALUES (?, 'cand-geom-4', 'rev-2', ?)",
            ("c-2-add", later),
        )

        # ---------------- Layer 4: Simulation Results / KPIs ----------------
        # V1 runs and batches
        conn.execute(
            "INSERT INTO simulation_runs (id, workspace_id, run_label, status, created_at) "
            "VALUES ('run-1', ?, 'Run 1', 'completed', ?)",
            (v1_ws_id, now),
        )
        conn.execute(
            "INSERT INTO simulation_result_batches (id, workspace_id, run_id, source_payload_digest, extractor_version, created_at) "
            "VALUES ('batch-1', ?, 'run-1', 'dig-1', 'ext-1', ?)",
            (v1_ws_id, now),
        )
        conn.execute(
            "INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, quantity, value_text, unit, conditions_json, status, created_at) "
            "VALUES ('srv-1-same', 'batch-1', 'Net Duty', 'dig-1', 'ext-1', '1', 'power', '100.0', 'kW', '{}', 'passed', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, quantity, value_text, unit, conditions_json, status, created_at) "
            "VALUES ('srv-1-chg', 'batch-1', 'Biomass Peak', 'dig-1', 'ext-1', '1', 'concentration', '2.0', 'g/L', '{}', 'passed', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, quantity, value_text, unit, conditions_json, status, created_at) "
            "VALUES ('srv-1-rem', 'batch-1', 'Waste Flow', 'dig-1', 'ext-1', '1', 'flow', '50.0', 'm3/h', '{}', 'passed', ?)",
            (now,),
        )

        # V2 runs and batches
        conn.execute(
            "INSERT INTO simulation_runs (id, workspace_id, run_label, status, created_at) "
            "VALUES ('run-2', ?, 'Run 2', 'completed', ?)",
            (v2_ws_id, later),
        )
        conn.execute(
            "INSERT INTO simulation_result_batches (id, workspace_id, run_id, source_payload_digest, extractor_version, created_at) "
            "VALUES ('batch-2', ?, 'run-2', 'dig-2', 'ext-1', ?)",
            (v2_ws_id, later),
        )
        conn.execute(
            "INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, quantity, value_text, unit, conditions_json, status, created_at) "
            "VALUES ('srv-2-same', 'batch-2', 'Net Duty', 'dig-2', 'ext-1', '1', 'power', '100.0', 'kW', '{}', 'passed', ?)",
            (later,),
        )
        conn.execute(
            "INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, quantity, value_text, unit, conditions_json, status, created_at) "
            "VALUES ('srv-2-chg', 'batch-2', 'Biomass Peak', 'dig-2', 'ext-1', '1', 'concentration', '2.5', 'g/L', '{}', 'confirmed', ?)",
            (later,),
        )
        conn.execute(
            "INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, quantity, value_text, unit, conditions_json, status, created_at) "
            "VALUES ('srv-2-add', 'batch-2', 'Dissolved Oxygen', 'dig-2', 'ext-1', '1', 'concentration', '8.0', 'mg/L', '{}', 'passed', ?)",
            (later,),
        )

        # ---------------- Layer 5: Requirement Validations ----------------
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES ('req-same', ?, 'Biomass purity > 90%', 'accepted', ?, ?)",
            (v1_ws_id, now, now),
        )
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES ('req-chg', ?, 'Cell growth rate > 0.05 1/h', 'accepted', ?, ?)",
            (v1_ws_id, now, now),
        )
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES ('req-rem', ?, 'Cooling utility cost < 100 USD/h', 'draft', ?, ?)",
            (v1_ws_id, now, now),
        )
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES ('req-add', ?, 'Minimum residence time > 2 h', 'accepted', ?, ?)",
            (v2_ws_id, later, later),
        )

        # Revisions for validations
        conn.execute(
            "INSERT INTO project_knowledge_revisions (id, workspace_id, parent_kind, state, change_set_digest, change_set_json, projected_state_digest, origin, created_at, accepted_at) "
            "VALUES ('pkr-1', ?, 'working', 'reconciled', 'd1', '[]', 'p1', 'apply', ?, ?)",
            (v1_ws_id, now, now),
        )
        conn.execute(
            "INSERT INTO project_knowledge_revisions (id, workspace_id, parent_kind, state, change_set_digest, change_set_json, projected_state_digest, origin, created_at, accepted_at) "
            "VALUES ('pkr-2', ?, 'working', 'reconciled', 'd2', '[]', 'p2', 'apply', ?, ?)",
            (v2_ws_id, later, later),
        )

        conn.execute(
            "INSERT INTO project_knowledge_validation (id, workspace_id, working_revision_id, requirement_id, requirement_updated_at, rule_version, validated_basis_digest, applicability_set_digest, validator_id, validator_version, outcome, reason_code, created_at) "
            "VALUES ('val-1-same', ?, 'pkr-1', 'req-same', ?, '1', 'b1', 'a1', 'val-1', '1', 'pass', 'criteria_met', ?)",
            (v1_ws_id, now, now),
        )
        conn.execute(
            "INSERT INTO project_knowledge_validation (id, workspace_id, working_revision_id, requirement_id, requirement_updated_at, rule_version, validated_basis_digest, applicability_set_digest, validator_id, validator_version, outcome, reason_code, created_at) "
            "VALUES ('val-1-chg', ?, 'pkr-1', 'req-chg', ?, '1', 'b1', 'a1', 'val-1', '1', 'pass', 'criteria_met', ?)",
            (v1_ws_id, now, now),
        )
        conn.execute(
            "INSERT INTO project_knowledge_validation (id, workspace_id, working_revision_id, requirement_id, requirement_updated_at, rule_version, validated_basis_digest, applicability_set_digest, validator_id, validator_version, outcome, reason_code, created_at) "
            "VALUES ('val-1-rem', ?, 'pkr-1', 'req-rem', ?, '1', 'b1', 'a1', 'val-1', '1', 'pass', 'criteria_met', ?)",
            (v1_ws_id, now, now),
        )

        # In V2:
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES ('req-2-same', ?, 'Biomass purity > 90%', 'accepted', ?, ?)",
            (v2_ws_id, later, later),
        )
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES ('req-2-chg', ?, 'Cell growth rate > 0.05 1/h', 'accepted', ?, ?)",
            (v2_ws_id, later, later),
        )
        conn.execute(
            "INSERT INTO project_knowledge_validation (id, workspace_id, working_revision_id, requirement_id, requirement_updated_at, rule_version, validated_basis_digest, applicability_set_digest, validator_id, validator_version, outcome, reason_code, created_at) "
            "VALUES ('val-2-same', ?, 'pkr-2', 'req-2-same', ?, '1', 'b2', 'a2', 'val-1', '1', 'pass', 'criteria_met', ?)",
            (v2_ws_id, later, later),
        )
        conn.execute(
            "INSERT INTO project_knowledge_validation (id, workspace_id, working_revision_id, requirement_id, requirement_updated_at, rule_version, validated_basis_digest, applicability_set_digest, validator_id, validator_version, outcome, reason_code, created_at) "
            "VALUES ('val-2-chg', ?, 'pkr-2', 'req-2-chg', ?, '1', 'b2', 'a2', 'val-1', '1', 'fail', 'threshold_violated', ?)",
            (v2_ws_id, later, later),
        )
        conn.execute(
            "INSERT INTO project_knowledge_validation (id, workspace_id, working_revision_id, requirement_id, requirement_updated_at, rule_version, validated_basis_digest, applicability_set_digest, validator_id, validator_version, outcome, reason_code, created_at) "
            "VALUES ('val-2-add', ?, 'pkr-2', 'req-add', ?, '1', 'b2', 'a2', 'val-1', '1', 'pass', 'criteria_met', ?)",
            (v2_ws_id, later, later),
        )
        conn.commit()

    # ---------------- Layer 3: Process Drafts on Disk ----------------
    # Create drafts in v1_ws_id
    d1 = draft.create_draft(v1_ws_id, "Main Train")
    d1_id = d1["draft_id"]
    draft.patch(
        v1_ws_id,
        d1_id,
        d1["revision"],
        [
            AddUnit(op="add_unit", id="u-h1", type="Heater", tag="H1", x=0, y=0),
            SetUnitParams(
                op="set_unit_params",
                unit="u-h1",
                values={"outlet_temperature": DraftQuantity(value=95.0, unit="degC")},
            ),
            AddUnit(op="add_unit", id="u-p1", type="Pump", tag="P1", x=100, y=100),
        ],
    )

    # Create draft in v2_ws_id
    d2 = draft.create_draft(v2_ws_id, "Main Train")
    d2_id = d2["draft_id"]
    draft.patch(
        v2_ws_id,
        d2_id,
        d2["revision"],
        [
            AddUnit(op="add_unit", id="u-h1", type="Heater", tag="H1", x=0, y=0),
            SetUnitParams(
                op="set_unit_params",
                unit="u-h1",
                values={"outlet_temperature": DraftQuantity(value=105.0, unit="degC")},
            ),
            AddUnit(op="add_unit", id="u-p2", type="Pump", tag="P2", x=200, y=200),
        ],
    )

    return {
        "project_id": project_id,
        "v1_id": v1_id,
        "v1_ws_id": v1_ws_id,
        "v2_id": v2_id,
        "v2_ws_id": v2_ws_id,
        "proj2_id": proj2_id,
        "v_cross_id": v_cross_id,
    }


def test_diff_design_versions_all_five_layers(diff_env: dict[str, str]) -> None:
    """Verify all 5 layers, deltas, counts, and summary via Python function and HTTP endpoint."""
    proj = diff_env["project_id"]
    v1 = diff_env["v1_id"]
    v2 = diff_env["v2_id"]

    with open_sqlite_connection() as conn:
        diff = diff_design_versions(conn, proj, v1, v2)

    assert diff.project_id == proj
    assert diff.base_version_id == v1
    assert diff.target_version_id == v2
    assert diff.has_changes is True

    # 1. Parameters
    assert [p.name for p in diff.parameters_added] == ["Agitation Speed"]
    assert diff.parameters_added[0].target_value == "300.0"
    assert diff.parameters_added[0].target_unit == "rpm"

    assert [p.name for p in diff.parameters_removed] == ["Cooling Temperature"]
    assert diff.parameters_removed[0].base_value == "20.0"

    assert [p.name for p in diff.parameters_changed] == ["Biomass Yield"]
    chg_p = diff.parameters_changed[0]
    assert chg_p.base_value == "0.40"
    assert chg_p.target_value == "0.50"
    assert pytest.approx(chg_p.numeric_delta, rel=1e-5) == 0.10
    assert pytest.approx(chg_p.relative_delta_pct, rel=1e-5) == 25.0
    assert chg_p.base_status == "candidate"
    assert chg_p.target_status == "accepted"

    # 2. Components
    assert [c.name for c in diff.components_added] == ["Sparger"]
    assert diff.components_added[0].target_candidate_id == "cand-geom-4"

    assert [c.name for c in diff.components_removed] == ["Baffle A"]
    assert diff.components_removed[0].base_candidate_id == "cand-geom-3"

    assert [c.name for c in diff.components_changed] == ["Impeller"]
    chg_c = diff.components_changed[0]
    assert chg_c.geometry_candidate_changed is True
    assert chg_c.base_candidate_id == "cand-geom-2"
    assert chg_c.target_candidate_id == "cand-geom-2-alt"
    assert chg_c.models_changed == ["model-phys-3"]

    # 3. Process draft units
    assert diff.units_added == ["Main Train:P2"]
    assert diff.units_removed == ["Main Train:P1"]
    assert len(diff.unit_params_changed) >= 1
    up_chg = next(u for u in diff.unit_params_changed if u.param_key == "outlet_temperature")
    assert up_chg.unit_tag == "H1"
    assert up_chg.base_value == 95.0
    assert up_chg.target_value == 105.0
    assert pytest.approx(up_chg.numeric_delta, rel=1e-5) == 10.0

    # 4. Simulation results / KPIs
    assert [r.output_name for r in diff.results_added] == ["Dissolved Oxygen"]
    assert [r.output_name for r in diff.results_removed] == ["Waste Flow"]
    assert [r.output_name for r in diff.results_changed] == ["Biomass Peak"]
    r_chg = diff.results_changed[0]
    assert r_chg.base_value == "2.0"
    assert r_chg.target_value == "2.5"
    assert pytest.approx(r_chg.numeric_delta, rel=1e-5) == 0.5
    assert pytest.approx(r_chg.relative_delta_pct, rel=1e-5) == 25.0
    assert r_chg.base_status == "passed"
    assert r_chg.target_status == "confirmed"

    # 5. Requirement validations
    assert [v.statement for v in diff.validations_added] == ["Minimum residence time > 2 h"]
    assert [v.statement for v in diff.validations_removed] == ["Cooling utility cost < 100 USD/h"]
    assert [v.statement for v in diff.validations_changed] == ["Cell growth rate > 0.05 1/h"]
    v_chg = diff.validations_changed[0]
    assert v_chg.base_outcome == "pass"
    assert v_chg.target_outcome == "fail"
    assert v_chg.base_reason_code == "criteria_met"
    assert v_chg.target_reason_code == "threshold_violated"

    # Counts and summary
    assert diff.counts["parameters_added"] == 1
    assert diff.counts["parameters_removed"] == 1
    assert diff.counts["parameters_changed"] == 1
    assert diff.counts["components_added"] == 1
    assert diff.counts["components_removed"] == 1
    assert diff.counts["components_changed"] == 1
    assert diff.counts["units_added"] == 1
    assert diff.counts["units_removed"] == 1
    assert diff.counts["unit_params_changed"] >= 1
    assert diff.counts["results_added"] == 1
    assert diff.counts["results_removed"] == 1
    assert diff.counts["results_changed"] == 1
    assert diff.counts["validations_added"] == 1
    assert diff.counts["validations_removed"] == 1
    assert diff.counts["validations_changed"] == 1

    assert "1 parameter added" in diff.summary
    assert "1 parameter changed" in diff.summary
    assert "1 component changed" in diff.summary
    assert "1 KPI changed" in diff.summary
    assert "1 validation changed" in diff.summary

    # Test via HTTP endpoint
    with TestClient(app) as client:
        resp = client.get(
            f"/workspaces/{proj}/design-versions/diff",
            params={"base_version_id": v1, "target_version_id": v2},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["has_changes"] is True
        assert data["summary"] == diff.summary
        assert data["counts"] == diff.counts


def test_identical_version_zero_diff(diff_env: dict[str, str]) -> None:
    """Identical versions return has_changes=False, zero counts, and empty lists."""
    proj = diff_env["project_id"]
    v1 = diff_env["v1_id"]

    with open_sqlite_connection() as conn:
        diff = diff_design_versions(conn, proj, v1, v1)

    assert diff.has_changes is False
    assert diff.summary == "No differences between versions."
    assert all(c == 0 for c in diff.counts.values())
    assert diff.parameters_added == []
    assert diff.parameters_removed == []
    assert diff.parameters_changed == []
    assert diff.components_added == []
    assert diff.components_removed == []
    assert diff.components_changed == []
    assert diff.units_added == []
    assert diff.units_removed == []
    assert diff.unit_params_changed == []
    assert diff.results_added == []
    assert diff.results_removed == []
    assert diff.results_changed == []
    assert diff.validations_added == []
    assert diff.validations_removed == []
    assert diff.validations_changed == []

    with TestClient(app) as client:
        resp = client.get(
            f"/workspaces/{proj}/design-versions/diff",
            params={"base_version_id": v1, "target_version_id": v1},
        )
        assert resp.status_code == 200
        assert resp.json()["has_changes"] is False
        assert resp.json()["summary"] == "No differences between versions."


def test_diff_404_on_unknown_or_cross_project_ids(diff_env: dict[str, str]) -> None:
    """Refuse nonexistent project or versions, and cross-project version IDs with 404."""
    proj = diff_env["project_id"]
    v1 = diff_env["v1_id"]
    v2 = diff_env["v2_id"]
    v_cross = diff_env["v_cross_id"]

    with TestClient(app) as client:
        # Unknown project
        assert client.get("/workspaces/nonexistent-proj/design-versions/diff",
                          params={"base_version_id": v1, "target_version_id": v2}).status_code == 404

        # Unknown base version
        assert client.get(f"/workspaces/{proj}/design-versions/diff",
                          params={"base_version_id": "nonexistent-v", "target_version_id": v2}).status_code == 404

        # Unknown target version
        assert client.get(f"/workspaces/{proj}/design-versions/diff",
                          params={"base_version_id": v1, "target_version_id": "nonexistent-v"}).status_code == 404

        # Cross-project version (v_cross belongs to proj-2)
        assert client.get(f"/workspaces/{proj}/design-versions/diff",
                          params={"base_version_id": v1, "target_version_id": v_cross}).status_code == 404
        assert client.get(f"/workspaces/{proj}/design-versions/diff",
                          params={"base_version_id": v_cross, "target_version_id": v2}).status_code == 404


def test_hermes_knowledge_read_design_versions(diff_env: dict[str, str]) -> None:
    """Hermes jarvis_knowledge_read(view="design_versions") works from project and version data workspaces."""
    proj = diff_env["project_id"]
    v1_ws = diff_env["v1_ws_id"]

    # 1. From parent project workspace
    call_proj = _make_hermes_call({"view": "design_versions"}, workspace_id=proj)
    res_proj, err_proj = dispatch_knowledge_read_tool(call_proj, proj, "default_err")
    assert err_proj == "default_err"
    assert res_proj is not None
    assert res_proj["view"] == "design_versions"
    assert res_proj["workspace_id"] == proj
    assert len(res_proj["design_versions"]) == 2
    v_labels = [v["label"] for v in res_proj["design_versions"]]
    assert "Variant A" in v_labels
    assert "Variant B" in v_labels
    # Check fields
    v_a = next(v for v in res_proj["design_versions"] if v["label"] == "Variant A")
    assert v_a["active"] is True
    assert v_a["parent_version_id"] is None
    assert v_a["source_mode"] == "empty"
    assert v_a["data_workspace_id"] == v1_ws

    # 2. From version data workspace
    call_ver = _make_hermes_call({"view": "design_versions"}, workspace_id=v1_ws)
    res_ver, err_ver = dispatch_knowledge_read_tool(call_ver, v1_ws, "default_err")
    assert err_ver == "default_err"
    assert res_ver is not None
    assert res_ver["view"] == "design_versions"
    assert res_ver["workspace_id"] == proj
    assert len(res_ver["design_versions"]) == 2

    # 3. Unknown workspace -> not_found
    call_bad = _make_hermes_call({"view": "design_versions"}, workspace_id="unknown-ws")
    res_bad, err_bad = dispatch_knowledge_read_tool(call_bad, "unknown-ws", "default_err")
    assert res_bad is None
    assert err_bad == "not_found"


def test_hermes_knowledge_read_version_diff(diff_env: dict[str, str]) -> None:
    """Hermes jarvis_knowledge_read(view="version_diff") works from project and version ws, and validates args."""
    proj = diff_env["project_id"]
    v1 = diff_env["v1_id"]
    v2 = diff_env["v2_id"]
    v1_ws = diff_env["v1_ws_id"]
    v_cross = diff_env["v_cross_id"]

    # 1. From parent project workspace
    call_proj = _make_hermes_call(
        {"view": "version_diff", "base_version_id": v1, "target_version_id": v2},
        workspace_id=proj,
    )
    res_proj, err_proj = dispatch_knowledge_read_tool(call_proj, proj, "default_err")
    assert err_proj == "default_err"
    assert res_proj is not None
    assert res_proj["view"] == "version_diff"
    assert res_proj["has_changes"] is True
    assert res_proj["base_version_id"] == v1
    assert res_proj["target_version_id"] == v2
    assert "counts" in res_proj
    assert "summary" in res_proj

    # 2. From version data workspace
    call_ver = _make_hermes_call(
        {"view": "version_diff", "base_version_id": v1, "target_version_id": v2},
        workspace_id=v1_ws,
    )
    res_ver, err_ver = dispatch_knowledge_read_tool(call_ver, v1_ws, "default_err")
    assert err_ver == "default_err"
    assert res_ver is not None
    assert res_ver["has_changes"] is True

    # 3. Identical versions zero diff
    call_same = _make_hermes_call(
        {"view": "version_diff", "base_version_id": v1, "target_version_id": v1},
        workspace_id=proj,
    )
    res_same, err_same = dispatch_knowledge_read_tool(call_same, proj, "default_err")
    assert err_same == "default_err"
    assert res_same is not None
    assert res_same["has_changes"] is False
    assert res_same["summary"] == "No differences between versions."

    # 4. Missing arguments -> invalid_arguments
    call_missing = _make_hermes_call(
        {"view": "version_diff", "base_version_id": v1},
        workspace_id=proj,
    )
    res_m, err_m = dispatch_knowledge_read_tool(call_missing, proj, "default_err")
    assert res_m is None
    assert err_m == "invalid_arguments"

    # Non-string argument
    call_nonstr = _make_hermes_call(
        {"view": "version_diff", "base_version_id": 123, "target_version_id": v2},  # type: ignore
        workspace_id=proj,
    )
    res_ns, err_ns = dispatch_knowledge_read_tool(call_nonstr, proj, "default_err")
    assert res_ns is None
    assert err_ns == "invalid_arguments"

    # 5. Cross-project version ID -> not_found
    call_cross = _make_hermes_call(
        {"view": "version_diff", "base_version_id": v1, "target_version_id": v_cross},
        workspace_id=proj,
    )
    res_c, err_c = dispatch_knowledge_read_tool(call_cross, proj, "default_err")
    assert res_c is None
    assert err_c == "not_found"

    # 6. Unknown version ID -> not_found
    call_unk = _make_hermes_call(
        {"view": "version_diff", "base_version_id": v1, "target_version_id": "ver-unknown"},
        workspace_id=proj,
    )
    res_u, err_u = dispatch_knowledge_read_tool(call_unk, proj, "default_err")
    assert res_u is None
    assert err_u == "not_found"
