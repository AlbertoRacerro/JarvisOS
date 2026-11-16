"""Offline and native engine tests for spec 283 (bench agent tools)."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import app.modules.engineering.bench as bench_module
from app.core.database import initialize_database
from app.core.paths import build_paths
from app.modules.agents.hermes.broker_mcp import _TOOLS
from app.modules.agents.hermes.supervisor import (
    _TOOL_CAPABILITIES,
    HERMES_TOOL_ALLOWLIST,
    UPSTREAM_REVISION,
    dispatch_tool,
)
from app.modules.agents.hermes.worker_shim import ADMITTED_TOOLS
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.thread_service import _install_surface_grants, _turn_tools
from app.modules.bluecad.models import BuildResult
from app.modules.workspace_actions.models import SurfaceBrief, SurfaceRef
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace

FIXTURES_DIR = Path(__file__).parent / "bluecad" / "fixtures"


@pytest.fixture
def test_workspace() -> str:
    initialize_database()
    ws = create_workspace(WorkspaceCreate(name=f"Bench Test {uuid4().hex[:6]}", slug=f"bench-test-{uuid4().hex[:8]}"))
    return ws.id


@pytest.fixture
def agent_session(test_workspace: str) -> AgentSessionRef:
    return AgentSessionRef(
        jarvis_thread_id=f"thread-{uuid4().hex[:8]}",
        hermes_session_id=f"hermes-{uuid4().hex[:8]}",
        profile_id="default",
        workspace_id=test_workspace,
        generation=1,
        upstream_revision=UPSTREAM_REVISION,
    )


def _mock_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_build_geometry_spec_file(path: Any, out_d: Any, *, timeout_s: float = 30.0) -> BuildResult:
        out = Path(out_d)
        out.mkdir(parents=True, exist_ok=True)
        manifest = {
            "assembly": {
                "total_volume_mm3": 12566.37,
                "bbox_mm": {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 50.0]},
                "kernel_checks": {"tube": {"brep_valid": True, "manifold": True}},
            }
        }
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (out / "validation_report.json").write_text(
            json.dumps(
                {
                    "checks": [
                        {"id": "T1_BREP_VALID", "status": "pass"},
                        {"id": "T1_WATERTIGHT", "status": "pass"},
                        {"id": "T1_VOLUME_DECL", "status": "pass", "detail": {"rel_error": 0.0001, "rel_tol": 0.01}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (out / "model.step").write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
        return BuildResult(
            spec_id="sha256:" + "1" * 64,
            out_dir=out,
            manifest_path=out / "manifest.json",
            report_path=out / "validation_report.json",
            manifest=manifest,
            report={
                "checks": [
                    {"id": "T1_BREP_VALID", "status": "pass"},
                    {"id": "T1_WATERTIGHT", "status": "pass"},
                    {"id": "T1_VOLUME_DECL", "status": "pass", "detail": {"rel_error": 0.0001, "rel_tol": 0.01}},
                ]
            },
            verdict="pass",
            errors=[],
        )

    def mock_mesh_analysis_spec(analysis_spec: Any, out_d: Any, **kwargs: Any) -> dict[str, Any]:
        out = Path(out_d)
        out.mkdir(parents=True, exist_ok=True)
        inp = out / "mesh.inp"
        inp.write_text("*NODE\n1, 0, 0, 0\n*ELEMENT\n1, 1\n", encoding="utf-8")
        inp_hash = hashlib.sha256(inp.read_bytes()).hexdigest()
        log_file = out / "gmsh.log"
        log_file.write_text("Info: worst = 0.45 / average = 0.72\n", encoding="utf-8")
        return {
            "schema_version": "bluecad_mesh_result_v0_1",
            "verdict": "pass",
            "attempts": [
                {
                    "counts": {"elements_total": 500, "nodes_total": 120},
                    "artifacts": {"gmsh_log": {"path": str(log_file), "sha256": "sha256:" + "2" * 64}},
                }
            ],
            "artifacts": {
                "mesh_inp": {"path": str(inp), "sha256": f"sha256:{inp_hash}"},
                "gmsh_log": {"path": str(log_file), "sha256": "sha256:" + "2" * 64},
            },
        }

    def mock_solve_static_analysis(spec: Any, mesh_res: Any, out_d: Any, **kwargs: Any) -> dict[str, Any]:
        out = Path(out_d)
        out.mkdir(parents=True, exist_ok=True)
        return {
            "verdict": "pass",
            "max_displacement": {"value": 0.025, "unit": "mm"},
            "max_von_mises": {"value": 45.2, "unit": "MPa"},
            "reactions": [{"force": [0.0, 0.0, 100.0]}],
            "reaction_resultant": [0.0, 0.0, 100.0],
            "artifacts": {},
        }

    monkeypatch.setattr(bench_module, "build_geometry_spec_file", mock_build_geometry_spec_file)
    monkeypatch.setattr(bench_module, "mesh_analysis_spec", mock_mesh_analysis_spec)
    monkeypatch.setattr(bench_module, "solve_static_analysis", mock_solve_static_analysis)


def test_criterion_2_bench_read_list_grants_and_refusals(
    agent_session: AgentSessionRef,
) -> None:
    """Criterion 2: jarvis_bench_read list through dispatch_tool with live grant returns nine rows;

    refused without grant, expired grant, or with jarvis.bluecad_read grant.
    """
    now = datetime.now(UTC)
    read_grant = CapabilityGrantRef(
        grant_id="grant-read-list",
        capability_id="jarvis.bench_read",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=agent_session.workspace_id, jarvis_thread_id=agent_session.jarvis_thread_id),
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    call = StructuredToolCall(
        call_id="call-1",
        capability_id="jarvis.bench_read",
        grant_id=read_grant.grant_id,
        correlation_id="corr-1",
        session_ref=agent_session,
        arguments={"op": "list"},
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )

    # 1. Live grant succeeds and returns 9 rows
    res = dispatch_tool(call, live_grants={read_grant.grant_id: read_grant})
    assert res.status == "succeeded"
    assert res.result is not None
    assert len(res.result["rows"]) == 9
    evaluator_ids = {r["evaluator_id"] for r in res.result["rows"]}
    assert "bluecad.solid" in evaluator_ids
    assert "bluecad.mesh" in evaluator_ids
    assert "bluecad.fem" in evaluator_ids
    assert "fluids.pipe_pressure_drop" in evaluator_ids

    # 2. Without grant: refused
    res_no_grant = dispatch_tool(call, live_grants={})
    assert res_no_grant.status == "refused"

    # 3. Expired grant: refused
    expired_grant = read_grant.model_copy(update={"expires_at": now - timedelta(seconds=1)})
    res_expired = dispatch_tool(call, live_grants={read_grant.grant_id: expired_grant})
    assert res_expired.status == "refused"

    # 4. With jarvis.bluecad_read grant: refused
    bluecad_grant = read_grant.model_copy(update={"capability_id": "jarvis.bluecad_read"})
    res_wrong_cap = dispatch_tool(call, live_grants={read_grant.grant_id: bluecad_grant})
    assert res_wrong_cap.status == "refused"

    # 5. With other workspace grant: refused
    other_ws_grant = read_grant.model_copy(
        update={"scope": CapabilityScope(workspace_id="other-workspace", jarvis_thread_id=agent_session.jarvis_thread_id)}
    )
    res_other_ws = dispatch_tool(call, live_grants={read_grant.grant_id: other_ws_grant})
    assert res_other_ws.status == "refused"


def test_criterion_3_bench_run_solid_mesh_fem_chain(
    agent_session: AgentSessionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 3: jarvis_bench_run runs solid -> mesh with geometry_run -> FEM with from_run,

    writing three run folders under workspace bench whose result.json verdicts equal tool verdicts.
    """
    _mock_adapters(monkeypatch)

    now = datetime.now(UTC)
    run_grant = CapabilityGrantRef(
        grant_id="grant-bench-run",
        capability_id="jarvis.bench_run",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=agent_session.workspace_id, jarvis_thread_id=agent_session.jarvis_thread_id),
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    tube_fixture = FIXTURES_DIR / "minimal_single_tube.json"
    assert tube_fixture.is_file()
    solid_req = json.loads(tube_fixture.read_text(encoding="utf-8"))

    # 1. Run solid
    call_solid = StructuredToolCall(
        call_id="call-solid",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-solid",
        session_ref=agent_session,
        arguments={"evaluator_id": "bluecad.solid", "request": solid_req},
        requested_at=now,
        deadline_at=now + timedelta(minutes=2),
    )
    res_solid = dispatch_tool(call_solid, live_grants={run_grant.grant_id: run_grant})
    assert res_solid.status == "succeeded"
    assert res_solid.result is not None
    solid_run_id = res_solid.result["run_id"]
    solid_verdict = res_solid.result["verdict"]
    assert solid_verdict == "usable"

    bench_root = (build_paths().workspaces_dir / agent_session.workspace_id / "engineering" / "bench").resolve()
    solid_folder = bench_root / solid_run_id
    assert solid_folder.is_dir()
    solid_res_file = solid_folder / "result.json"
    assert solid_res_file.is_file()
    saved_solid_res = json.loads(solid_res_file.read_text(encoding="utf-8"))
    assert saved_solid_res["verdict"] == solid_verdict
    assert saved_solid_res["status"] == res_solid.result["status"]

    # 2. Run mesh with geometry_run pointing to solid_run_id
    mesh_req = {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "tube-mesh-tool",
        "mesh": {"target_size": 10.0},
    }
    call_mesh = StructuredToolCall(
        call_id="call-mesh",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-mesh",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.mesh",
            "request": mesh_req,
            "geometry_run": solid_run_id,
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=2),
    )
    res_mesh = dispatch_tool(call_mesh, live_grants={run_grant.grant_id: run_grant})
    assert res_mesh.status == "succeeded"
    assert res_mesh.result is not None
    mesh_run_id = res_mesh.result["run_id"]
    mesh_verdict = res_mesh.result["verdict"]
    assert mesh_verdict == "usable"

    mesh_folder = bench_root / mesh_run_id
    assert mesh_folder.is_dir()
    mesh_res_file = mesh_folder / "result.json"
    assert mesh_res_file.is_file()
    saved_mesh_res = json.loads(mesh_res_file.read_text(encoding="utf-8"))
    assert saved_mesh_res["verdict"] == mesh_verdict
    assert saved_mesh_res["status"] == res_mesh.result["status"]
    assert (mesh_folder / "mesh_result.json").is_file()

    # 3. Run FEM with from_run pointing to mesh_run_id
    fem_req = {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "tube-fem-tool",
        "bcs": [{"port_label": "p1", "kind": "fixed"}],
        "loads": [{"type": "force_total", "force": [0.0, 0.0, -100.0]}],
    }
    call_fem = StructuredToolCall(
        call_id="call-fem",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-fem",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.fem",
            "request": fem_req,
            "from_run": mesh_run_id,
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=2),
    )
    res_fem = dispatch_tool(call_fem, live_grants={run_grant.grant_id: run_grant})
    assert res_fem.status == "succeeded"
    assert res_fem.result is not None
    fem_run_id = res_fem.result["run_id"]
    fem_verdict = res_fem.result["verdict"]
    assert fem_verdict == "usable"

    fem_folder = bench_root / fem_run_id
    assert fem_folder.is_dir()
    fem_res_file = fem_folder / "result.json"
    assert fem_res_file.is_file()
    saved_fem_res = json.loads(fem_res_file.read_text(encoding="utf-8"))
    assert saved_fem_res["verdict"] == fem_verdict
    assert saved_fem_res["status"] == res_fem.result["status"]
    assert (fem_folder / "fem_result.json").is_file()


def test_criterion_4_refusals_before_engine_runs_and_busy(
    agent_session: AgentSessionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 4: A request naming a path, a run id from another workspace or of the wrong evaluator,

    and a timeout above 100 s are refused before any engine runs; a second run while one is active is refused busy.
    """
    engine_called = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        engine_called.append(True)
        raise RuntimeError("engine should not have run!")

    monkeypatch.setattr(bench_module, "build_geometry_spec_file", boom)
    monkeypatch.setattr(bench_module, "mesh_analysis_spec", boom)
    monkeypatch.setattr(bench_module, "solve_static_analysis", boom)

    now = datetime.now(UTC)
    run_grant = CapabilityGrantRef(
        grant_id="grant-bench-run",
        capability_id="jarvis.bench_run",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=agent_session.workspace_id, jarvis_thread_id=agent_session.jarvis_thread_id),
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    # 1. Request naming a path (any *_path key in request)
    call_path = StructuredToolCall(
        call_id="call-path",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-path",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.solid",
            "request": {"geometry": {"step_path": "/tmp/custom.step"}},
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_path = dispatch_tool(call_path, live_grants={run_grant.grant_id: run_grant})
    assert res_path.status == "refused"
    assert res_path.error_code == "path_in_request_forbidden"
    assert not engine_called

    # 2. Run id from another workspace
    ws_other = create_workspace(WorkspaceCreate(name="Other WS", slug=f"other-ws-{uuid4().hex[:6]}"))
    other_bench_root = (build_paths().workspaces_dir / ws_other.id / "engineering" / "bench").resolve()
    other_run = other_bench_root / "run_other_123"
    other_run.mkdir(parents=True, exist_ok=True)
    (other_run / "result.json").write_text(json.dumps({"evaluator_id": "bluecad.solid", "verdict": "usable"}), encoding="utf-8")

    call_other_ws = StructuredToolCall(
        call_id="call-other-ws",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-other-ws",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.mesh",
            "request": {"mesh": {"target_size": 10.0}},
            "geometry_run": "run_other_123",  # run from ws_other, not in agent_session workspace
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_other_ws = dispatch_tool(call_other_ws, live_grants={run_grant.grant_id: run_grant})
    assert res_other_ws.status == "refused"
    assert res_other_ws.error_code == "run_not_found"
    assert not engine_called

    # 3. Run id of wrong evaluator
    # Create a mesh run in this workspace, but pass it as geometry_run (which requires bluecad.solid)
    my_bench_root = (build_paths().workspaces_dir / agent_session.workspace_id / "engineering" / "bench").resolve()
    my_mesh_run = my_bench_root / "run_my_mesh"
    my_mesh_run.mkdir(parents=True, exist_ok=True)
    (my_mesh_run / "result.json").write_text(
        json.dumps({"evaluator_id": "bluecad.mesh", "verdict": "usable"}), encoding="utf-8"
    )

    call_wrong_eval = StructuredToolCall(
        call_id="call-wrong-eval",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-wrong-eval",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.mesh",
            "request": {"mesh": {"target_size": 10.0}},
            "geometry_run": "run_my_mesh",  # mesh run, not solid run
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_wrong_eval = dispatch_tool(call_wrong_eval, live_grants={run_grant.grant_id: run_grant})
    assert res_wrong_eval.status == "refused"
    assert res_wrong_eval.error_code == "wrong_evaluator"
    assert not engine_called

    # 4. Timeout above 100 s
    call_timeout = StructuredToolCall(
        call_id="call-timeout",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-timeout",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.solid",
            "request": {"geometry": "tube"},
            "timeout_s": 100.5,
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_timeout = dispatch_tool(call_timeout, live_grants={run_grant.grant_id: run_grant})
    assert res_timeout.status == "refused"
    assert res_timeout.error_code == "timeout_exceeded"
    assert not engine_called

    # 5. Second run while one is active is refused busy
    bench_module.acquire_bench_worker(agent_session.hermes_session_id)
    try:
        call_busy = StructuredToolCall(
            call_id="call-busy",
            capability_id="jarvis.bench_run",
            grant_id=run_grant.grant_id,
            correlation_id="corr-busy",
            session_ref=agent_session,
            arguments={
                "evaluator_id": "bluecad.solid",
                "request": {"geometry": "tube"},
                "timeout_s": 30.0,
            },
            requested_at=now,
            deadline_at=now + timedelta(minutes=1),
        )
        res_busy = dispatch_tool(call_busy, live_grants={run_grant.grant_id: run_grant})
        assert res_busy.status == "refused"
        assert res_busy.error_code == "busy"
        assert not engine_called
    finally:
        bench_module.release_bench_worker(agent_session.hermes_session_id)


def test_criterion_5_show_and_compare_return_282_views(
    agent_session: AgentSessionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 5: show and compare return the 282 views of stored runs."""
    _mock_adapters(monkeypatch)

    now = datetime.now(UTC)
    read_grant = CapabilityGrantRef(
        grant_id="grant-bench-read",
        capability_id="jarvis.bench_read",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=agent_session.workspace_id, jarvis_thread_id=agent_session.jarvis_thread_id),
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    run_grant = CapabilityGrantRef(
        grant_id="grant-bench-run",
        capability_id="jarvis.bench_run",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=agent_session.workspace_id, jarvis_thread_id=agent_session.jarvis_thread_id),
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    tube_fixture = FIXTURES_DIR / "minimal_single_tube.json"
    solid_req = json.loads(tube_fixture.read_text(encoding="utf-8"))

    # Run two runs so we can show and compare
    call_run1 = StructuredToolCall(
        call_id="call-run1",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-run1",
        session_ref=agent_session,
        arguments={"evaluator_id": "bluecad.solid", "request": solid_req},
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_run1 = dispatch_tool(call_run1, live_grants={run_grant.grant_id: run_grant})
    assert res_run1.status == "succeeded"
    assert res_run1.result is not None
    run_1_id = res_run1.result["run_id"]

    call_run2 = StructuredToolCall(
        call_id="call-run2",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="corr-run2",
        session_ref=agent_session,
        arguments={"evaluator_id": "bluecad.solid", "request": solid_req},
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_run2 = dispatch_tool(call_run2, live_grants={run_grant.grant_id: run_grant})
    assert res_run2.status == "succeeded"
    assert res_run2.result is not None
    run_2_id = res_run2.result["run_id"]

    # 1. Test show
    call_show = StructuredToolCall(
        call_id="call-show",
        capability_id="jarvis.bench_read",
        grant_id=read_grant.grant_id,
        correlation_id="corr-show",
        session_ref=agent_session,
        arguments={"op": "show", "run_id": run_1_id},
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_show = dispatch_tool(call_show, live_grants={read_grant.grant_id: read_grant})
    assert res_show.status == "succeeded"
    assert res_show.result is not None
    assert res_show.result["view"]["evaluator_id"] == "bluecad.solid"
    assert res_show.result["view"]["verdict"] == "usable"
    assert res_show.result["view"]["status"] == "succeeded"
    assert "failed_checks" in res_show.result["view"]
    assert "outputs" in res_show.result["view"]

    # 2. Test compare
    call_compare = StructuredToolCall(
        call_id="call-compare",
        capability_id="jarvis.bench_read",
        grant_id=read_grant.grant_id,
        correlation_id="corr-compare",
        session_ref=agent_session,
        arguments={"op": "compare", "run_id_a": run_1_id, "run_id_b": run_2_id},
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    res_compare = dispatch_tool(call_compare, live_grants={read_grant.grant_id: read_grant})
    assert res_compare.status == "succeeded"
    assert res_compare.result is not None
    assert res_compare.result["view"]["evaluator_a"] == "bluecad.solid"
    assert res_compare.result["view"]["evaluator_b"] == "bluecad.solid"
    assert res_compare.result["view"]["verdict_a"] == "usable"
    assert res_compare.result["view"]["verdict_b"] == "usable"
    assert "outputs" in res_compare.result["view"]
    assert "checks" in res_compare.result["view"]


def test_criterion_6_allowlists_and_surface_briefs(test_workspace: str) -> None:
    """Criterion 6: The four allowlists (_TOOLS, _TOOL_CAPABILITIES, ADMITTED_TOOLS, HERMES_TOOL_ALLOWLIST)

    list the new tools, and the bluecad surface brief names both grant ids; process and studio do not.
    """
    # 1. Allowlist 1: _TOOLS in broker_mcp
    broker_tool_names = {t["name"] for t in _TOOLS}
    assert "jarvis_bench_read" in broker_tool_names
    assert "jarvis_bench_run" in broker_tool_names

    # 2. Allowlist 2: _TOOL_CAPABILITIES in supervisor
    assert _TOOL_CAPABILITIES.get("jarvis_bench_read") == "jarvis.bench_read"
    assert _TOOL_CAPABILITIES.get("jarvis_bench_run") == "jarvis.bench_run"

    # 3. Allowlist 3: ADMITTED_TOOLS in worker_shim
    assert "mcp__jarvis__jarvis_bench_read" in ADMITTED_TOOLS
    assert "mcp__jarvis__jarvis_bench_run" in ADMITTED_TOOLS

    # 4. Allowlist 4: HERMES_TOOL_ALLOWLIST in supervisor
    assert "mcp__jarvis__jarvis_bench_read" in HERMES_TOOL_ALLOWLIST
    assert "mcp__jarvis__jarvis_bench_run" in HERMES_TOOL_ALLOWLIST

    # 5. Surface brief on bluecad names both grant ids
    worker_bluecad = SimpleNamespace(live_grants={})
    brief_bluecad = SurfaceBrief(
        surface="bluecad",
        route_id="design-bluecad",
        workspace_id=test_workspace,
        base_revision="candidate-1",
        candidate_id="candidate-1",
        summary="BLUECAD · tube",
        selected=[{"part_id": "template_tube", "kind": "tube_run"}],
        text="candidate candidate-1 selected tube",
        digest="sha256:" + "b" * 64,
    )
    text_bluecad = _install_surface_grants(
        worker_bluecad,
        test_workspace,
        "thread-1",
        SurfaceRef(route_id="design-bluecad", candidate_id="candidate-1"),
        brief_bluecad,
    )
    assert "mcp__jarvis__jarvis_bench_read" in text_bluecad
    assert "mcp__jarvis__jarvis_bench_run" in text_bluecad
    grant_caps_bluecad = {g.capability_id for g in worker_bluecad.live_grants.values()}
    assert "jarvis.bench_read" in grant_caps_bluecad
    assert "jarvis.bench_run" in grant_caps_bluecad
    assert "jarvis.bluecad_read" in grant_caps_bluecad
    assert "jarvis.bluecad_act" in grant_caps_bluecad

    # Turn tools for bluecad includes bench tools
    bluecad_tools = _turn_tools("bluecad")
    assert "mcp__jarvis__jarvis_bench_read" in bluecad_tools
    assert "mcp__jarvis__jarvis_bench_run" in bluecad_tools

    # 6. Surface brief on process does NOT name bench tools or issue bench grants
    worker_process = SimpleNamespace(live_grants={})
    brief_process = SurfaceBrief(
        surface="process",
        route_id="design-process",
        workspace_id=test_workspace,
        base_revision="rev-1",
        draft_id="draft-1",
        summary="Process · stream",
        selected=[{"kind": "stream", "tag": "S1"}],
        text="draft draft-1",
        digest="sha256:" + "p" * 64,
    )
    text_process = _install_surface_grants(
        worker_process,
        test_workspace,
        "thread-1",
        SurfaceRef(route_id="design-process", draft_id="draft-1"),
        brief_process,
    )
    assert "jarvis_bench" not in text_process
    grant_caps_process = {g.capability_id for g in worker_process.live_grants.values()}
    assert not any("bench" in cap for cap in grant_caps_process)
    process_tools = _turn_tools("process")
    assert "mcp__jarvis__jarvis_bench_read" not in process_tools
    assert "mcp__jarvis__jarvis_bench_run" not in process_tools

    # 7. Surface brief on studio does NOT name bench tools or issue bench grants
    worker_studio = SimpleNamespace(live_grants={})
    brief_studio = SurfaceBrief(
        surface="studio",
        route_id="design-studio",
        workspace_id=test_workspace,
        base_revision="draft-1",
        draft_id="draft-1",
        summary="Studio · model",
        selected=[],
        text="studio draft draft-1",
        digest="sha256:" + "s" * 64,
    )
    text_studio = _install_surface_grants(
        worker_studio,
        test_workspace,
        "thread-1",
        SurfaceRef(route_id="design-studio", draft_id="draft-1"),
        brief_studio,
    )
    assert "jarvis_bench" not in text_studio
    grant_caps_studio = {g.capability_id for g in worker_studio.live_grants.values()}
    assert not any("bench" in cap for cap in grant_caps_studio)
    studio_tools = _turn_tools("studio")
    assert "mcp__jarvis__jarvis_bench_read" not in studio_tools
    assert "mcp__jarvis__jarvis_bench_run" not in studio_tools


def _make_working_tools_registry(tmp_path: Path) -> Path:
    import yaml

    reg_path = tmp_path / "tools.yaml"
    gmsh_bin = shutil.which("gmsh") or ""
    ccx_bin = shutil.which("ccx") or ""

    gmsh_hash = hashlib.sha256(Path(gmsh_bin).read_bytes()).hexdigest() if gmsh_bin and Path(gmsh_bin).is_file() else "0" * 64
    ccx_hash = hashlib.sha256(Path(ccx_bin).read_bytes()).hexdigest() if ccx_bin and Path(ccx_bin).is_file() else "0" * 64

    tools = [
        {
            "id": "build123d",
            "kind": "cad_kernel",
            "integration_mode": "in_process",
            "version_pin": "0.11.1",
            "license": {"spdx": "Apache-2.0", "boundary": "A", "verified_date": "2026-07-03"},
            "enabled": True,
            "entrypoint": "build123d",
            "capabilities": ["brep", "sweep", "boolean", "fillet", "step_export", "stl_export"],
            "health_check": None,
        },
        {
            "id": "gmsh",
            "kind": "mesher",
            "integration_mode": "subprocess",
            "version_pin": "4.15.2",
            "license": {"spdx": "GPL-2.0-or-later", "boundary": "C", "verified_date": "2026-10-24"},
            "enabled": bool(gmsh_bin),
            "entrypoint": gmsh_bin,
            "binary_sha256": gmsh_hash,
            "provenance_url": "https://example.com/gmsh",
            "capabilities": ["step_import", "tet_mesh", "physical_groups", "inp_export"],
            "health_check": None,
        },
        {
            "id": "calculix",
            "kind": "fem_solver",
            "integration_mode": "subprocess",
            "version_pin": "2.22",
            "license": {"spdx": "GPL-2.0", "boundary": "C", "verified_date": "2026-07-03"},
            "enabled": bool(ccx_bin),
            "entrypoint": ccx_bin,
            "binary_sha256": ccx_hash,
            "provenance_url": "https://example.com/calculix",
            "capabilities": ["static", "modal", "thermal"],
            "health_check": None,
        },
    ]
    reg_path.write_text(yaml.safe_dump({"registry_version": "bluecad_tool_registry_v0_1", "tools": tools}), encoding="utf-8")
    return reg_path


def test_criterion_7_native_engine_proof(
    agent_session: AgentSessionRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 7: Real solid -> mesh -> FEM chain through dispatch_tool with installed tools."""
    pytest.importorskip("build123d", reason="build123d is not installed; solid engine path not proved")
    if not shutil.which("gmsh") or not shutil.which("ccx"):
        pytest.skip("gmsh or ccx is not installed; mesh and FEM engine paths not proved")

    working_reg = _make_working_tools_registry(tmp_path)
    monkeypatch.setenv("JARVISOS_BLUECAD_TOOL_REGISTRY", str(working_reg))

    tube_fixture = FIXTURES_DIR / "minimal_single_tube.json"
    assert tube_fixture.is_file(), "minimal_single_tube.json fixture required"
    solid_req = json.loads(tube_fixture.read_text(encoding="utf-8"))

    now = datetime.now(UTC)
    run_grant = CapabilityGrantRef(
        grant_id="grant-real-run",
        capability_id="jarvis.bench_run",
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=agent_session.workspace_id, jarvis_thread_id=agent_session.jarvis_thread_id),
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    # 1. Real solid run
    call_solid = StructuredToolCall(
        call_id="real-solid",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="real-solid",
        session_ref=agent_session,
        arguments={"evaluator_id": "bluecad.solid", "request": solid_req, "timeout_s": 60.0},
        requested_at=now,
        deadline_at=now + timedelta(minutes=2),
    )
    res_solid = dispatch_tool(call_solid, live_grants={run_grant.grant_id: run_grant})
    assert res_solid.status == "succeeded"
    assert res_solid.result is not None
    assert res_solid.result["status"] == "succeeded"
    assert res_solid.result["verdict"] == "usable"
    solid_run_id = res_solid.result["run_id"]

    # 2. Real mesh run
    mesh_spec = {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "tube-real-mesh",
        "analysis_type": "static",
        "material": {"name": "steel", "E": 200000.0, "nu": 0.3, "rho": 7.8e-9, "yield_strength": 250.0},
        "bcs": [{"port_label": "tube1.port_a", "kind": "fixed"}],
        "loads": [{"port_label": "tube1.port_b", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
        "mesh": {"target_size": 50.0},
        "pass_criteria": [{"metric": "max_displacement", "op": "<=", "value": 100.0}],
    }
    call_mesh = StructuredToolCall(
        call_id="real-mesh",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="real-mesh",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.mesh",
            "request": mesh_spec,
            "geometry_run": solid_run_id,
            "timeout_s": 60.0,
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=2),
    )
    res_mesh = dispatch_tool(call_mesh, live_grants={run_grant.grant_id: run_grant})
    assert res_mesh.status == "succeeded"
    assert res_mesh.result is not None
    assert res_mesh.result["status"] == "succeeded"
    assert res_mesh.result["verdict"] == "usable"
    mesh_run_id = res_mesh.result["run_id"]

    # 3. Real FEM run
    call_fem = StructuredToolCall(
        call_id="real-fem",
        capability_id="jarvis.bench_run",
        grant_id=run_grant.grant_id,
        correlation_id="real-fem",
        session_ref=agent_session,
        arguments={
            "evaluator_id": "bluecad.fem",
            "request": mesh_spec,
            "from_run": mesh_run_id,
            "timeout_s": 60.0,
        },
        requested_at=now,
        deadline_at=now + timedelta(minutes=2),
    )
    res_fem = dispatch_tool(call_fem, live_grants={run_grant.grant_id: run_grant})
    assert res_fem.status == "succeeded"
    assert res_fem.result is not None
    assert res_fem.result["status"] == "succeeded"
    assert res_fem.result["verdict"] == "usable"


def test_bench_run_stays_outside_common_registry_and_view_hides_paths() -> None:
    """EXECUTE keeps its own grant (111): the common registry still refuses it; the agent view names no data-root path."""
    from app.modules.agents.hermes import supervisor
    from app.modules.ai.jarvis_context import PRODUCTION_CAPABILITY_REGISTRY, JarvisContextError
    from app.modules.ai.jarvis_context_models import JarvisCapabilityDescriptor
    from app.modules.engineering.bench import _agent_view

    ids = {item.capability_id for item in PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads")}
    assert "jarvis.bench_read" in ids
    assert "jarvis.bench_run" not in ids
    assert supervisor._HERMES_BENCH_RUN_CAPABILITY.action_class == "EXECUTE"
    with pytest.raises(JarvisContextError):
        PRODUCTION_CAPABILITY_REGISTRY.register(JarvisCapabilityDescriptor(
            capability_id="jarvis.other_execute", route_id="ai-threads", action_class="EXECUTE", label="x"))
    assert _agent_view({"result_path": "/data/ws/engineering/bench/run_x/result.json"})["result_path"] == "result.json"


def _run_call(agent_session: AgentSessionRef, arguments: dict[str, Any]) -> Any:
    now = datetime.now(UTC)
    grant = CapabilityGrantRef(
        grant_id="grant-review", capability_id="jarvis.bench_run", issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=agent_session.workspace_id, jarvis_thread_id=agent_session.jarvis_thread_id),
        issued_at=now, expires_at=now + timedelta(minutes=10),
    )
    call = StructuredToolCall(
        call_id="call-review", capability_id="jarvis.bench_run", grant_id=grant.grant_id,
        correlation_id="corr-review", session_ref=agent_session, arguments=arguments,
        requested_at=now, deadline_at=now + timedelta(minutes=2),
    )
    return dispatch_tool(call, live_grants={grant.grant_id: grant})


def test_agent_cannot_run_evaluators_that_write_outside_the_run(agent_session: AgentSessionRef) -> None:
    for evaluator_id in ("openfoam_channel_v1", "dwsim.flowsheet", "photobioreactor.day_night"):
        res = _run_call(agent_session, {"evaluator_id": evaluator_id, "request": {"request_ref": {}}})
        assert res.status == "refused"
        assert res.error_code == "evaluator_not_agent_runnable"


def test_registry_request_is_pinned_to_grant_workspace_and_tool_deadline(agent_session: AgentSessionRef,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_engineering_bench import _valid_pipe_pressure_drop_request

    foreign = _valid_pipe_pressure_drop_request("other-workspace")
    res = _run_call(agent_session, {"evaluator_id": "fluids.pipe_pressure_drop", "request": foreign})
    assert res.status == "refused"
    assert not (build_paths().workspaces_dir / "other-workspace").exists()

    request = _valid_pipe_pressure_drop_request(agent_session.workspace_id)
    request["requested_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    request["deadline_at"] = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    import app.modules.engineering.bench as bench_module

    seen: list[dict] = []
    real_run = bench_module.run_evaluation

    def _capture(evaluator_id, request_data, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        seen.append(request_data)
        return real_run(evaluator_id, request_data, *args, **kwargs)

    monkeypatch.setattr(bench_module, "run_evaluation", _capture)
    res = _run_call(agent_session, {"evaluator_id": "fluids.pipe_pressure_drop", "request": request,
                                    "timeout_s": 5.0})
    assert res.status == "succeeded", res.error_code
    bench_root = (build_paths().workspaces_dir / agent_session.workspace_id / "engineering" / "bench").resolve()
    saved = json.loads((bench_root / res.result["run_id"] / "result.json").read_text(encoding="utf-8"))
    assert saved["request_ref"]["workspace_id"] == agent_session.workspace_id
    sent = seen[-1]
    window = datetime.fromisoformat(sent["deadline_at"]) - datetime.fromisoformat(sent["requested_at"])
    assert window == timedelta(seconds=5)
    assert datetime.now(UTC) - datetime.fromisoformat(sent["requested_at"]) < timedelta(minutes=1)


def test_non_mapping_geometry_is_refused_not_raised(agent_session: AgentSessionRef,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_adapters(monkeypatch)
    solid_req = json.loads((FIXTURES_DIR / "minimal_single_tube.json").read_text(encoding="utf-8"))
    solid = _run_call(agent_session, {"evaluator_id": "bluecad.solid", "request": solid_req})
    assert solid.status == "succeeded"
    res = _run_call(agent_session, {"evaluator_id": "bluecad.mesh", "geometry_run": solid.result["run_id"],
                                    "request": {"geometry": 123, "mesh": {"target_size": 10.0}}})
    assert res.status == "refused"
    assert res.error_code == "invalid_arguments"

    for falsy in ([], "", 0, False):
        res = _run_call(agent_session, {"evaluator_id": "bluecad.mesh", "geometry_run": solid.result["run_id"],
                                        "request": {"geometry": falsy, "mesh": {"target_size": 10.0}}})
        assert res.status == "refused"
        assert res.error_code == "invalid_arguments"


def test_nan_timeout_never_reaches_the_bench(agent_session: AgentSessionRef) -> None:
    from pydantic import ValidationError

    from tests.test_engineering_bench import _valid_pipe_pressure_drop_request

    request = _valid_pipe_pressure_drop_request(agent_session.workspace_id)
    with pytest.raises(ValidationError):
        _run_call(agent_session, {"evaluator_id": "fluids.pipe_pressure_drop", "request": request,
                                  "timeout_s": float("nan")})


def test_agent_view_strips_paths_inside_multiline_relative_and_windows_text() -> None:
    from app.modules.engineering.bench import _agent_view

    view = _agent_view({"failure": {"message": "File missing:\n/data/ws/a/run_1/mesh.inp\tsee ../run_2/x.inp"},
                        "checks": [{"hint": "C:\\Users\\someone\\model.step not found"}]})
    text = json.dumps(view)
    assert "/data/" not in text and "run_2" not in text and "Users" not in text
    assert view["failure"]["message"] == "File missing:\nmesh.inp\tsee x.inp"
    assert view["checks"][0]["hint"] == "model.step not found"


def test_agent_view_strips_absolute_paths_but_keeps_units() -> None:
    from app.modules.engineering.bench import _agent_view

    view = _agent_view({
        "failure": {"message": "[Errno 2] No such file: '/data/ws/a/engineering/bench/run_1/mesh.inp'"},
        "hint": "binary /usr/bin/gmsh missing", "unit": "W/(m**2*K)", "other": "kg/m3",
    })
    text = json.dumps(view)
    assert "/data/" not in text and "/usr/bin" not in text
    assert view["failure"]["message"].endswith("'mesh.inp'")
    assert view["unit"] == "W/(m**2*K)" and view["other"] == "kg/m3"
