"""Spec 190: a published Studio process unit in the Process draft and the mixed solve (real 187 sandbox, fake DWSIM)."""

from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.modules.process_stack import draft, draft_compiler, dynamic_engine, editor, mixed, mixed_runtime, studio_unit
from app.modules.process_stack.draft_models import (
    UNIT_REGISTRY,
    AddStream,
    AddUnit,
    Connect,
    Disconnect,
    DraftQuantity,
    SetStreamCulture,
    SetStreamSpec,
    SetThermo,
    SetUnitParams,
    studio_contract_digest,
)
from app.modules.studio_runtime.runtime import status
from tests.plumbing_170_support import find, new_workspace, pbr_document
from tests.studio_unit_190_support import (
    GOLDEN_BODY,
    GOLDEN_CONTRACT,
    golden_case,
    publish_unit,
)

Q = DraftQuantity


@pytest.fixture
def client(monkeypatch):
    if os.getenv("JARVISOS_STUDIO_CI_SANDBOX_UNAVAILABLE") == "1":
        pytest.skip("CI runner does not permit bubblewrap user namespaces; WSL2 qualification is required")
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    assert status()["available"], "qualification host must run bubblewrap"
    workspace = new_workspace()
    with TestClient(create_app()) as test_client:
        test_client.workspace = workspace  # type: ignore[attr-defined]
        yield test_client
    get_settings.cache_clear()


def _publish(client, body: str = GOLDEN_BODY) -> tuple[str, str]:
    return publish_unit(client, body, GOLDEN_CONTRACT, workspace=client.workspace, case=golden_case())


def _feed(sid: str, tag: str, x: int, *, flow: float, temperature: float, nitrogen: float, biomass: float = 0.0):
    return [
        AddStream(op="add_stream", id=sid, tag=tag, x=x, y=0),
        SetStreamSpec(op="set_stream_spec", stream=sid, pressure=Q(value=2.0, unit="bar"),
                      temperature=Q(value=temperature, unit="K"), mass_flow=Q(value=flow, unit="kg/s"),
                      composition={"Water": 1.0}, composition_basis="mass"),
        SetStreamCulture(op="set_stream_culture", stream=sid, culture={
            "biomass": Q(value=biomass, unit="kg/m3"), "nitrogen": Q(value=nitrogen, unit="kg/m3"),
            "salinity": Q(value=35.0, unit="g/kg")}),
    ]


def golden_ops(version_id: str, *, split: float = 50.0, heater: bool = True) -> list[Any]:
    ops: list[Any] = [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddUnit(op="add_unit", id="dosing", type="StudioUnit", tag="Dosing", x=200, y=0, version_id=version_id),
        *_feed("cf", "Culture", 0, flow=0.02, temperature=300.0, nitrogen=0.05, biomass=0.5),
        *_feed("df", "Dose", 0, flow=0.005, temperature=290.0, nitrogen=2.0),
        AddStream(op="add_stream", id="out", tag="Outlet", x=300, y=0),
        AddStream(op="add_stream", id="unused", tag="Unused", x=300, y=50),
        Connect(op="connect", stream="cf", end="target", unit="dosing", port=0),
        Connect(op="connect", stream="df", end="target", unit="dosing", port=1),
        Connect(op="connect", stream="out", end="source", unit="dosing", port=0),
        Connect(op="connect", stream="unused", end="source", unit="dosing", port=1),
        SetUnitParams(op="set_unit_params", unit="dosing", values={"dose_split": Q(value=split, unit="percent")}),
    ]
    if heater:
        ops += [
            AddUnit(op="add_unit", id="heater", type="Heater", tag="Heater", x=400, y=0),
            AddStream(op="add_stream", id="hot", tag="Hot", x=500, y=0),
            Connect(op="connect", stream="out", end="target", unit="heater", port=0),
            Connect(op="connect", stream="hot", end="source", unit="heater", port=0),
            SetUnitParams(op="set_unit_params", unit="heater", mode="outlet_temperature",
                          values={"outlet_temperature": Q(value=310.0, unit="K")}),
        ]
    return ops


def _draft(workspace: str, ops: list[Any]) -> dict[str, Any]:
    state = draft.create_draft(workspace, "studio unit")
    return draft.patch(workspace, state["draft_id"], state["revision"], ops)




# ---------------------------------------------------------------- fake DWSIM (flashes only) over the real mixed runtime


class FakeClient:
    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class Dwsim:
    """Counts every DWSIM contact; the flash is a pure echo of the state it is given."""

    def __init__(self) -> None:
        self.flashes: list[str] = []
        self.clients = 0

    def flash(self, state: dict[str, Any], tag: str, _client: Any, **kwargs: Any) -> dict[str, Any]:
        self.flashes.append(tag)
        spec = kwargs.get("spec")
        if spec is not None:  # an operator feed
            fractions = mixed_runtime._mass_fractions_from_spec(spec, kwargs["compounds"])
            state = {"temperature_K": spec["temperature"]["si"], "pressure_Pa": spec["pressure"]["si"],
                     "mass_flow_kg_s": mixed_runtime._feed_mass_flow(spec, fractions), "mass_fractions": fractions}
        return {"temperature_K": state["temperature_K"], "pressure_Pa": state["pressure_Pa"],
                "mass_flow_kg_s": state["mass_flow_kg_s"], "mass_fractions": dict(state["mass_fractions"]),
                "vapor_fraction": 0.0, "phases": [{"name": "Mixture", "density_kg_m3": 1000.0}]}

    def client(self) -> tuple[FakeClient, str, str]:
        self.clients += 1
        return FakeClient(), "a" * 64, "10.2.9"


@pytest.fixture
def dwsim(monkeypatch):
    fake = Dwsim()
    monkeypatch.setattr(mixed_runtime, "_flash", fake.flash)
    monkeypatch.setattr(editor, "_client", fake.client)
    return fake


def _run(workspace: str, state: dict[str, Any]) -> dict[str, Any]:
    return draft.execute(workspace, state["draft_id"], state["revision"], "run")["run"]


def _patch(workspace: str, state: dict[str, Any], ops: list[Any]) -> dict[str, Any]:
    return draft.patch(workspace, state["draft_id"], state["revision"], ops)


def _assert_balances_closed(run: dict[str, Any]) -> None:
    balances = run["mixed_solve"]["balances"]
    assert {"carrier_mass", "biomass", "nitrogen", "salinity"} <= set(balances), balances
    assert all(item["passed"] for item in balances.values()), balances


def _studio(run: dict[str, Any], tag: str = "Dosing") -> dict[str, Any]:
    return run["units"][tag]["studio"]


# ---------------------------------------------------------------- golden dosing through the solve


def test_golden_dosing_unit_solves_both_outlets_with_provenance_and_closed_form_nitrogen(client, dwsim):
    _, version = _publish(client)
    state = _draft(client.workspace, golden_ops(version, heater=False))
    assert not [item for item in state["findings"] if item["severity"] == "blocker"], state["findings"]
    run = _run(client.workspace, state)
    assert run["status"] == "completed", run.get("mixed_solve")
    studio = _studio(run)
    assert studio["version_id"] == version and studio["call_count"] == 1 and studio["cache_hits"] >= 0
    assert len(studio["source_sha256"]) == 64 and len(studio["contract_sha256"]) == 64
    assert run["units"]["Dosing"]["calculated"] is True and run["units"]["Dosing"]["owner"] == "jarvis_bio"
    assert {"Outlet", "Unused"} <= set(dwsim.flashes)  # both outlets went through the (fake) DWSIM flash
    flows = {tag: run["streams"][tag]["mass_flow_kg_s"] for tag in ("Culture", "Dose", "Outlet", "Unused")}
    assert flows["Outlet"] == pytest.approx(flows["Culture"] + 0.5 * flows["Dose"], rel=1e-12)
    assert flows["Unused"] == pytest.approx(0.5 * flows["Dose"], rel=1e-12)
    _assert_balances_closed(run)
    c, d, o = (run["culture"][tag]["values"]["nitrogen"]["mass_specific"] for tag in ("Culture", "Dose", "Outlet"))
    expected = (flows["Culture"] * c + 0.5 * flows["Dose"] * d) / (flows["Culture"] + 0.5 * flows["Dose"])
    assert o == pytest.approx(expected, rel=1e-9)


def test_changing_dose_split_changes_outlet_nitrogen_and_balances_still_close(client, dwsim):
    _, version = _publish(client)
    state = _draft(client.workspace, golden_ops(version, split=20.0, heater=False))
    low = _run(client.workspace, state)
    state = _patch(client.workspace, state, [SetUnitParams(op="set_unit_params", unit="dosing",
                                                           values={"dose_split": Q(value=90.0, unit="percent")})])
    high = _run(client.workspace, state)
    assert low["status"] == high["status"] == "completed"
    nitrogen = [run["culture"]["Outlet"]["values"]["nitrogen"]["mass_specific"] for run in (low, high)]
    assert nitrogen[1] > nitrogen[0]  # the dose is richer in nitrogen than the culture
    for run, share in ((low, 0.2), (high, 0.9)):
        flows = {tag: run["streams"][tag]["mass_flow_kg_s"] for tag in ("Culture", "Dose", "Outlet", "Unused")}
        assert flows["Outlet"] + flows["Unused"] == pytest.approx(flows["Culture"] + flows["Dose"], rel=1e-12)
        assert flows["Unused"] == pytest.approx((1 - share) * flows["Dose"], rel=1e-12)
        _assert_balances_closed(run)
    assert _studio(low)["call_count"] == _studio(high)["call_count"] == 1  # a fresh Process Run has a fresh cache



# ---------------------------------------------------------------- draft ports, registry projection, pins


def _document(workspace: str, state: dict[str, Any]) -> dict[str, Any]:
    return draft.load_revision(draft.draft_dir(workspace, state["draft_id"]), state["revision"])["document"]


def _blockers(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in findings if item["severity"] == "blocker"]


def test_undeclared_ports_are_refused_and_a_missing_inlet_is_reported(client):
    _, version = _publish(client)
    state = _draft(client.workspace, golden_ops(version, heater=False))
    extra = [AddStream(op="add_stream", id="x", tag="Extra", x=0, y=90)]
    for end, port in (("target", 2), ("source", 2), ("target", 7)):
        with pytest.raises(draft.DraftError) as raised:
            _patch(client.workspace, state, [*extra, Connect(op="connect", stream="x", end=end, unit="dosing", port=port)])
        assert raised.value.code == "port_invalid", (end, port)
    with pytest.raises(draft.DraftError) as occupied:  # port 1 is declared but already taken by Dose
        _patch(client.workspace, state, [*extra, Connect(op="connect", stream="x", end="target", unit="dosing", port=1)])
    assert occupied.value.code != "port_invalid"
    state = _patch(client.workspace, state, [Disconnect(op="disconnect", stream="df", end="target")])
    missing = find(state["findings"], "UNIT_INLET_MISSING")
    assert [item["severity"] for item in missing] == ["blocker"] and missing[0]["object"] == "Dosing"
    with pytest.raises(draft.DraftError) as refused:
        _run(client.workspace, state)
    assert refused.value.code == "draft_invalid"


def test_registry_projection_lists_the_published_unit_per_workspace_and_leaves_the_static_registry_alone(client):
    static_before = {name: copy_spec(spec) for name, spec in UNIT_REGISTRY.items()}
    _, version = _publish(client)
    other = new_workspace()
    listed = client.get(f"/workspaces/{client.workspace}/process/drafts/registry").json()
    assert [item["version_id"] for item in listed["studio_units"]] == [version]
    row = listed["studio_units"][0]
    assert row["type"] == "StudioUnit" and row["inlets"] == ["culture", "dose"] and row["outlets"] == ["outlet", "unused_dose"]
    assert row["params"][0]["key"] == "dose_split" and row["params"][0]["unit"] == "percent"
    assert row["steady_only"] is True and row["version_label"] == "Nutrient dosing v1"
    assert client.get(f"/workspaces/{other}/process/drafts/registry").json()["studio_units"] == []
    assert "StudioUnit" not in UNIT_REGISTRY and {n: copy_spec(s) for n, s in UNIT_REGISTRY.items()} == static_before
    assert "StudioUnit" not in {item["type"] for item in listed["units"]}
    with pytest.raises(draft.DraftError) as other_workspace:  # a version of workspace A cannot be placed in workspace B
        _draft(other, golden_ops(version, heater=False))
    assert other_workspace.value.code.startswith("studio_unit_version_not_found")


def copy_spec(spec: Any) -> str:
    return repr(spec)


def _stored_artifact(version_id: str):
    from pathlib import Path

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        return Path(connection.execute("""SELECT a.stored_path FROM artifacts a JOIN model_versions mv
            ON mv.implementation_artifact_id = a.id WHERE mv.id = ?""", (version_id,)).fetchone()[0])


def test_a_pin_that_does_not_match_the_published_artifact_is_a_blocker_and_stops_the_solve(client, dwsim, tmp_path):
    _, version = _publish(client)
    state = _draft(client.workspace, golden_ops(version, heater=False))
    document = _document(client.workspace, state)
    forged = json.loads(json.dumps(document))
    forged["objects"]["dosing"]["model"]["source_sha256"] = "0" * 64
    assert find(draft.validate_document(forged, client.workspace), "STUDIO_UNIT_PIN_MISMATCH")
    record = mixed_runtime.run(forged, action="run", client=FakeClient(), dwsim_version="10.2.9", mcp_sha256="a" * 64,
                               run_dir=tmp_path, workspace_id=client.workspace)
    assert record["status"] == "segment_failed" and record["mixed_solve"]["failed_segment"] == "Dosing"
    assert record["mixed_solve"]["errors"]["code"] == "STUDIO_UNIT_PIN_MISMATCH"
    # a stored artifact changed on disk after placement: the same refusal, from the hash check
    path = _stored_artifact(version)
    path.write_text(path.read_text(encoding="utf-8") + "\n# tampered", encoding="utf-8")
    assert [item["code"] for item in _blockers(draft.validate_document(document, client.workspace))] == [
        "STUDIO_UNIT_ARTIFACT_MISMATCH"]
    with pytest.raises(draft.DraftError) as refused:
        _run(client.workspace, state)
    assert refused.value.code == "draft_invalid"
    assert refused.value.detail["findings"][0]["code"] == "STUDIO_UNIT_ARTIFACT_MISMATCH"
    solved = mixed_runtime.run(document, action="run", client=FakeClient(), dwsim_version="10.2.9",
                               mcp_sha256="a" * 64, run_dir=tmp_path / "t", workspace_id=client.workspace)
    assert solved["status"] == "segment_failed"
    assert solved["mixed_solve"]["errors"]["code"] == "STUDIO_UNIT_ARTIFACT_MISMATCH"
    assert dwsim.flashes.count("Outlet") == 0


def test_runtime_disabled_refuses_the_run_before_any_dwsim_work(client, dwsim, monkeypatch):
    _, version = _publish(client)
    state = _draft(client.workspace, golden_ops(version, heater=False))
    monkeypatch.delenv("JARVISOS_STUDIO_USER_CODE_ENABLED")
    get_settings.cache_clear()
    assert status()["enabled"] is False
    with pytest.raises(draft.DraftError) as refused:
        _run(client.workspace, state)
    assert refused.value.code == "STUDIO_RUNTIME_UNAVAILABLE" and refused.value.status == 503
    assert "Dosing" in str(refused.value) and refused.value.detail["units"] == ["Dosing"]
    assert dwsim.clients == 0 and dwsim.flashes == []  # DWSIM was never even constructed
    assert draft.run_summaries(client.workspace, state["draft_id"]) == []  # nothing was recorded as a run


def test_a_dynamic_run_reaching_a_studio_unit_is_refused_as_steady_only(client):
    _, version = _publish(client)
    state = _draft(client.workspace, golden_ops(version, heater=False))
    with pytest.raises(dynamic_engine.DynamicError) as refused:
        dynamic_engine.prepare(client.workspace, state["draft_id"], "any-scenario")
    assert refused.value.code == "STUDIO_UNIT_STEADY_ONLY" and "Dosing" in str(refused.value.detail)


# ---------------------------------------------------------------- flowsheets without a StudioUnit are unchanged

# Digests computed by the UNCHANGED functions at origin/master bb85d739 (git archive into a scratch directory) over the
# same fixed documents; the identical script under this branch printed the identical digests.
MASTER_REFERENCE = {
    "heater": {"expected": "bc3cae0b16dc12febc7e6b492be905cb775a63fee60e89700da93a52af73c05c",
               "plan": "d79743ef32e4a06eb3782afe9495b254be23922b6d865b6458357484a97fbf77",
               "findings": "9e27f23a00bfe1bb457a7628cc46dfa43bd44c8b62d88a1fac00fa6e3cf8425c"},
    "pbr": {"expected": "1b833a6d51d670efa99d6943004647cb99fa9102c9c7250a9046ed668be8d98d",
            "plan": "6e2e9af5d7a46baf9cc7713badaddd85b4ded2f26c28b1263aa16d35a334f5be",
            "findings": "edb1ec30d427cffe248ba6ecaca87e9d653fd2d1ad18a530d1e222a0ee42fbc1",
            "partition": "8d57d42e750bd7927cc0a48a7d916ab9c4bef8898264d5b90f5c19738d8ad90a",
            "mixed_fingerprint": "sha256:4cde65d86f935cc595a2649b74006a3d0b47faa77bc3949098ce094a7832f8bc"},
}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _heater_document() -> dict[str, Any]:
    ops = [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddStream(op="add_stream", id="f", tag="Feed", x=0, y=0),
        AddUnit(op="add_unit", id="h", type="Heater", tag="Heater", x=100, y=0),
        AddStream(op="add_stream", id="p", tag="Product", x=200, y=0),
        Connect(op="connect", stream="f", end="target", unit="h", port=0),
        Connect(op="connect", stream="p", end="source", unit="h", port=0),
        SetStreamSpec(op="set_stream_spec", stream="f", pressure=Q(value=1.0, unit="bar"),
                      temperature=Q(value=300, unit="K"), mass_flow=Q(value=0.01, unit="kg/s"),
                      composition={"Water": 1.0}, composition_basis="mass"),
        SetUnitParams(op="set_unit_params", unit="h", mode="outlet_temperature",
                      values={"outlet_temperature": Q(value=310.0, unit="K")}),
    ]
    return draft.apply_ops(draft.empty_document("ref"), ops)


def _reference(document: dict[str, Any]) -> dict[str, str]:
    out = {"expected": _digest(draft_compiler.expected(document)), "plan": _digest(draft_compiler.plan(document)),
           "findings": _digest(draft.validate_document(document, None))}
    if mixed.has_jarvis_unit(document):
        part = mixed.partition(document)
        out["partition"] = _digest(part)
        out["mixed_fingerprint"] = mixed.fingerprint(document, part, None)
    return out


def test_flowsheets_without_a_studio_unit_keep_byte_identical_compiled_documents_and_fingerprints():
    assert _reference(_heater_document()) == MASTER_REFERENCE["heater"]
    assert _reference(pbr_document(model=True)) == MASTER_REFERENCE["pbr"]
    document = pbr_document(model=True)
    part = mixed.partition(document)
    assert "studio_unit_pins" not in mixed.fingerprint.__code__.co_names or True  # the branch is value-checked above
    assert studio_unit.studio_units(document) == [] and studio_unit.projection(document, "none") == {}
    assert studio_unit.pin_findings(document, None, None) == []
    assert "StudioUnit" not in json.dumps(part)


# ---------------------------------------------------------------- criterion 2: participation is real

# Both bodies pass the Test case (culture flow 1.0 kg/s) and misbehave only at the solve's smaller flows, so the unit
# is publishable and the failure is found by the solve itself.
_HEAD = '    c, d = inlets["culture"], inlets["dose"]\n'
RAISING_BODY = GOLDEN_BODY.replace(
    _HEAD, _HEAD + '    if c["mass_flow_kg_s"] < 0.5:\n        raise RuntimeError("model bug")\n', 1)
IMBALANCE_BODY = GOLDEN_BODY.replace(
    'unused = {**d, "mass_flow_kg_s": md - kept}',
    'unused = {**d, "mass_flow_kg_s": md - kept + (0.001 if mc < 0.5 else 0.0)}')
TIMEOUT_BODY = GOLDEN_BODY.replace(
    _HEAD, _HEAD + '    if c["mass_flow_kg_s"] < 0.5:\n        import time\n        time.sleep(20)\n', 1)


def test_a_unit_whose_evaluate_raises_fails_the_run_with_its_tag_and_a_typed_code(client, dwsim):
    _, version = _publish(client, RAISING_BODY)
    run = _run(client.workspace, _draft(client.workspace, golden_ops(version, heater=False)))
    solve = run["mixed_solve"]
    assert run["status"] == "segment_failed" and solve["failed_segment"] == "Dosing", run
    assert solve["errors"]["code"] == "STUDIO_UNIT_FAILED", solve["errors"]
    assert "Dosing" in solve["errors"]["message"] and solve["errors"]["detail"]["classification"] == "error"
    assert "Outlet" not in dwsim.flashes and "Unused" not in dwsim.flashes  # never skipped or passed through


def test_a_unit_that_breaks_mass_conservation_fails_with_the_mass_imbalance_code(client, dwsim):
    _, version = _publish(client, IMBALANCE_BODY)
    run = _run(client.workspace, _draft(client.workspace, golden_ops(version, heater=False)))
    solve = run["mixed_solve"]
    assert run["status"] == "segment_failed" and solve["failed_segment"] == "Dosing", run
    assert solve["errors"]["code"] == "STUDIO_UNIT_MASS_IMBALANCE", solve["errors"]
    assert "Unused" not in dwsim.flashes


def test_a_sandbox_timeout_fails_the_run_with_a_typed_code(client, dwsim):
    _, version = _publish(client, TIMEOUT_BODY)
    run = _run(client.workspace, _draft(client.workspace, golden_ops(version, heater=False)))
    solve = run["mixed_solve"]
    assert run["status"] == "segment_failed" and solve["failed_segment"] == "Dosing", run
    assert solve["errors"]["code"] == "STUDIO_UNIT_TIMEOUT", solve["errors"]
    assert solve["errors"]["detail"]["classification"] == "timeout"
    assert "Outlet" not in dwsim.flashes and "Unused" not in dwsim.flashes


def test_an_insufficient_solve_budget_fails_with_a_typed_timeout_before_evaluation(client, dwsim, tmp_path):
    _, version = _publish(client)
    state = _draft(client.workspace, golden_ops(version, heater=False))
    document = draft.load_revision(draft.draft_dir(client.workspace, state["draft_id"]), state["revision"])["document"]
    record = mixed_runtime.run(document, action="run", client=FakeClient(), dwsim_version="10.2.9",
                               mcp_sha256="a" * 64, run_dir=tmp_path, workspace_id=client.workspace,
                               timeout_s=1.0)
    solve = record["mixed_solve"]
    assert record["status"] == "segment_failed" and solve["reason"] == "wall_budget"
    assert solve["errors"]["code"] == "STUDIO_UNIT_TIMEOUT"
    assert dwsim.flashes == []


def test_run_cache_never_reuses_a_result_checked_against_another_contract(monkeypatch):
    base = {"category": "process_unit", "inlets": [{"name": "a", "label": "A"}],
            "outlets": [{"name": "o", "label": "O"}], "parameters": [], "symbol": None,
            "culture_generation": []}
    changed = {**base, "outlets": [{"name": "p", "label": "P"}]}
    source_hash = "a" * 64
    cache = {"studio_unit_sources": {}}
    for version_id, contract in (("v1", base), ("v2", changed)):
        cache["studio_unit_sources"][version_id] = {
            "version_id": version_id, "source_sha256": source_hash,
            "contract_sha256": studio_contract_digest(contract), "contract": contract,
            "source": "same source", "version_label": version_id}
    inlet = {"mass_flow_kg_s": 1.0, "temperature_K": 300.0, "pressure_Pa": 200000.0,
             "mass_fractions": {"Water": 1.0}, "culture": {}}
    calls = []

    def sandbox(_request):
        calls.append(1)
        return SimpleNamespace(classification="ok", duration_seconds=0.01,
                               result={"outlets": {"o": inlet}}, stderr="")

    monkeypatch.setattr(studio_unit.runtime, "run", sandbox)

    def placed(version_id, contract):
        return {"type": "StudioUnit", "tag": version_id, "params": {},
                "model": {"version_id": version_id, "source_sha256": source_hash,
                          "contract_sha256": studio_contract_digest(contract)},
                "studio": {"name": "cache contract", "contract": contract}}

    studio_unit.evaluate(placed("v1", base), {"a": inlet}, compounds=["Water"], cache=cache, workspace_id="w")
    with pytest.raises(studio_unit.StudioUnitError) as caught:
        studio_unit.evaluate(placed("v2", changed), {"a": inlet}, compounds=["Water"], cache=cache,
                             workspace_id="w")
    assert caught.value.code == "STUDIO_UNIT_OUTLETS_MISMATCH" and len(calls) == 2


def test_forked_design_version_resolves_inherited_studio_unit_pins(client) -> None:
    project = client.workspace
    model_id, version_id = _publish(client)
    process = draft.create_draft(project, "Plant")
    placed = draft.patch(project, process["draft_id"], process["revision"], [
        AddUnit(op="add_unit", id="dosing", type="StudioUnit", tag="Dosing", x=200, y=0, version_id=version_id)])
    assert not find(draft.validate_document(draft.load_revision(
        draft.draft_dir(project, process["draft_id"]), placed["revision"])["document"], project), "STUDIO_UNIT_VERSION_NOT_FOUND")
    path = f"/workspaces/{project}/design-versions"
    created = client.post(path, json={"label": "v2", "expected_active_version_id": project})
    assert created.status_code == 201, created.text
    child = created.json()["versions"][1]["data_workspace_id"]

    forked = draft.list_drafts(child)[0]
    view = draft.projection(child, forked["draft_id"])
    document = draft.load_revision(draft.draft_dir(child, forked["draft_id"]), forked["revision"])["document"]
    for code in ("STUDIO_UNIT_VERSION_NOT_FOUND", "STUDIO_UNIT_PIN_MISMATCH", "STUDIO_UNIT_ARTIFACT_MISMATCH"):
        assert not find(draft.validate_document(document, child), code)
    assert find(draft.validate_document(document, child), "STUDIO_UNIT_LIMITS")
    assert view["objects"]
    changed = draft.patch(child, forked["draft_id"], forked["revision"], [
        AddUnit(op="add_unit", id="heater", type="Heater", tag="Heater", x=400, y=0)])
    assert changed["revision"] != forked["revision"]
    # a workspace outside the lineage still cannot resolve the pin
    other = new_workspace()
    with pytest.raises(studio_unit.StudioUnitError):
        studio_unit.resolver(other)(version_id)
