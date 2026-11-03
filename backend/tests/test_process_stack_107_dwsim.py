"""107 DWSIM boundary checks using the real stdio protocol and opt-in runtime."""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.modules.process_stack.dwsim import DwsimEvaluator, _connector_records

WS = "dwsim-test"


def _case(path: Path) -> Path:
    xml = """<Simulation>
      <GraphicObject><Name>MAT-IN</Name><Tag>FeedA</Tag><InputConnectors><Connector IsAttached="false" /></InputConnectors><OutputConnectors><Connector IsAttached="true" ConnType="ConOut" AttachedToObjID="MIX-1" AttachedToConnIndex="0" AttachedToEnergyConn="False" /></OutputConnectors></GraphicObject>
      <GraphicObject><Name>MAT-OUT</Name><Tag>Product</Tag><InputConnectors><Connector IsAttached="true" ConnType="ConIn" AttachedToObjID="MIX-1" AttachedToConnIndex="1" AttachedToEnergyConn="False" /></InputConnectors><OutputConnectors><Connector IsAttached="false" /></OutputConnectors></GraphicObject>
    </Simulation>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("case.xml", xml)
    return path


def _fake_peer(path: Path, mode: str) -> tuple[Path, str]:
    executable = path / "fake-dwsim"
    executable.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(
            f"""
            import json, sys
            mode = {mode!r}
            for line in sys.stdin:
                request = json.loads(line)
                method = request.get("method")
                if method == "notifications/initialized":
                    continue
                params = request.get("params", {{}})
                if method == "initialize":
                    result = {{"protocolVersion": "2024-11-05", "serverInfo": {{"name": "fake", "version": "10.2.9"}}, "capabilities": {{}}}}
                elif method == "tools/list":
                    result = {{"tools": []}}
                elif method == "tools/call":
                    name, args = params["name"], params["arguments"]
                    if name == "dwsim_flowsheet_load": result = {{"flowsheet_id": "flow-1"}}
                    elif name == "dwsim_stream_set_conditions": result = {{"ok": True}}
                    elif name == "dwsim_stream_get_results":
                        flow = 1.1 if mode == "mass_failure" and args["name"] == "Product" else 1.0
                        if mode == "readback_mismatch" and args["name"] == "FeedA": flow = 2.0
                        result = {{"mass_flow_kg_s": flow, "temperature_K": 300.0, "pressure_Pa": 101325.0}}
                    elif name == "dwsim_flowsheet_list_objects":
                        result = {{"objects": [
                            {{"name": "FeedA", "id": "MAT-IN", "type": "MaterialStream", "x": 1, "y": 2, "calculated": True, "error": ""}},
                            {{"name": "Product", "id": "MAT-OUT", "type": "MaterialStream", "x": 3, "y": 4, "calculated": True, "error": ""}},
                            {{"name": "M1", "id": "MIX-1", "type": "Mixer", "x": 5, "y": 6, "calculated": True, "error": ""}}]}}
                    elif name == "dwsim_solve_run":
                        if mode == "blockers": result = {{"ok": True, "findings": [{{"code": "NO_PROPERTY_PACKAGE"}}], "blockers": [{{"code": "NOT_CONVERGED"}}], "objects": []}}
                        elif mode == "uncalculated": result = {{"ok": True, "findings": [], "blockers": [], "errors": [], "objects": [{{"name": "LG-007", "calculated": False, "error": ""}}]}}
                        else: result = {{"ok": True, "findings": [], "blockers": [], "errors": [], "objects": [{{"calculated": True, "error": ""}}]}}
                    elif name == "dwsim_dynamics_run": result = {{"run_id": "run-1", "duration_s": 20.0}}
                    elif name == "dwsim_dynamics_status": result = {{"state": "completed", "simulated_s": "20", "summary": {{"completed": True, "simulated_s": "25", "errors": []}}}}
                    elif name == "dwsim_dynamics_series": result = {{"points": [{{"time_s": 0.0}}, {{"time_s": 5.0}}, {{"time_s": 20.0}}]}}
                    else: result = {{}}
                    result = {{"structuredContent": result}}
                else:
                    continue
                print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": result}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executable.with_name("fake-dwsim.deps.json").write_text(
        json.dumps({"libraries": {"DWSIM.Automation/10.2.9": {"type": "package"}}}), encoding="utf-8"
    )
    return executable, hashlib.sha256(executable.read_bytes()).hexdigest()


def _request(case_path: Path, **options: object):
    now = datetime.now(UTC)
    return {
        "request_ref": {"authority_owner": "dwsim_test", "object_type": "evaluation_request", "object_id": "req-1", "workspace_id": WS, "revision": "1"},
        "evaluator_id": "dwsim.flowsheet",
        "subject_ref": {"authority_owner": "dwsim_test", "object_type": "process_model_ir", "object_id": "case-1", "workspace_id": WS, "revision": "1"},
        "inputs": [{"name": "stream.FeedA.mass_flow", "value": {"value": 1.0, "unit": "kg/s"}}],
        "backend_options": {"case_path": str(case_path), **options},
        "requested_at": now,
        "deadline_at": now + timedelta(minutes=10),
    }


def test_saved_xml_projection_preserves_native_connector_ids_and_ports(tmp_path: Path) -> None:
    by_id, by_tag, available = _connector_records(_case(tmp_path / "case.dwxmz"))
    assert available
    assert by_tag["FeedA"]["output"][0]["native_object_id"] == "MIX-1"
    assert by_tag["Product"]["input"][0]["native_connector_index"] == 1
    assert by_id["MAT-IN"]["energy"] == []


def _configure(monkeypatch: pytest.MonkeyPatch, executable: Path, digest: str) -> None:
    monkeypatch.setenv("JARVISOS_DWSIM_MCP_PATH", str(executable))
    monkeypatch.setenv("JARVISOS_DWSIM_MCP_SHA256", digest)


def test_stream_write_requires_authoritative_readback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_path = _case(tmp_path / "case.dwxmz")
    executable, digest = _fake_peer(tmp_path, "readback_mismatch")
    _configure(monkeypatch, executable, digest)
    result = DwsimEvaluator().evaluate(type_request(_request(case_path)))
    assert result.status == "refused"
    assert result.failure and result.failure.backend_code == "DWSIM_READBACK_MISMATCH"


def test_ok_true_with_blockers_is_not_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_path = _case(tmp_path / "case.dwxmz")
    executable, digest = _fake_peer(tmp_path, "blockers")
    _configure(monkeypatch, executable, digest)
    result = DwsimEvaluator().evaluate(type_request(_request(case_path)))
    assert result.status == "failed"
    assert result.failure and result.failure.backend_code == "NO_PROPERTY_PACKAGE"


def test_ok_true_with_uncalculated_object_is_not_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_path = _case(tmp_path / "case.dwxmz")
    executable, digest = _fake_peer(tmp_path, "uncalculated")
    _configure(monkeypatch, executable, digest)
    result = DwsimEvaluator().evaluate(type_request(_request(case_path)))
    assert result.status == "failed"
    assert result.failure and result.failure.backend_code == "DWSIM_OBJECT_UNCALCULATED"


def test_mass_residual_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_path = _case(tmp_path / "case.dwxmz")
    executable, digest = _fake_peer(tmp_path, "mass_failure")
    _configure(monkeypatch, executable, digest)
    result = DwsimEvaluator().evaluate(type_request(_request(case_path)))
    assert result.status == "failed"
    assert result.failure and result.failure.backend_code == "DWSIM_MASS_RESIDUAL"


def test_dynamic_time_series_is_authoritative_and_reports_discrepancy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case_path = _case(tmp_path / "case.dwxmz")
    executable, digest = _fake_peer(tmp_path, "ok")
    _configure(monkeypatch, executable, digest)
    # The series-derived end time and summary discrepancy remain visible as numeric diagnostics.
    request = type_request(_request(case_path, dynamic=True, duration_s=20.0))
    result = DwsimEvaluator().evaluate(request)
    assert result.status == "succeeded"
    assert result.validity and result.validity.qualification_status == "unqualified"
    values = {item.name: item.value.value for item in result.outputs}
    assert values["dynamic_end_time"] == 20.0
    assert values["nested_summary_time_discrepancy"] == 5.0


def test_runtime_digest_mismatch_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable, digest = _fake_peer(tmp_path, "ok")
    _configure(monkeypatch, executable, "0" * 64)
    availability = DwsimEvaluator().availability()
    assert availability.state == "unhealthy"
    assert availability.reason_code == "dwsim_mcp_digest_mismatch"
    assert digest != "0" * 64


def type_request(fields):
    from app.modules.engineering.evaluator_contracts import EvaluationRequest

    return EvaluationRequest.model_validate(fields)


@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_mixer_and_tank_smoke(tmp_path: Path) -> None:
    from app.modules.engineering.evaluator_contracts import EvaluationRequest

    runtime = Path(os.environ["JARVISOS_DWSIM_MCP_PATH"])
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    root = runtime.parents[3]
    cases = (root / "mixer.dwxmz", root / "tank-event-restart.dwxmz")
    evidence = {"executable": str(runtime), "executable_sha256": digest, "results": []}
    expected = {"mixer.dwxmz": "succeeded", "tank-event-restart.dwxmz": "failed"}
    for case in cases:
        now = datetime.now(UTC)
        options: dict[str, object] = {"case_path": str(case)}
        if case.name.startswith("tank"):
            options.update(dynamic=True, duration_s=20.0)
        request_fields = _request(case, **{k: v for k, v in options.items() if k != "case_path"})
        if case.name.startswith("tank"):
            request_fields["inputs"] = []
        request = EvaluationRequest.model_validate(request_fields | {"backend_options": options,
            "request_ref": {"authority_owner": "dwsim_test", "object_type": "evaluation_request", "object_id": f"req-{case.stem}", "workspace_id": WS, "revision": "1"},
            "requested_at": now, "deadline_at": now + timedelta(minutes=10)})
        result = DwsimEvaluator().evaluate(request)
        evidence["results"].append({"case": case.name, "case_sha256": hashlib.sha256(case.read_bytes()).hexdigest(),
                                         "status": result.status, "failure": result.failure.model_dump(mode="json") if result.failure else None,
                                         "backend_version": result.backend_version, "validity": result.validity.qualification_status if result.validity else None,
                                         "outputs": {item.name: item.value.model_dump(mode="json") for item in result.outputs},
                                         "evidence_refs": [ref.model_dump(mode="json") for ref in result.evidence_refs]})
        assert result.status == expected[case.name], result.failure
        if case.name == "tank-event-restart.dwxmz":
            assert result.failure and result.failure.backend_code == "DWSIM_OBJECT_UNCALCULATED"
