from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.agents.hermes import broker_mcp
from app.modules.agents.hermes.knowledge_read_tool import (
    _ALLOWED_VIEWS,
    _VIEW_ALLOWED_KEYS,
    dispatch_knowledge_read_tool,
)
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.flowsheet.freshness import (
    find_upstream_levers,
    find_upstream_levers_from_connection,
    preview_change_impact,
    preview_change_impact_from_connection,
)
from app.modules.flowsheet.service import FlowsheetError


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "jarvis_data"))
    monkeypatch.setenv("DATABASE_URL", "must-not-enter-flowsheet")
    get_settings.cache_clear()

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def _seed_test_graph(workspace_id: str = "main") -> dict[str, str]:
    now = "2026-07-20T13:30:00+00:00"
    completed_t1 = "2026-07-20T13:30:10+00:00"  # 10s duration
    completed_t2 = "2026-07-20T13:30:20+00:00"  # 20s duration
    ids = {
        "assumption": "asm-test-01",
        "parameter_p1": "param-test-feed",
        "model_spec": "ms-test-01",
        "impl_artifact": "art-impl-01",
        "model_version": "mv-test-01",
        "run_r1": "run-test-01",
        "runner_job_rj1": "runner-test-01",
        "run_r2": "run-test-02",
        "runner_job_rj2": "runner-test-02",
        "decision": "dec-test-01",
        "output_param": "param-test-out",
    }

    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, 'Test Workspace', 'ws-test', 'Flowsheet test ws', 'active', ?, ?)",
            (workspace_id, now, now),
        )

        # 1. Upstream Assumption
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, origin, created_at, updated_at) "
            "VALUES (?, ?, 'Steady state feed assumption', 'process', 'high', 'accepted', 'user', ?, ?)",
            (ids["assumption"], workspace_id, now, now),
        )

        # 2. Input Parameter connected to Assumption via source_ref
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, value, unit, value_status, status, origin, source_ref, created_at, updated_at) "
            "VALUES (?, ?, 'Feed Rate', '100', 'kg/h', 'accepted', 'accepted', 'user', ?, ?, ?)",
            (ids["parameter_p1"], workspace_id, f"assumption:{ids['assumption']}", now, now),
        )

        # 3. Model Spec & Artifact
        conn.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
            "VALUES (?, ?, 'Reactor Model', 'Yield sizing', ?, ?)",
            (ids["model_spec"], workspace_id, now, now),
        )
        conn.execute(
            "INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, mime_type, sha256, status, created_at) "
            "VALUES (?, ?, 'reactor_sim.py', '/mock/reactor_sim.py', 'python_script', 'text/x-python', ?, 'registered', ?)",
            (ids["impl_artifact"], workspace_id, "a" * 64, now),
        )

        # 4. Model Version
        conn.execute(
            "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, implementation_artifact_id, implementation_kind, status, created_at) "
            "VALUES (?, ?, ?, 'v1.0', ?, 'calc_v0', 'ready', ?)",
            (ids["model_version"], workspace_id, ids["model_spec"], ids["impl_artifact"], now),
        )

        # 5. Simulation Run 1 (depends on parameter_p1)
        input_payload_r1 = {
            "feed_rate": {
                "value": 100.0,
                "unit": "kg/h",
                "source_parameter_id": ids["parameter_p1"],
            }
        }
        conn.execute(
            "INSERT INTO simulation_runs (id, workspace_id, model_version_id, run_label, status, input_payload, output_payload, started_at, completed_at, created_at) "
            "VALUES (?, ?, ?, 'Run 01', 'succeeded', ?, ?, ?, ?, ?)",
            (
                ids["run_r1"],
                workspace_id,
                ids["model_version"],
                json.dumps(input_payload_r1, sort_keys=True),
                json.dumps({"yield": 0.85}, sort_keys=True),
                now,
                completed_t1,
                now,
            ),
        )

        # Runner Job 1
        conn.execute(
            "INSERT INTO runner_jobs (id, workspace_id, simulation_run_id, runner_type, status, script_path, script_sha256, "
            "implementation_kind, working_dir, input_file, output_dir, timeout_seconds, max_stdout_bytes, max_stderr_bytes, "
            "max_output_json_bytes, max_artifact_bytes, created_at, updated_at) "
            "VALUES (?, ?, ?, 'python_local', 'succeeded', '/mock/reactor_sim.py', ?, 'calc_v0', '/mock/work', "
            "'/mock/in.json', '/mock/out', 60, 1000, 1000, 1000, 1000, ?, ?)",
            (ids["runner_job_rj1"], workspace_id, ids["run_r1"], "b" * 64, now, completed_t1),
        )

        # Historical completed runner job with known duration (10s and 20s) in simulation_runs
        conn.execute(
            "INSERT INTO simulation_runs (id, workspace_id, model_version_id, run_label, status, input_payload, output_payload, started_at, completed_at, created_at) "
            "VALUES (?, ?, ?, 'Run 02', 'succeeded', ?, ?, ?, ?, ?)",
            (
                ids["run_r2"],
                workspace_id,
                ids["model_version"],
                json.dumps({"dummy": 1}, sort_keys=True),
                json.dumps({"dummy": 2}, sort_keys=True),
                now,
                completed_t2,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO runner_jobs (id, workspace_id, simulation_run_id, runner_type, status, script_path, script_sha256, "
            "implementation_kind, working_dir, input_file, output_dir, timeout_seconds, max_stdout_bytes, max_stderr_bytes, "
            "max_output_json_bytes, max_artifact_bytes, created_at, updated_at) "
            "VALUES (?, ?, ?, 'python_local', 'succeeded', '/mock/reactor_sim.py', ?, 'calc_v0', '/mock/work', "
            "'/mock/in.json', '/mock/out', 60, 1000, 1000, 1000, 1000, ?, ?)",
            (ids["runner_job_rj2"], workspace_id, ids["run_r2"], "c" * 64, now, completed_t2),
        )

        # 6. Decision informed by Run 1
        conn.execute(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, status, origin, linked_run_id, created_at, updated_at) "
            "VALUES (?, ?, 'Operating feed decision', 'Proceed with feed configuration', 'accepted', 'user', ?, ?, ?)",
            (ids["decision"], workspace_id, ids["run_r1"], now, now),
        )

        # 7. Output parameter downstream of Decision
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, value, unit, value_status, status, origin, source_ref, created_at, updated_at) "
            "VALUES (?, ?, 'Confirmed Output Rate', '95', 'kg/h', 'accepted', 'accepted', 'user', ?, ?, ?)",
            (ids["output_param"], workspace_id, f"decision:{ids['decision']}", now, now),
        )

        conn.commit()
    return ids


def _make_hermes_call(arguments: dict[str, Any], workspace_id: str = "main") -> StructuredToolCall:
    now = datetime.now(UTC)
    session = AgentSessionRef(
        jarvis_thread_id="thread-test-impact",
        hermes_session_id="hermes-session-test",
        profile_id="isaac",
        workspace_id=workspace_id,
        generation=1,
        upstream_revision="d0ee69151234567890abcdef",
    )
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id="jarvis.knowledge_read",
        grant_id="grant-impact-test",
        correlation_id="corr-1",
        session_ref=session,
        arguments=arguments,
        requested_at=now,
        deadline_at=now + timedelta(seconds=60),
    )


def _make_hermes_grant(workspace_id: str = "main") -> CapabilityGrantRef:
    now = datetime.now(UTC)
    return CapabilityGrantRef(
        grant_id="grant-impact-test",
        capability_id="jarvis.knowledge_read",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id="thread-test-impact"),
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10),
    )


def test_downstream_change_impact_from_parameter(isolated_app: TestClient) -> None:
    ids = _seed_test_graph()

    impact = preview_change_impact("main", f"parameter:{ids['parameter_p1']}")

    assert impact.workspace_id == "main"
    assert impact.source_ref == f"parameter:{ids['parameter_p1']}"
    assert impact.source_kind == "parameter"

    affected_refs = [n.ref for n in impact.affected_nodes]
    assert f"simulation_run:{ids['run_r1']}" in affected_refs
    assert f"runner_job:{ids['runner_job_rj1']}" in affected_refs
    assert f"decision:{ids['decision']}" in affected_refs
    assert f"parameter:{ids['output_param']}" in affected_refs

    # required_reruns includes simulation_run and runner_job
    assert f"simulation_run:{ids['run_r1']}" in impact.required_reruns
    assert f"runner_job:{ids['runner_job_rj1']}" in impact.required_reruns
    assert f"decision:{ids['decision']}" not in impact.required_reruns
    assert impact.required_reruns == sorted(impact.required_reruns)

    # affected_decisions includes decision
    assert impact.affected_decisions == [f"decision:{ids['decision']}"]

    # Counts
    assert impact.affected_count == len(impact.affected_nodes)
    assert impact.counts_by_kind["simulation_run"] >= 1
    assert impact.counts_by_kind["runner_job"] >= 1
    assert impact.counts_by_kind["decision"] >= 1
    assert impact.counts_by_kind["parameter"] >= 1

    # Deterministic order by (distance, kind, ref)
    for i in range(len(impact.affected_nodes) - 1):
        curr = impact.affected_nodes[i]
        nxt = impact.affected_nodes[i + 1]
        assert (curr.distance, curr.kind, curr.ref) <= (nxt.distance, nxt.kind, nxt.ref)

    # Shortest path checks
    for node in impact.affected_nodes:
        assert node.distance >= 1
        assert node.path[0] == f"parameter:{ids['parameter_p1']}"
        assert node.path[-1] == node.ref
        assert len(node.path) - 1 == node.distance

    # Estimated rerun seconds (historical runs: 10s and 20s -> median 15s; 2 reruns -> 30.0s)
    assert impact.estimated_rerun_seconds is not None
    assert impact.estimated_rerun_seconds > 0

    # Strictly read-only check: zero rows added to freshness_invalidations or freshness_marks
    with open_sqlite_connection() as conn:
        inval_count = conn.execute("SELECT COUNT(*) AS c FROM freshness_invalidations").fetchone()["c"]
        mark_count = conn.execute("SELECT COUNT(*) AS c FROM freshness_marks").fetchone()["c"]
        assert inval_count == 0
        assert mark_count == 0


def test_downstream_change_impact_from_assumption_and_decision(isolated_app: TestClient) -> None:
    ids = _seed_test_graph()

    # Impact from assumption
    asm_impact = preview_change_impact("main", f"assumption:{ids['assumption']}")
    assert asm_impact.source_ref == f"assumption:{ids['assumption']}"
    assert asm_impact.source_kind == "assumption"
    asm_refs = {n.ref for n in asm_impact.affected_nodes}
    assert f"parameter:{ids['parameter_p1']}" in asm_refs
    assert f"simulation_run:{ids['run_r1']}" in asm_refs
    assert f"runner_job:{ids['runner_job_rj1']}" in asm_refs
    assert f"decision:{ids['decision']}" in asm_refs
    assert f"parameter:{ids['output_param']}" in asm_refs

    # Impact from decision
    dec_impact = preview_change_impact("main", f"decision:{ids['decision']}")
    assert dec_impact.source_ref == f"decision:{ids['decision']}"
    assert dec_impact.source_kind == "decision"
    dec_refs = {n.ref for n in dec_impact.affected_nodes}
    assert f"parameter:{ids['output_param']}" in dec_refs
    assert f"simulation_run:{ids['run_r1']}" not in dec_refs
    assert dec_impact.required_reruns == []
    assert dec_impact.estimated_rerun_seconds is None


def test_upstream_levers_from_decision_and_run(isolated_app: TestClient) -> None:
    ids = _seed_test_graph()

    # Levers for decision
    dec_levers = find_upstream_levers("main", f"decision:{ids['decision']}")
    assert dec_levers.target_ref == f"decision:{ids['decision']}"
    assert dec_levers.target_kind == "decision"

    lever_refs = [lever.ref for lever in dec_levers.levers]
    assert f"simulation_run:{ids['run_r1']}" in lever_refs
    assert f"parameter:{ids['parameter_p1']}" in lever_refs
    assert f"assumption:{ids['assumption']}" in lever_refs
    assert f"decision:{ids['decision']}" not in lever_refs

    # Distance ordering
    for i in range(len(dec_levers.levers) - 1):
        curr = dec_levers.levers[i]
        nxt = dec_levers.levers[i + 1]
        assert (curr.distance, curr.kind, curr.ref) <= (nxt.distance, nxt.kind, nxt.ref)

    # Path orientation: from lever to target_ref
    for lever in dec_levers.levers:
        assert lever.path[0] == lever.ref
        assert lever.path[-1] == f"decision:{ids['decision']}"
        assert len(lever.path) - 1 == lever.distance

    # Levers for simulation_run
    run_levers = find_upstream_levers("main", f"simulation_run:{ids['run_r1']}")
    run_lever_refs = [lever.ref for lever in run_levers.levers]
    assert f"parameter:{ids['parameter_p1']}" in run_lever_refs
    assert f"assumption:{ids['assumption']}" in run_lever_refs
    assert f"decision:{ids['decision']}" not in run_lever_refs

    for lever in run_levers.levers:
        assert lever.path[0] == lever.ref
        assert lever.path[-1] == f"simulation_run:{ids['run_r1']}"


def test_http_routes_and_error_handling(isolated_app: TestClient) -> None:
    ids = _seed_test_graph()

    # 1. Valid impact
    res_impact = isolated_app.get(f"/workspaces/main/flowsheet/nodes/parameter:{ids['parameter_p1']}/impact")
    assert res_impact.status_code == 200, res_impact.text
    body_impact = res_impact.json()
    assert body_impact["workspace_id"] == "main"
    assert body_impact["source_ref"] == f"parameter:{ids['parameter_p1']}"
    assert isinstance(body_impact["affected_nodes"], list)
    assert len(body_impact["affected_nodes"]) == body_impact["affected_count"]

    # 2. Valid levers
    res_levers = isolated_app.get(f"/workspaces/main/flowsheet/nodes/decision:{ids['decision']}/levers")
    assert res_levers.status_code == 200, res_levers.text
    body_levers = res_levers.json()
    assert body_levers["workspace_id"] == "main"
    assert body_levers["target_ref"] == f"decision:{ids['decision']}"
    assert isinstance(body_levers["levers"], list)
    assert len(body_levers["levers"]) == body_levers["lever_count"]

    # 3. 404 for unknown node_ref
    res_unknown_node = isolated_app.get("/workspaces/main/flowsheet/nodes/parameter:non-existent-param-id/impact")
    assert res_unknown_node.status_code == 404, res_unknown_node.text
    assert res_unknown_node.json()["detail"]["code"] == "flowsheet_node_not_found"

    res_unknown_levers = isolated_app.get("/workspaces/main/flowsheet/nodes/decision:non-existent-dec-id/levers")
    assert res_unknown_levers.status_code == 404, res_unknown_levers.text
    assert res_unknown_levers.json()["detail"]["code"] == "flowsheet_node_not_found"

    # 4. 404 for unknown workspace_id
    res_unknown_ws = isolated_app.get(f"/workspaces/non-existent-ws/flowsheet/nodes/parameter:{ids['parameter_p1']}/impact")
    assert res_unknown_ws.status_code == 404, res_unknown_ws.text
    assert res_unknown_ws.json()["detail"]["code"] == "flowsheet_workspace_not_found"

    # 5. 400 for malformed node_ref
    res_malformed = isolated_app.get("/workspaces/main/flowsheet/nodes/not-valid-ref/impact")
    assert res_malformed.status_code == 400, res_malformed.text
    assert res_malformed.json()["detail"]["code"] == "flowsheet_ref_invalid"

    res_malformed_levers = isolated_app.get("/workspaces/main/flowsheet/nodes/invalidkind:123/levers")
    assert res_malformed_levers.status_code == 400, res_malformed_levers.text
    assert res_malformed_levers.json()["detail"]["code"] == "flowsheet_ref_invalid"


def test_dispatch_knowledge_read_tool_views(isolated_app: TestClient) -> None:
    ids = _seed_test_graph()
    grant = _make_hermes_grant("main")

    # 1. Valid change_impact view
    call_impact = _make_hermes_call({"view": "change_impact", "node_ref": f"parameter:{ids['parameter_p1']}"}, "main")
    result, err = dispatch_knowledge_read_tool(call_impact, "main", "ok", grant=grant)
    assert err == "ok"
    assert result is not None
    assert result["source_ref"] == f"parameter:{ids['parameter_p1']}"
    assert result["affected_count"] > 0
    assert "affected_nodes" in result

    # 2. Valid upstream_levers view
    call_levers = _make_hermes_call({"view": "upstream_levers", "node_ref": f"decision:{ids['decision']}"}, "main")
    result_levers, err = dispatch_knowledge_read_tool(call_levers, "main", "ok", grant=grant)
    assert err == "ok"
    assert result_levers is not None
    assert result_levers["target_ref"] == f"decision:{ids['decision']}"
    assert result_levers["lever_count"] > 0
    assert "levers" in result_levers

    # 3. Missing node_ref
    call_missing_ref = _make_hermes_call({"view": "change_impact"}, "main")
    res_missing, err_missing = dispatch_knowledge_read_tool(call_missing_ref, "main", "ok", grant=grant)
    assert res_missing is None
    assert err_missing == "invalid_arguments"

    # 4. Malformed node_ref
    call_bad_ref = _make_hermes_call({"view": "change_impact", "node_ref": "invalid_ref_without_colon"}, "main")
    res_bad, err_bad = dispatch_knowledge_read_tool(call_bad_ref, "main", "ok", grant=grant)
    assert res_bad is None
    assert err_bad == "invalid_arguments"

    # 5. Unknown node_ref
    call_unknown = _make_hermes_call({"view": "change_impact", "node_ref": "parameter:param-missing-404"}, "main")
    res_unknown, err_unknown = dispatch_knowledge_read_tool(call_unknown, "main", "ok", grant=grant)
    assert res_unknown is None
    assert err_unknown == "node_not_found"

    call_unknown_lever = _make_hermes_call({"view": "upstream_levers", "node_ref": "decision:dec-missing-404"}, "main")
    res_unknown_l, err_unknown_l = dispatch_knowledge_read_tool(call_unknown_lever, "main", "ok", grant=grant)
    assert res_unknown_l is None
    assert err_unknown_l == "node_not_found"

    # 6. Cross-workspace denial
    call_cross = _make_hermes_call(
        {"view": "change_impact", "workspace_id": "other-workspace", "node_ref": f"parameter:{ids['parameter_p1']}"},
        "main",
    )
    res_cross, err_cross = dispatch_knowledge_read_tool(call_cross, "main", "ok", grant=grant)
    assert res_cross is None
    assert err_cross == "cross_workspace_denied"


def test_broker_mcp_declarations_and_allowed_views() -> None:
    assert "change_impact" in _ALLOWED_VIEWS
    assert "upstream_levers" in _ALLOWED_VIEWS
    assert _VIEW_ALLOWED_KEYS["change_impact"] == {"view", "workspace_id", "node_ref"}
    assert _VIEW_ALLOWED_KEYS["upstream_levers"] == {"view", "workspace_id", "node_ref"}

    mcp_tool = broker_mcp._KNOWLEDGE_READ_TOOL
    schema = mcp_tool["inputSchema"]
    view_enum = schema["properties"]["view"]["enum"]
    assert "change_impact" in view_enum
    assert "upstream_levers" in view_enum
    assert "node_ref" in schema["properties"]
    assert schema["properties"]["node_ref"]["type"] == "string"
    assert schema["properties"]["node_ref"]["maxLength"] == 160


def test_direct_connection_functions(isolated_app: TestClient) -> None:
    ids = _seed_test_graph()
    with open_sqlite_connection() as conn:
        impact = preview_change_impact_from_connection(conn, "main", f"parameter:{ids['parameter_p1']}")
        assert impact.source_ref == f"parameter:{ids['parameter_p1']}"
        assert impact.affected_count > 0

        levers = find_upstream_levers_from_connection(conn, "main", f"decision:{ids['decision']}")
        assert levers.target_ref == f"decision:{ids['decision']}"
        assert levers.lever_count > 0

        with pytest.raises(FlowsheetError) as exc_info:
            preview_change_impact_from_connection(conn, "main", "parameter:unknown-999")
        assert exc_info.value.code == "flowsheet_node_not_found"

        with pytest.raises(FlowsheetError) as exc_info:
            find_upstream_levers_from_connection(conn, "main", "invalid-syntax")
        assert exc_info.value.code == "flowsheet_ref_invalid"

