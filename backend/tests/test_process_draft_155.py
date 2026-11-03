"""Spec 155: Jarvis-owned process draft, verified DWSIM materialization, run binding and proposals."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from app.core.database import initialize_database
from app.main import app
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.process_stack import draft, draft_compiler, editor
from app.modules.process_stack.draft_models import DraftOp

Q = lambda value, unit: {"value": value, "unit": unit}  # noqa: E731


@pytest.fixture()
def api() -> Any:
    initialize_database()
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "155 draft", "slug": f"draft-155-{id(client)}"})
        workspace.raise_for_status()
        base = f"/workspaces/{workspace.json()['id']}/process/drafts"
        created = client.post(base, json={"name": "Synthetic train"})
        created.raise_for_status()
        yield client, base, created.json(), workspace.json()["id"]


def _patch(client: TestClient, base: str, state: dict[str, Any], *ops: dict[str, Any], status: int = 200) -> Any:
    response = client.post(f"{base}/{state['draft_id']}/patch",
                           json={"expected_revision": state["revision"], "ops": list(ops)})
    assert response.status_code == status, response.text
    if status == 200:
        state.update(response.json())
    return response.json()


TRAIN = [
    {"op": "set_thermo", "compounds": ["Water", "Methanol"], "property_package": "NRTL"},
    {"op": "add_stream", "id": "feed", "tag": "Feed", "x": 40, "y": 120},
    {"op": "add_unit", "id": "h1", "type": "Heater", "tag": "H1", "x": 140, "y": 120},
    {"op": "add_stream", "id": "s2", "tag": "S2", "x": 220, "y": 120},
    {"op": "add_unit", "id": "v1", "type": "Valve", "tag": "V1", "x": 300, "y": 120},
    {"op": "add_stream", "id": "s3", "tag": "S3", "x": 380, "y": 120},
    {"op": "add_unit", "id": "fl1", "type": "Flash", "tag": "FL1", "x": 460, "y": 120},
    {"op": "add_stream", "id": "vap", "tag": "Vap", "x": 540, "y": 60},
    {"op": "add_stream", "id": "liq", "tag": "Liq", "x": 540, "y": 180},
    {"op": "connect", "stream": "feed", "end": "target", "unit": "h1", "port": 0},
    {"op": "connect", "stream": "s2", "end": "source", "unit": "h1", "port": 0},
    {"op": "connect", "stream": "s2", "end": "target", "unit": "v1", "port": 0},
    {"op": "connect", "stream": "s3", "end": "source", "unit": "v1", "port": 0},
    {"op": "connect", "stream": "s3", "end": "target", "unit": "fl1", "port": 0},
    {"op": "connect", "stream": "vap", "end": "source", "unit": "fl1", "port": 0},
    {"op": "connect", "stream": "liq", "end": "source", "unit": "fl1", "port": 1},
    {"op": "set_stream_spec", "stream": "feed", "temperature": Q(25, "degC"), "pressure": Q(5, "bar"),
     "mass_flow": Q(3600, "kg/h"), "composition": {"Water": 0.7, "Methanol": 0.3}},
    {"op": "set_unit_params", "unit": "h1", "values": {"outlet_temperature": Q(95, "degC")}},
    {"op": "set_unit_params", "unit": "v1", "mode": "outlet_pressure", "values": {"outlet_pressure": Q(1.2, "bar")}},
]


def _built(client: TestClient, base: str, state: dict[str, Any]) -> dict[str, Any]:
    _patch(client, base, state, *TRAIN)
    return state


class _FakeDwsim:
    """In-memory DWSIM MCP stand-in: records calls and saves a native-shaped XML case."""

    def __init__(self, *, drop: str | None = None) -> None:
        self.calls: list[str] = []
        self.drop = drop
        self.streams: dict[str, dict[str, Any]] = {}
        self.units: dict[str, dict[str, Any]] = {}
        self.links: list[tuple[str, str, int, str]] = []  # (unit, direction, port, stream)
        self.pos: dict[str, tuple[int, int]] = {}
        self.compounds: list[str] = []
        self.package = ""

    def __enter__(self) -> _FakeDwsim:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, name: str, args: dict[str, Any], _timeout: float) -> dict[str, Any]:
        self.calls.append(name)
        if name == "dwsim_flowsheet_create":
            return {"flowsheet_id": "flow"}
        if name == "dwsim_thermo_add_compounds":
            self.compounds = list(args["names"])
        elif name == "dwsim_thermo_set_property_package":
            self.package = args["name"]
        elif name == "dwsim_stream_add_material":
            self.streams[args["name"]] = {key: value for key, value in args.items() if key not in {"flowsheet_id", "name"}}
        elif name == "dwsim_unitop_add":
            self.units[args["name"]] = {"type": args["type"], "props": {}}
        elif name == "dwsim_graphic_edit":
            self.pos[args["name"]] = (args["x"], args["y"])
        elif name == "dwsim_unitop_connect":
            if "feed_stream" in args:
                self.links.append((args["unitop"], "in", args["feed_port"], args["feed_stream"]))
            else:
                self.links.append((args["unitop"], "out", args["product_port"], args["product_stream"]))
        elif name == "dwsim_unitop_set":
            if args["name"] != self.drop:
                self.units[args["name"]]["props"].update(args["properties"])
        elif name == "dwsim_flowsheet_save":
            self._save(Path(args["filepath"]))
        elif name == "dwsim_stream_get_results":
            spec = self.streams[args["name"]]
            return {"temperature_K": spec.get("temperature_K", 298.15), "pressure_Pa": spec.get("pressure_Pa", 101325.0),
                    "mass_flow_kg_s": spec.get("mass_flow_kg_s", 1.0),
                    "phases": [{"name": "Mixture", "compounds": {
                        name: {"mass_fraction": (spec.get("composition") or {}).get(name, 0.0)}
                        for name in self.compounds}}, {"name": "Vapor", "fraction": 0.0}]}
        elif name == "dwsim_flowsheet_check":
            return {"ready": True, "findings": []}
        elif name == "dwsim_solve_run":
            return {"ok": True, "errors": []}
        elif name == "dwsim_flowsheet_list_objects":
            return {"objects": [{"name": tag, "type": "MaterialStream", "calculated": True, "error": ""}
                                for tag in self.streams]}
        elif name == "dwsim_unitop_get_results":
            return {"calculated": True, "error": "", "properties": {}}
        return {}

    def _save(self, path: Path) -> None:
        ids = {tag: f"MAT-{tag}" for tag in self.streams} | {tag: f"UO-{tag}" for tag in self.units}
        native = {"Heater": "Heater", "Valve": "Valve", "Vessel": "Vessel", "Pump": "Pump", "Cooler": "Cooler",
                  "Mixer": "Mixer"}
        sims, graphics = [], []
        for tag, oid in ids.items():
            typ = "MaterialStream" if tag in self.streams else native[self.units[tag]["type"]]
            props = "".join(f"<{key}>{value}</{key}>" for key, value in self.units.get(tag, {}).get("props", {}).items())
            sims.append(f"<SimulationObject><Type>DWSIM.X.{typ}</Type><ComponentName>{oid}</ComponentName>{props}"
                        "</SimulationObject>")
            ins = sorted((port, stream) for unit, d, port, stream in self.links if unit == tag and d == "in")
            outs = sorted((port, stream) for unit, d, port, stream in self.links if unit == tag and d == "out")
            if tag in self.streams:
                ins = [(0, unit) for unit, d, _p, stream in self.links if stream == tag and d == "out"]
                outs = [(0, unit) for unit, d, _p, stream in self.links if stream == tag and d == "in"]
                in_xml = "".join(
                    f'<Connector IsAttached="true" AttachedFromObjID="{ids[u]}" AttachedFromConnIndex="'
                    f'{next(p for uu, d, p, s in self.links if uu == u and s == tag)}"/>' for _i, u in ins)
                out_xml = "".join(
                    f'<Connector IsAttached="true" AttachedToObjID="{ids[u]}" AttachedToConnIndex="'
                    f'{next(p for uu, d, p, s in self.links if uu == u and s == tag)}"/>' for _i, u in outs)
            else:
                width_in = max([p for p, _s in ins], default=-1) + 1
                width_out = max([p for p, _s in outs], default=-1) + 1
                by_in, by_out = dict(ins), dict(outs)
                in_xml = "".join(
                    f'<Connector IsAttached="true" AttachedFromObjID="{ids[by_in[p]]}" AttachedFromConnIndex="0"/>'
                    if p in by_in else '<Connector IsAttached="false"/>' for p in range(width_in))
                out_xml = "".join(
                    f'<Connector IsAttached="true" AttachedToObjID="{ids[by_out[p]]}" AttachedToConnIndex="0"/>'
                    if p in by_out else '<Connector IsAttached="false"/>' for p in range(width_out))
            x, y = self.pos.get(tag, (0, 0))
            graphics.append(f"<GraphicObject><Name>{oid}</Name><Tag>{tag}</Tag><X>{x}</X><Y>{y}</Y>"
                            f"<InputConnectors>{in_xml}</InputConnectors><OutputConnectors>{out_xml}</OutputConnectors>"
                            "</GraphicObject>")
        compounds = "".join(f"<Compound><Name>{name}</Name></Compound>" for name in self.compounds)
        path.write_text(f"<Simulation><SimulationObjects>{''.join(sims)}</SimulationObjects>"
                        f"<GraphicObjects>{''.join(graphics)}</GraphicObjects>"
                        f"<PropertyPackages><PropertyPackage><ComponentName>{self.package}</ComponentName>"
                        f"</PropertyPackage></PropertyPackages><Compounds>{compounds}</Compounds></Simulation>",
                        encoding="utf-8")


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeDwsim) -> None:
    monkeypatch.setattr(editor, "_client", lambda: (fake, "a" * 64, "10.2.9"))
    monkeypatch.setattr(draft_compiler, "_mass_balance", lambda *_args: (0.0, {"Feed": 1.0}))


def test_edits_are_instant_revisions_without_dwsim(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, base, state, _ws = api
    monkeypatch.setattr(editor, "_client", lambda: pytest.fail("draft edits must not contact DWSIM"))
    first = state["revision"]
    _built(client, base, state)
    assert state["revision"] != first and state["seq"] == 2
    assert [(item["code"], item["severity"]) for item in state["findings"]] == [
        ("OPTIONAL_PORT_UNCONNECTED", "warning")]
    feed = next(item for item in state["objects"] if item["tag"] == "Feed")
    assert feed["spec"]["temperature"] == {"si": 298.15, "value": 25, "unit": "degC"}
    assert feed["spec"]["pressure"]["si"] == 500000.0 and feed["spec"]["mass_flow"]["si"] == 1.0
    stale = client.post(f"{base}/{state['draft_id']}/patch",
                        json={"expected_revision": first, "ops": [{"op": "move", "id": "h1", "x": 1, "y": 2}]})
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == state["revision"]
    history = client.get(f"{base}/{state['draft_id']}/revisions").json()
    assert [row["seq"] for row in history] == [2, 1]


def test_registry_refuses_unsupported_constructs(api: Any) -> None:
    client, base, state, _ws = api
    _patch(client, base, state, {"op": "set_thermo", "compounds": ["Water"], "property_package": "NRTL"},
           {"op": "add_unit", "id": "p1", "type": "Pump", "tag": "P1", "x": 0, "y": 0},
           {"op": "add_stream", "id": "a", "tag": "A", "x": 0, "y": 0})
    cases = [
        ({"op": "connect", "stream": "a", "end": "target", "unit": "p1", "port": 1}, "port_invalid"),
        ({"op": "set_unit_params", "unit": "p1", "mode": "curves"}, "mode_unsupported"),
        ({"op": "set_unit_params", "unit": "p1", "values": {"pressure_increase": Q(1, "bar")}}, "param_inactive"),
        ({"op": "set_unit_params", "unit": "p1", "values": {"outlet_pressure": Q(5, "degC")}}, "unit_unsupported"),
        ({"op": "set_unit_params", "unit": "p1", "values": {"efficiency": Q(150, "percent")}}, "quantity_out_of_range"),
        ({"op": "set_stream_spec", "stream": "a", "composition": {"Methanol": 1.0}}, "compound_undeclared"),
        ({"op": "set_thermo", "property_package": "Black Oil"}, "package_unsupported"),
        ({"op": "add_stream", "tag": "A", "x": 0, "y": 0}, "tag_conflict"),
    ]
    for op, code in cases:
        body = _patch(client, base, state, op, status=422)
        assert body["detail"]["code"] == code, (op, body)
    with pytest.raises(ValueError):
        TypeAdapter(DraftOp).validate_python({"op": "rename", "id": "p1", "tag": "bad tag!"})


def test_jarvis_validation_findings_and_spec_rules(api: Any) -> None:
    client, base, state, _ws = api
    _patch(client, base, state, {"op": "add_unit", "id": "m1", "type": "Mixer", "tag": "M1", "x": 0, "y": 0},
           {"op": "add_stream", "id": "a", "tag": "A", "x": 0, "y": 0},
           {"op": "connect", "stream": "a", "end": "target", "unit": "m1", "port": 0},
           {"op": "set_thermo", "compounds": ["Water"]},
           {"op": "set_stream_spec", "stream": "a", "composition": {"Water": 0.5}})
    codes = {item["code"] for item in state["findings"]}
    assert {"NO_PROPERTY_PACKAGE", "UNIT_INLET_MISSING", "UNIT_OUTLET_MISSING", "FEED_SPEC_MISSING",
            "COMPOSITION_SUM"} <= codes
    body = _patch(client, base, state, {"op": "add_stream", "id": "b", "tag": "B", "x": 0, "y": 0},
                  {"op": "connect", "stream": "b", "end": "source", "unit": "m1", "port": 0},
                  {"op": "set_stream_spec", "stream": "b", "temperature": Q(300, "K")}, status=422)
    assert body["detail"]["code"] == "spec_on_product"
    _patch(client, base, state, {"op": "delete", "id": "m1"})
    assert all(item.get("target") is None for item in state["objects"])


def test_compiler_plan_and_expected_are_deterministic(api: Any) -> None:
    client, base, state, _ws = api
    _built(client, base, state)
    document = draft.load_revision(draft.draft_dir(state["workspace_id"], state["draft_id"]),
                                   state["revision"])["document"]
    assert draft_compiler.plan(document) == draft_compiler.plan(copy.deepcopy(document))
    expected = draft_compiler.expected(document)
    assert expected["connections"] == sorted(["Feed>H1:in0", "H1:out0>S2", "S2>V1:in0", "V1:out0>S3", "S3>FL1:in0",
                                              "FL1:out0>Vap", "FL1:out1>Liq"])
    assert expected["units"]["V1"] == {"CalcMode": "OutletPressure", "OutletPressure": 120000.0}
    assert expected["units"]["H1"] == {"CalcMode": "OutletTemperature", "OutletTemperature": 368.15, "DeltaP": 0.0}
    assert expected["feeds"]["Feed"]["composition"] == {"Water": 0.7, "Methanol": 0.3}
    fingerprint = draft_compiler.fingerprint(expected, dwsim_version="10.2.9", mcp_sha256="a" * 64)
    assert fingerprint == draft_compiler.fingerprint(copy.deepcopy(expected), dwsim_version="10.2.9",
                                                     mcp_sha256="a" * 64)
    assert fingerprint != draft_compiler.fingerprint(expected, dwsim_version="10.3.0", mcp_sha256="a" * 64)


def test_run_binds_revision_goes_stale_and_refuses_mismatch(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, base, state, _ws = api
    _built(client, base, state)
    solved = state["revision"]
    fake = _FakeDwsim()
    _use(monkeypatch, fake)
    run = client.post(f"{base}/{state['draft_id']}/revisions/{solved}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["run"]["status"] == "completed" and body["run"]["materialization_diffs"] == []
    assert body["run"]["materialization_fingerprint"] == body["run"]["expected_fingerprint"]
    assert body["run"]["draft_revision"] == solved and body["draft"]["results"]["state"] == "current"
    assert body["run"]["streams"]["Feed"]["display"]["temperature"] == {"value": 25, "unit": "degC"}
    again = client.post(f"{base}/{state['draft_id']}/revisions/{solved}/validate").json()["run"]
    assert again["materialization_fingerprint"] == body["run"]["materialization_fingerprint"]

    _patch(client, base, state, {"op": "set_route", "stream": "feed",
                                  "points": [{"x": 40, "y": 120}, {"x": 90, "y": 120}]})
    assert state["results"]["state"] == "current"

    _use(monkeypatch, _FakeDwsim(drop="V1"))
    refused = client.post(f"{base}/{state['draft_id']}/revisions/{solved}/run").json()
    assert refused["run"]["status"] == "materialization_mismatch"
    assert {diff["path"] for diff in refused["run"]["materialization_diffs"]} == {
        "units.V1.CalcMode", "units.V1.OutletPressure"}
    assert "dwsim_solve_run" not in refused["run"].get("calls", []) and refused["run"].get("streams") is None
    assert refused["draft"]["results"]["state"] == "current"

    # Spec 162: a move is layout-only and keeps results current; a process edit makes them stale.
    _patch(client, base, state, {"op": "move", "id": "h1", "x": 150, "y": 120})
    assert state["results"]["state"] == "current"
    _patch(client, base, state, {"op": "set_unit_params", "unit": "h1", "values": {"outlet_temperature": Q(90, "degC")}})
    assert state["results"]["state"] == "stale" and state["results"]["edits_since"] == 1
    assert state["results"]["draft_revision"] == solved


def test_invalid_draft_is_refused_before_dwsim(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, base, state, _ws = api
    monkeypatch.setattr(editor, "_client", lambda: pytest.fail("an invalid draft must not reach DWSIM"))
    response = client.post(f"{base}/{state['draft_id']}/revisions/{state['revision']}/run")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "draft_invalid"


def test_proposal_lifecycle(api: Any) -> None:
    client, base, state, _ws = api
    _built(client, base, state)
    url = f"{base}/{state['draft_id']}/proposals"
    proposal = client.post(url, json={"base_revision": state["revision"], "source": "hermes:thread", "changes": [
        {"target": "V1", "property": "outlet_pressure", "proposed": Q(2, "bar")},
        {"target": "Feed", "property": "temperature", "proposed": Q(30, "degC")}]})
    assert proposal.status_code == 200, proposal.text
    pending = proposal.json()
    assert pending["state"] == "pending"
    assert pending["changes"][0]["current"] == {"value": 1.2, "unit": "bar"}
    assert pending["changes"][1]["current"] == {"value": 25.0, "unit": "degC"}
    head = client.get(f"{base}/{state['draft_id']}").json()
    assert head["revision"] == state["revision"] and head["proposals"][0]["proposal_id"] == pending["proposal_id"]

    rejected = client.post(url, json={"base_revision": state["revision"], "changes": [
        {"target": "V1", "property": "outlet_pressure", "proposed": Q(3, "bar")}]}).json()
    client.post(f"{url}/{rejected['proposal_id']}/reject").raise_for_status()
    assert client.get(f"{base}/{state['draft_id']}").json()["revision"] == state["revision"]

    approved = client.post(f"{url}/{pending['proposal_id']}/approve", json={"accepted_changes": [0]})
    assert approved.status_code == 200, approved.text
    applied = approved.json()
    assert applied["proposal"]["state"] == "approved" and applied["draft"]["seq"] == state["seq"] + 1
    valve = next(item for item in applied["draft"]["objects"] if item["tag"] == "V1")
    feed = next(item for item in applied["draft"]["objects"] if item["tag"] == "Feed")
    assert valve["params"]["outlet_pressure"]["si"] == 200000.0 and feed["spec"]["temperature"]["value"] == 25
    history = client.get(f"{base}/{state['draft_id']}/revisions").json()
    assert history[0]["actor"] == f"hermes_proposal:{pending['proposal_id']}"
    again = client.post(f"{url}/{pending['proposal_id']}/approve", json={})
    assert again.status_code == 409


def test_stale_and_invalid_proposals_are_refused(api: Any) -> None:
    client, base, state, _ws = api
    _built(client, base, state)
    url = f"{base}/{state['draft_id']}/proposals"
    pending = client.post(url, json={"base_revision": state["revision"], "changes": [
        {"target": "H1", "property": "outlet_temperature", "proposed": Q(90, "degC")}]}).json()
    _patch(client, base, state, {"op": "move", "id": "v1", "x": 310, "y": 120})
    listed = client.get(url).json()
    assert listed[0]["state"] == "stale"
    stale = client.post(f"{url}/{pending['proposal_id']}/approve", json={})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "proposal_stale"
    for change, code in [
        ({"target": "Nope", "property": "pressure", "proposed": Q(1, "bar")}, "proposal_target_unknown"),
        ({"target": "V1", "property": "Kv", "proposed": Q(1, "bar")}, "proposal_property_unsupported"),
        ({"target": "V1", "property": "outlet_pressure", "proposed": Q(1, "degC")}, "proposal_value_invalid"),
        ({"target": "S2", "property": "temperature", "proposed": Q(300, "K")}, "spec_on_product"),
    ]:
        response = client.post(url, json={"base_revision": state["revision"], "changes": [change]})
        assert response.status_code == 422 and response.json()["detail"]["code"] == code, response.text
    old_base = client.post(url, json={"base_revision": "1:" + "0" * 16, "changes": [
        {"target": "V1", "property": "outlet_pressure", "proposed": Q(1, "bar")}]})
    assert old_base.status_code == 409


def test_hermes_process_tools_read_and_act_only_within_surface_grant(api: Any) -> None:
    """166 supersedes 155's unscoped read/propose: grants carry the turn's Process surface."""
    from app.modules.agents.hermes.supervisor import dispatch_tool
    from app.modules.workspace_actions import service

    client, base, state, workspace_id = api
    _built(client, base, state)
    now = datetime.now(UTC)
    session = AgentSessionRef(jarvis_thread_id="thread-1", hermes_session_id="hermes-1", profile_id="default",
                              workspace_id=workspace_id, generation=1, upstream_revision="r" * 40)
    surface = {"surface": "process", "route_id": "design-process", "draft_id": state["draft_id"],
               "base_revision": state["revision"], "process_selection_count": 1,
               "process_selection_0_kind": "unit", "process_selection_0_tag": "V1"}

    def call(capability: str, arguments: dict[str, Any], grant_workspace: str = workspace_id,
             constraints: dict[str, Any] | None = None) -> Any:
        grant = CapabilityGrantRef(grant_id="g", capability_id=capability, issuer="jarvis_policy",
                                   scope=CapabilityScope(workspace_id=grant_workspace, jarvis_thread_id="thread-1"),
                                   constraints=surface if constraints is None else constraints,
                                   issued_at=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=5))
        tool_call = StructuredToolCall(call_id="c", capability_id=capability, grant_id="g", correlation_id="c",
                                       session_ref=session, arguments=arguments, requested_at=now,
                                       deadline_at=now + timedelta(minutes=1))
        return dispatch_tool(tool_call, live_grants={"g": grant}, interaction_id="interaction-1")

    read = call("jarvis.process_read", {})
    assert read.status == "succeeded" and read.result is not None
    assert read.result["surface"] == "process" and read.result["base_revision"] == state["revision"]
    valve = next(item for item in read.result["selected"] if item["tag"] == "V1")
    assert "outlet_pressure" in json.dumps(valve)
    assert call("jarvis.process_read", {}, grant_workspace="other").status == "refused"
    assert call("jarvis.process_read", {}, constraints={}).status == "refused"
    proposed = call("jarvis.process_act", {"base_revision": state["revision"], "rationale": "test", "actions": [
        {"op": "set_value", "target": "V1", "property": "outlet_pressure", "value": {"value": 2, "unit": "bar"}}]})
    assert proposed.status == "succeeded" and proposed.result["state"] == "proposed", proposed.result
    assert client.get(f"{base}/{state['draft_id']}").json()["revision"] == state["revision"]
    stored = service.list_for(workspace_id, thread_id="thread-1")
    assert [item.state for item in stored] == ["proposed"] and stored[0].origin.kind == "local"
    refused = call("jarvis.process_act", {"base_revision": state["revision"], "actions": [
        {"op": "set_value", "target": "V1", "property": "kinetics", "value": "monod"}]})
    assert refused.status == "succeeded" and refused.result["state"] == "refused"
    assert client.get(f"{base}/{state['draft_id']}").json()["revision"] == state["revision"]


def test_workspace_isolation(api: Any) -> None:
    client, base, state, _ws = api
    other = client.post("/workspaces", json={"name": "other", "slug": f"other-155-{id(state)}"}).json()["id"]
    response = client.get(f"/workspaces/{other}/process/drafts/{state['draft_id']}")
    assert response.status_code == 404
    assert client.get("/workspaces/nope/process/drafts").status_code == 404


def test_confused_process_grant_is_refused_with_a_correcting_code(api: Any) -> None:
    from app.modules.agents.hermes.supervisor import dispatch_tool
    from app.modules.workspace_actions import service

    client, base, state, workspace_id = api
    _built(client, base, state)
    now = datetime.now(UTC)
    session = AgentSessionRef(jarvis_thread_id="thread-1", hermes_session_id="hermes-1", profile_id="default",
                              workspace_id=workspace_id, generation=1, upstream_revision="r" * 40)
    read_grant = CapabilityGrantRef(grant_id="read", capability_id="jarvis.process_read", issuer="jarvis_policy",
                                    scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id="thread-1"),
                                    constraints={"surface": "process", "route_id": "design-process",
                                                 "draft_id": state["draft_id"]},
                                    issued_at=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=5))
    call = StructuredToolCall(call_id="c", capability_id="jarvis.process_act", grant_id="read", correlation_id="c",
                              session_ref=session, requested_at=now, deadline_at=now + timedelta(minutes=1),
                              arguments={"base_revision": state["revision"], "actions": [
                                  {"op": "set_value", "target": "V1", "property": "outlet_pressure",
                                   "value": {"value": 2, "unit": "bar"}}]})
    result = dispatch_tool(call, live_grants={"read": read_grant})
    assert result.status == "refused" and result.error_code == "use_process_act_grant_id"
    assert service.list_for(workspace_id, thread_id="thread-1") == []
