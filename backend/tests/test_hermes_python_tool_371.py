"""Spec 371: Hermes ``jarvis_python`` runs short scripts only in the offline calculation sandbox."""
from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.agents.hermes import calc_run_tool
from app.modules.agents.hermes.broker_mcp import _TOOLS
from app.modules.agents.hermes.provenance import compute_number_provenance
from app.modules.agents.hermes.supervisor import (
    _TOOL_CAPABILITIES,
    HERMES_TOOL_ALLOWLIST,
    UPSTREAM_REVISION,
    HermesSupervisor,
    dispatch_tool,
    turn_deadline,
)
from app.modules.agents.hermes.worker_shim import ADMITTED_TOOLS, COMPACT_METHODOLOGY, pinned_config
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.thread_service import _install_surface_grants, _turn_tools
from app.modules.studio_runtime import runtime
from app.modules.workspace_actions.models import SurfaceBrief, SurfaceRef
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace

DESCRIPTION = ("Run Python 3 code (numpy, scipy, pint available) and return what it prints. Use it for every "
               "calculation, unit conversion and data fit. Always print() the results.")
SUFFIX = "\n\ndef main(inputs):\n    return None\n"


@pytest.fixture
def workspace_id() -> str:
    initialize_database()
    return create_workspace(WorkspaceCreate(name=f"Calc {uuid4().hex[:6]}", slug=f"calc-{uuid4().hex[:8]}")).id


def _session(workspace: str) -> AgentSessionRef:
    return AgentSessionRef(jarvis_thread_id=f"thread-{uuid4().hex[:8]}", hermes_session_id=f"hermes-{uuid4().hex[:8]}",
                           profile_id="default", workspace_id=workspace, generation=1,
                           upstream_revision=UPSTREAM_REVISION)


def _call(workspace: str, arguments: dict[str, Any], *, capability: str = "jarvis.calc_run",
          grant_capability: str = "jarvis.calc_run", with_grant: bool = True):
    session = _session(workspace)
    now = datetime.now(UTC)
    grant_id = str(uuid4())
    grants: dict[str, CapabilityGrantRef] = {}
    if with_grant:
        grants[grant_id] = CapabilityGrantRef(
            grant_id=grant_id, capability_id=grant_capability, issuer="jarvis_policy",
            scope=CapabilityScope(workspace_id=workspace, jarvis_thread_id=session.jarvis_thread_id),
            issued_at=now, expires_at=now + timedelta(minutes=10))
    call = StructuredToolCall(call_id=f"call-{uuid4().hex[:8]}", capability_id=capability, grant_id=grant_id,
                              correlation_id="c", session_ref=session, arguments=arguments,
                              requested_at=now, deadline_at=now + timedelta(seconds=30))
    return dispatch_tool(call, live_grants=grants), call


class _Double:
    def __init__(self, record: runtime.RunRecord) -> None:
        self.record = record
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> runtime.RunRecord:
        self.calls.append((args, kwargs))
        return self.record


def _record(classification: str = "ok", **fields: Any) -> runtime.RunRecord:
    limits = runtime.AppliedLimits(**{n: runtime.PROFILES["calculation"][n] for n in runtime.AppliedLimits.model_fields})
    return runtime.RunRecord(source_sha256="a" * 64, limits=limits, mechanism=runtime.MECHANISM,
                             duration_seconds=0.25, classification=classification, **fields)  # type: ignore[arg-type]


def _install(monkeypatch: pytest.MonkeyPatch, record: runtime.RunRecord) -> _Double:
    double = _Double(record)
    monkeypatch.setattr(runtime, "run", double)
    return double


def test_valid_call_uses_calculation_profile_and_returns_stdout(monkeypatch: pytest.MonkeyPatch,
                                                                workspace_id: str) -> None:
    double = _install(monkeypatch, _record(stdout="42.0\n"))
    result, _ = _call(workspace_id, {"code": "print(6 * 7.0)"})
    assert result.status == "succeeded" and result.result == {"text": "42.0\n"}
    assert len(double.calls) == 1
    args, kwargs = double.calls[0]
    request = args[0]
    assert kwargs == {"profile": "calculation"} and len(args) == 1
    assert request.source == "print(6 * 7.0)" + SUFFIX and request.source.endswith(SUFFIX)
    assert request.inputs == {}
    maximum = runtime.PROFILES["calculation"]
    assert request.limits.model_dump() == {name: maximum[name] for name in runtime.AppliedLimits.model_fields}
    assert (request.limits.wall_seconds, request.limits.cpu_seconds) == (15, 12)


def test_nothing_printed_error_and_limit_text(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    _install(monkeypatch, _record())
    assert _call(workspace_id, {"code": "x = 1"})[0].result == {"text": "(nothing printed: use print())"}

    stderr = "A" * 3000 + "ZeroDivisionError: division by zero"
    _install(monkeypatch, _record("error", stdout="partial\n", stderr=stderr))
    text = _call(workspace_id, {"code": "1/0"})[0].result["text"]
    assert text.startswith("partial\n\n[stderr]\n") or text.startswith("partial\n[stderr]\n")
    assert text.endswith("ZeroDivisionError: division by zero")
    assert len(text.split("[stderr]\n", 1)[1]) == 1500

    _install(monkeypatch, _record("timeout", limit="wall_seconds", limit_value=15, stdout="started\n"))
    text = _call(workspace_id, {"code": "while True: pass"})[0].result["text"]
    assert text == "started\n\nerror: wall_seconds limit reached (wall 15 s)" or \
        text == "started\nerror: wall_seconds limit reached (wall 15 s)"
    _install(monkeypatch, _record("limit", limit="cpu_seconds", limit_value=12))
    assert _call(workspace_id, {"code": "x"})[0].result == {"text": "error: cpu_seconds limit reached (cpu 12 s)"}


def test_output_is_capped_and_carries_no_host_path(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    _install(monkeypatch, _record(stdout="9" * 9000))
    assert len(_call(workspace_id, {"code": "print(1)"})[0].result["text"]) == 4000
    venv = str(Path(os.sys.prefix).resolve())
    leak = (f'File "{venv}/lib/python3/site-packages/x.py", line 1\n'
            'File "/home/example/private/a.py"\nFile "/usr/lib/example.py"\nBoom')
    _install(monkeypatch, _record("error", stderr=leak))
    text = _call(workspace_id, {"code": "boom"})[0].result["text"]
    assert venv not in text and "/home/example" not in text and "/usr/lib" not in text and "Boom" in text


@pytest.mark.parametrize(("printed", "expected"), [
    ("velocity = 2 /s", "velocity = 2 /s"),
    ("pressure 3 kg /m^3", "pressure 3 kg /m^3"),
    ("speed 2.5 m/s^2; density 1.2 kg/m^3", "speed 2.5 m/s^2; density 1.2 kg/m^3"),
    ("rate 4 /m/s; concentration 2 /mol/L", "rate 4 /m/s; concentration 2 /mol/L"),
    ('File "/tmp/private/result.py", line 1', 'File "<path>", line 1'),
    ("read /home/example/report.txt and /usr/lib/example.py", "read <path> and <path>"),
    ("scratch /tmp; file:///tmp/private/result.py", "scratch <path>; <path>"),
    ("scratch /custom and /secret/data", "scratch <path> and <path>"),
    ("file:///tmp/private", "<path>"),
    ("file://localhost/tmp/private/result.py", "<path>"),
    (r"C:\temp\private\result.py", "<path>"),
])
def test_scrub_preserves_units_and_removes_host_paths(printed: str, expected: str) -> None:
    assert calc_run_tool._scrub(printed) == expected


def test_scrub_applies_to_stdout_and_stderr_before_output_cap() -> None:
    record = _record("error", stdout="velocity = 2 /s\n" + "7" * 5000 + " /tmp/private/result.py",
                     stderr="pressure 3 kg /m^3\nfile:///tmp/private/error.txt")
    text = calc_run_tool._result_text(record)
    assert len(text) == calc_run_tool.MAX_TEXT_CHARS
    assert text.startswith("velocity = 2 /s\n")
    assert text.endswith("[stderr]\npressure 3 kg /m^3\n<path>")
    assert "/tmp/private" not in text and "file://" not in text


def test_output_cap_preserves_error_and_limit(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    _install(monkeypatch, _record("error", stdout="x" * 9000, stderr="final error"))
    text = _call(workspace_id, {"code": "print(1)"})[0].result["text"]
    assert len(text) == 4000 and text.endswith("[stderr]\nfinal error")
    _install(monkeypatch, _record("timeout", stdout="x" * 9000, limit="wall_seconds", limit_value=15))
    text = _call(workspace_id, {"code": "print(1)"})[0].result["text"]
    assert len(text) == 4000 and text.endswith("error: wall_seconds limit reached (wall 15 s)")


@pytest.mark.parametrize("classification", ["disabled", "unavailable"])
def test_disabled_or_unavailable_returns_calc_unavailable(monkeypatch: pytest.MonkeyPatch, workspace_id: str,
                                                          classification: str) -> None:
    _install(monkeypatch, _record(classification))
    result, _ = _call(workspace_id, {"code": "print(1)"})
    assert (result.status, result.error_code, result.result) == ("refused", "calc_unavailable", None)


def test_real_runtime_without_flag_runs_nothing(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "0")
    get_settings.cache_clear()
    launched: list[object] = []
    monkeypatch.setattr(runtime, "_launch", lambda *a, **k: launched.append(a) or (0, False))
    try:
        result, _ = _call(workspace_id, {"code": "print(1)"})
    finally:
        get_settings.cache_clear()
    assert result.error_code == "calc_unavailable" and launched == []


@pytest.mark.parametrize("arguments", [
    {}, {"code": 5}, {"code": None}, {"code": ["print(1)"]}, {"code": "x" * 16_385},
    {"code": "print(1)", "profile": "geometry"}, {"code": "print(1)", "artifact_dir": "/tmp/x"},
    {"code": "print('\ud800')"},
])
def test_invalid_arguments_run_nothing(monkeypatch: pytest.MonkeyPatch, workspace_id: str,
                                       arguments: dict[str, Any]) -> None:
    double = _install(monkeypatch, _record(stdout="no"))
    result, _ = _call(workspace_id, arguments)
    assert (result.status, result.error_code) == ("refused", "invalid_arguments")
    assert double.calls == []


def test_code_at_the_limit_is_accepted(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    double = _install(monkeypatch, _record(stdout="ok"))
    assert _call(workspace_id, {"code": "#" * 16_384})[0].status == "succeeded"
    assert len(double.calls) == 1


def test_call_without_a_valid_grant_is_refused(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    double = _install(monkeypatch, _record(stdout="no"))
    missing, _ = _call(workspace_id, {"code": "print(1)"}, with_grant=False)
    wrong, _ = _call(workspace_id, {"code": "print(1)"}, grant_capability="jarvis.bench_read")
    assert (missing.status, missing.error_code) == ("refused", "capability_denied")
    assert (wrong.status, wrong.error_code) == ("refused", "capability_denied")
    assert double.calls == []


def test_event_carries_only_classification_limit_duration_and_digest(monkeypatch: pytest.MonkeyPatch,
                                                                     workspace_id: str) -> None:
    _install(monkeypatch, _record("timeout", limit="wall_seconds", limit_value=15, stdout="secret-out"))
    _, call = _call(workspace_id, {"code": "print('secret-source')"})
    with open_sqlite_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT payload FROM events WHERE event_type = ? AND target_id = ?",
                                  (calc_run_tool.CALC_RUN_EVENT, call.call_id)).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload == {"classification": "timeout", "limit": "wall_seconds", "duration_seconds": 0.25,
                       "source_sha256": "a" * 64}
    assert "secret" not in rows[0]["payload"]


def test_supervisor_event_omits_code_and_printed_number_becomes_evidence(
    monkeypatch: pytest.MonkeyPatch, workspace_id: str,
) -> None:
    _install(monkeypatch, _record(stdout="pressure 12.5 kPa\n"))
    session = _session(workspace_id)
    now = datetime.now(UTC)
    grant_id = str(uuid4())
    grant = CapabilityGrantRef(
        grant_id=grant_id, capability_id="jarvis.calc_run", issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=session.jarvis_thread_id),
        issued_at=now, expires_at=now + timedelta(minutes=10),
    )
    worker = HermesSupervisor("unused")
    worker.session = session
    worker.live_grants[grant_id] = grant
    worker.active_interaction_id = "interaction-calc"
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(worker, "_send", sent.append)
    code = "print('private-source-marker')"
    worker._handle_tool({"id": "calc-call", "session_ref": session.model_dump(mode="json"),
                         "arguments": {"tool_name": "jarvis_python", "grant_id": grant_id, "code": code}})
    with open_sqlite_connection() as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT payload FROM events WHERE event_type = 'hermes.tool_result' "
                                 "AND target_id = 'calc-call'").fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["status"] == "succeeded" and payload["arguments"] == {}
    assert "private-source-marker" not in row["payload"]
    assert sent[-1]["tool_result"]["result"] == {"text": "pressure 12.5 kPa\n"}
    evidence = worker.turn_evidence_ledger
    assert any(item.value == 12.5 and item.source_ref == "tool:calc-call" for item in evidence)
    checked = compute_number_provenance("The pressure is 12.5 kPa.", evidence,
                                        interaction_id="interaction-calc", workspace_id=workspace_id)
    assert checked["verified_count"] >= 1


def test_only_the_calculation_runner_is_reachable() -> None:
    tree = ast.parse(inspect.getsource(calc_run_tool))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {f"{node.module}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 for alias in node.names}
    assert not {name for name in imported if name.split(".")[0] in {"subprocess", "multiprocessing", "asyncio",
                                                                      "ctypes", "pty"}}
    assert "app.modules.studio_runtime.runtime" in imported or "app.modules.studio_runtime.runtime" in {
        f"app.modules.studio_runtime.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module == "app.modules.studio_runtime" for alias in node.names}
    run_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "run"]
    assert len(run_calls) == 1
    assert [(kw.arg, ast.literal_eval(kw.value)) for kw in run_calls[0].keywords] == [("profile", "calculation")]
    assert len(run_calls[0].args) == 1
    source = inspect.getsource(calc_run_tool)
    for forbidden in ("geometry", "artifact_dir", "os.system", "Popen", "exec(", "eval("):
        assert forbidden not in source


@pytest.mark.parametrize("surface", ["none", "process", "bluecad", "studio"])
@pytest.mark.parametrize("question", ["", "Hello", "Compute the pressure drop", "patent paper research code repo python",
                                      "Find papers about this", "Read the patent claims and the repository source file"])
def test_python_is_first_and_at_most_seven_tools(surface: str, question: str) -> None:
    names = _turn_tools(surface, question)
    assert names[0] == "mcp__jarvis__jarvis_python"
    assert len(names) <= 7 and len(set(names)) == len(names)
    assert set(names) <= HERMES_TOOL_ALLOWLIST


def test_bluecad_router_keeps_bench_tools_and_drops_knowledge_read() -> None:
    names = _turn_tools("bluecad", "Hello")
    assert names[0] == "mcp__jarvis__jarvis_python" and len(names) == 7
    assert "mcp__jarvis__jarvis_bench_read" in names and "mcp__jarvis__jarvis_bench_run" in names
    assert "mcp__jarvis__jarvis_knowledge_read" not in names


def _brief(surface: str, workspace: str) -> SurfaceBrief:
    return SurfaceBrief(surface=surface, route_id=f"design-{surface}" if surface != "none" else "none",
                        workspace_id=workspace, base_revision="rev-1", draft_id="d-1", summary="s",
                        selected=[], text="t", digest="sha256:" + "0" * 64)


@pytest.mark.parametrize("surface", ["none", "process", "bluecad", "studio"])
@pytest.mark.parametrize("compact", [True, False])
def test_every_surface_holds_one_calc_grant(workspace_id: str, surface: str, compact: bool) -> None:
    worker = SimpleNamespace(live_grants={})
    text = _install_surface_grants(worker, workspace_id, "thread-1", SurfaceRef(route_id=f"design-{surface}"),
                                   _brief(surface, workspace_id), compact=compact)
    grants = [g for g in worker.live_grants.values() if g.capability_id == "jarvis.calc_run"]
    assert len(grants) == 1
    grant = grants[0]
    assert f"mcp__jarvis__jarvis_python grant_id={grant.grant_id}" in text
    assert grant.scope.workspace_id == workspace_id and grant.scope.jarvis_thread_id == "thread-1"
    assert grant.expires_at - grant.issued_at == timedelta(seconds=turn_deadline(120, 10) + 60)
    if surface == "none":
        assert len(worker.live_grants) == 1
    else:
        assert any(g.capability_id == f"jarvis.{surface}_read" for g in worker.live_grants.values())
        assert any(g.expires_at == grant.expires_at or abs((g.expires_at - grant.expires_at).total_seconds()) < 5
                   for g in worker.live_grants.values() if g is not grant)


def test_tool_is_admitted_everywhere_with_the_exact_description() -> None:
    assert _TOOL_CAPABILITIES["jarvis_python"] == "jarvis.calc_run"
    assert "mcp__jarvis__jarvis_python" in HERMES_TOOL_ALLOWLIST
    assert "mcp__jarvis__jarvis_python" in ADMITTED_TOOLS
    tool = next(item for item in _TOOLS if item["name"] == "jarvis_python")
    assert tool["description"] == DESCRIPTION
    schema = tool["inputSchema"]
    assert schema["required"] == ["grant_id", "code"] and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"grant_id", "code"} and schema["properties"]["code"]["maxLength"] == 16384
    config = pinned_config("http://127.0.0.1:1", "token")
    assert "jarvis_python" in config["mcp_servers"]["jarvis"]["tools"]["include"]
    assert "never compute numbers in your head" in COMPACT_METHODOLOGY.lower()
    assert "jarvis_python" in COMPACT_METHODOLOGY and "print the results" in COMPACT_METHODOLOGY


def test_calc_grant_does_not_authorize_other_capabilities(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    double = _install(monkeypatch, _record(stdout="no"))
    result, _ = _call(workspace_id, {"code": "print(1)"}, capability="jarvis.bench_run")
    assert result.status == "refused" and double.calls == []


def test_sandbox_integration_ode_and_pint(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")
    if os.environ.get("JARVISOS_STUDIO_USER_CODE_ENABLED") != "1":
        pytest.skip("JARVISOS_STUDIO_USER_CODE_ENABLED=1 is not set")
    get_settings.cache_clear()
    try:
        state = runtime.status()
        if not (state["enabled"] and state["available"]):
            pytest.skip(f"calculation sandbox unavailable: {state.get('reason')}")
        code = (
            "import numpy as np\n"
            "from scipy.integrate import solve_ivp\n"
            "import pint\n"
            "sol = solve_ivp(lambda t, y: -0.5 * y, (0, 2), [1.0], rtol=1e-9, atol=1e-12)\n"
            "print('ode', round(float(sol.y[0, -1]), 6))\n"
            "q = pint.UnitRegistry().Quantity(3.0, 'bar').to('kPa')\n"
            "print('pressure', round(float(q.magnitude), 3), str(q.units))\n"
        )
        result, _ = _call(workspace_id, {"code": code})
    finally:
        get_settings.cache_clear()
    assert result.status == "succeeded", result
    text = result.result["text"]
    assert "ode 0.367879" in text and "pressure 300.0 kilopascal" in text
