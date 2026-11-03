"""Focused contracts for Jarvis-owned Process culture streams (spec 167)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.main import app
from app.modules.process_stack import culture, draft, draft_compiler
from app.modules.process_stack.draft_models import (
    QUANTITY_UNITS,
    UNIT_REGISTRY,
    AddStream,
    AddUnit,
    Connect,
    SetStreamCulture,
)
from app.modules.workspace_actions.models import ActionOrigin, ActionRequest, SurfaceRef
from app.modules.workspace_actions.service import apply as apply_action
from app.modules.workspace_actions.service import submit, surface_brief

Q = lambda value, unit: {"value": value, "unit": unit}  # noqa: E731


def _culture(**values: tuple[float, str]) -> dict[str, dict[str, Any]]:
    return {key: draft._si(draft.DraftQuantity(value=value, unit=unit), culture.FIELD_KINDS[key], key)
            for key, (value, unit) in values.items()}


def _stream(object_id: str, tag: str, *, source: str | None = None, target: str | None = None,
            port: int = 0, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": object_id, "kind": "stream", "type": "MaterialStream", "tag": tag, "x": 0, "y": 0,
            "source": {"unit": source, "port": port} if source else None,
            "target": {"unit": target, "port": port} if target else None, "spec": spec or {}}


def _unit(object_id: str, tag: str, typ: str) -> dict[str, Any]:
    spec = UNIT_REGISTRY[typ]
    return {"id": object_id, "kind": "unit", "type": typ, "tag": tag, "x": 0, "y": 0,
            "mode": spec.default_mode, "params": {}, "options": {}, "reactions": []}


def _reported(flow: float, density: float = 1000.0, vapor: float = 0.0) -> dict[str, Any]:
    return {"mass_flow_kg_s": flow, "vapor_fraction": vapor,
            "reported": {"phases": [{"name": "Mixture", "density_kg_m3": density}]}}


def test_culture_units_are_explicit_and_pH_is_a_scalar() -> None:
    assert QUANTITY_UNITS["mass_concentration"] == ("kg/m3", ("kg/m3", "g/L", "mg/L"))
    assert QUANTITY_UNITS["molar_concentration"] == ("mol/m3", ("mol/m3", "mmol/L", "kmol/m3"))
    assert draft._si(draft.DraftQuantity(value=1.5, unit="g/L"), "mass_concentration", "biomass")["si"] == 1.5
    assert draft._si(draft.DraftQuantity(value=1500, unit="mg/L"), "mass_concentration", "biomass")["si"] == 1.5
    assert draft._si(draft.DraftQuantity(value=4.2, unit="mmol/L"), "molar_concentration", "dic")["si"] == 4.2
    assert draft.convert_si(1.5, "mass_concentration", "mg/L") == 1500
    assert draft._si(draft.DraftQuantity(value=8.1, unit="pH"), "ph", "ph")["si"] == 8.1
    with pytest.raises(draft.DraftError, match="not offered"):
        draft._si(draft.DraftQuantity(value=8.1, unit="degC"), "ph", "ph")


def test_culture_operation_is_cas_reversible_and_outside_dwsim_materialization() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "Culture 167", "slug": f"culture-167-{uuid4().hex[:8]}"}).json()
        root = f"/workspaces/{workspace['id']}/process/drafts"
        view = client.post(root, json={"name": "culture"}).json()
        base_revision = client.post(f"{root}/{view['draft_id']}/patch", json={
            "expected_revision": view["revision"], "ops": [{"op": "add_stream", "id": "feed", "tag": "Feed", "x": 0, "y": 0}],
        }).json()
        old_document = draft.load_revision(draft.draft_dir(workspace["id"], view["draft_id"]), base_revision["revision"])["document"]
        view = client.post(f"{root}/{view['draft_id']}/patch", json={
            "expected_revision": base_revision["revision"], "ops": [
                {"op": "set_stream_culture", "stream": "feed", "culture": {"biomass": Q(1, "g/L"), "salinity": Q(35, "g/kg")}},
            ],
        }).json()
        assert view["objects"][0]["spec"]["culture"]["biomass"] == {"si": 1.0, "value": 1.0, "unit": "g/L"}
        stale = client.post(f"{root}/{view['draft_id']}/patch", json={
            "expected_revision": base_revision["revision"],
            "ops": [{"op": "set_stream_culture", "stream": "feed", "culture": None}],
        })
        assert stale.status_code == 409
        before = draft.load_revision(draft.draft_dir(workspace["id"], view["draft_id"]), view["revision"])["document"]
        changed = draft.apply_ops(before, [SetStreamCulture(
            op="set_stream_culture", stream="feed", culture={"biomass": draft.DraftQuantity(value=2, unit="g/L"),
                                                                "salinity": draft.DraftQuantity(value=35, unit="g/kg")})])
        assert draft_compiler.expected(before) == draft_compiler.expected(changed)
        assert draft_compiler.compare(draft_compiler.expected(before), draft_compiler.expected(changed)) == []
        assert draft_compiler.fingerprint(draft_compiler.expected(before), dwsim_version="10.2.9", mcp_sha256="a" * 64) == draft_compiler.fingerprint(draft_compiler.expected(changed), dwsim_version="10.2.9", mcp_sha256="a" * 64)
        assert draft_compiler.result_fingerprint(before, dwsim_version="10.2.9", mcp_sha256="a" * 64) != draft_compiler.result_fingerprint(changed, dwsim_version="10.2.9", mcp_sha256="a" * 64)
        result_fingerprint = draft_compiler.result_fingerprint(before, dwsim_version="10.2.9", mcp_sha256="a" * 64)
        solved = {"action": "run", "status": "completed", "run_id": "run", "draft_revision": view["revision"],
                  "result_fingerprint": result_fingerprint, "dwsim_version": "10.2.9", "mcp_sha256": "a" * 64,
                  "materialization_fingerprint": "sha256:unchanged"}
        stale_state = draft.results_state({"revision": "4:" + "f" * 16, "seq": 4}, [solved], changed, edits_since=1)
        assert stale_state["state"] == "stale"
        assert stale_state["materialization_fingerprint"] == "sha256:unchanged"
        restored = client.post(f"{root}/{view['draft_id']}/restore", json={
        "expected_revision": view["revision"], "source_revision": base_revision["revision"],
        })
        assert restored.status_code == 200
        assert "culture" not in old_document["objects"]["feed"]["spec"]
        assert restored.json()["objects"][0]["spec"].get("culture") is None


def test_registry_owners_and_rules_cover_all_existing_units() -> None:
    projection = draft.registry_projection()
    dwsim_units = [item for item in projection["units"] if item["owner"] == "dwsim"]
    assert len(dwsim_units) == 12
    assert len(projection["units"]) == 15
    assert {item["type"] for item in projection["units"] if item["owner"] == "jarvis_bio"} == {
        "SpecifiedSeparator", "PhotobioreactorT1", "HoldupTank",
    }
    assert {item["owner"] for item in projection["units"]} == {"dwsim", "jarvis_bio"}
    assert {item["type"]: item["culture_rule"] for item in projection["units"]}["Mixer"] == "mixer"
    assert {item["type"]: item["culture_rule"] for item in projection["units"]}["Splitter"] == "splitter"
    assert {item["type"]: item["culture_rule"] for item in projection["units"]}["Flash"] == "refuse"


def test_carrier_findings_use_mass_fraction_for_mole_basis_and_required_fields() -> None:
    feed = _stream("f", "Culture", target="m", spec={"composition": {"Water": 0.55, "Methanol": 0.45},
        "composition_basis": "mole", "culture": _culture(biomass=(1, "g/L"), salinity=(35, "g/kg"))})
    mixer = _unit("m", "M1", "Mixer")
    document = {"objects": {"f": feed, "m": mixer, "p": _stream("p", "Product", source="m")},
                "compounds": ["Water", "Methanol"], "property_package": "NRTL", "reactions": {}}
    codes = {item["code"] for item in culture.culture_findings(document)}
    assert "CULTURE_CARRIER_NOT_AQUEOUS" in codes  # 0.55 mole water is only 0.408 water by mass
    assert "CULTURE_CARRIER_IMPURE" in codes
    assert "CULTURE_MIXED_WITH_UNSPECIFIED" not in codes
    feed["spec"]["culture"].pop("biomass")
    assert "CULTURE_FIELD_REQUIRED" in {item["code"] for item in culture.culture_findings(document)}


def test_unit_refusals_and_culture_free_behavior() -> None:
    findings_by_type = {}
    for typ in ("Flash", "DistillationColumn", "PFR"):
        unit = _unit("u", "U1", typ)
        doc = {"objects": {"f": _stream("f", "Feed", target="u", spec={"culture": _culture(biomass=(1, "g/L"), salinity=(35, "g/kg"))}),
                            "u": unit, "p": _stream("p", "Product", source="u")}, "compounds": [], "property_package": None}
        findings_by_type[typ] = {item["code"] for item in culture.culture_findings(doc)}
    assert all("CULTURE_UNIT_UNSUPPORTED" in codes for codes in findings_by_type.values())
    recycle = {"objects": {
        "feed": _stream("feed", "Feed", target="h", spec={"culture": _culture(biomass=(1, "g/L"), salinity=(35, "g/kg"))}),
        "h": _unit("h", "H1", "Heater"), "mid": _stream("mid", "Mid", source="h", target="r"),
        "r": _unit("r", "R1", "Recycle"), "back": _stream("back", "Back", source="r", target="h"),
    }}
    assert "CULTURE_RECYCLE_UNSUPPORTED" not in {item["code"] for item in culture.culture_findings(recycle)}
    assert "CULTURE_UNIT_UNSUPPORTED" not in {item["code"] for item in culture.culture_findings(recycle)}
    no_culture = {"objects": {"f": _stream("f", "Feed")}}
    assert culture.culture_findings(no_culture) == []


@pytest.mark.parametrize("unit_type", ["Heater", "Cooler", "Pump", "Valve", "Splitter"])
def test_single_inlet_pass_through_and_splitter_copy(unit_type: str) -> None:
    outputs = ["p1", "p2"] if unit_type == "Splitter" else ["p1"]
    objects = {"f": _stream("f", "Feed", target="u", spec={"culture": _culture(
        biomass=(1, "g/L"), nitrogen=(2, "mg/L"), oxygen=(0, "mg/L"), dic=(3, "mmol/L"),
        ph=(8.1, "pH"), salinity=(35, "g/kg"))}), "u": _unit("u", "U1", unit_type)}
    objects.update({key: _stream(key, tag, source="u", port=index) for index, (key, tag) in enumerate(zip(outputs, outputs, strict=True))})
    document = {"objects": objects}
    solved = {"Feed": _reported(1.0), **{tag: _reported(1.0 / len(outputs)) for tag in outputs}}
    result, findings = culture.propagate(document, solved)
    assert not findings
    assert result["p1"]["status"] == "completed"
    assert result["p1"]["values"]["biomass"]["display"] == {"value": 1.0, "unit": "kg/m3"}
    assert result["p1"]["values"]["oxygen"]["display"]["value"] == 0.0
    if len(outputs) == 2:
        assert result["p2"]["values"]["biomass"]["mass_specific"] == result["p1"]["values"]["biomass"]["mass_specific"]


def test_mixer_weighting_unknown_fields_ph_and_phase_density_failures() -> None:
    culture_a = _culture(biomass=(1, "g/L"), oxygen=(0, "mg/L"), dic=(4.2, "mmol/L"), ph=(8.1, "pH"), salinity=(35, "g/kg"))
    objects = {
        "a": _stream("a", "A", target="m", port=0, spec={"culture": culture_a}),
        "b": _stream("b", "Makeup", target="m", port=1), "m": _unit("m", "M1", "Mixer"),
        "p": _stream("p", "P", source="m"),
    }
    document = {"objects": objects}
    solved = {"A": _reported(1.0, 1000), "Makeup": _reported(3.0, 1000), "P": _reported(4.0, 900)}
    result, findings = culture.propagate(document, solved)
    assert any(item["code"] == "CULTURE_MIXED_WITH_UNSPECIFIED" for item in culture.culture_findings(document))
    assert result["P"]["values"]["biomass"]["mass_specific"] == pytest.approx(0.00025)
    assert result["P"]["values"]["biomass"]["display"]["value"] == pytest.approx(0.225)
    assert result["P"]["values"]["nitrogen"]["display"] is None
    assert "not specified on feed A" in result["P"]["values"]["nitrogen"]["reason"]
    assert result["P"]["values"]["ph"]["display"] is None
    assert result["P"]["pH_reason"] == culture.PH_REASON
    assert result["P"]["unit_balances"]["oxygen"]["unit"] == "kg/s"
    assert result["P"]["unit_balances"]["dic"]["unit"] == "mol/s"
    assert result["P"]["unit_balances"]["salinity"]["unit"] == "kg/s"
    assert result["P"]["unit_balances"]["salinity"]["in"] == pytest.approx(0.035)
    assert result["P"]["unit_balances"]["salinity"]["out"] == pytest.approx(0.035)
    assert not findings
    agreeing = {"objects": {
        "a": _stream("a", "A", target="m", port=0, spec={"culture": _culture(
            biomass=(1, "g/L"), ph=(8.1, "pH"), salinity=(35, "g/kg"))}),
        "b": _stream("b", "B", target="m", port=1, spec={"culture": _culture(
            biomass=(1, "g/L"), ph=(8.105, "pH"), salinity=(35, "g/kg"))}),
        "m": _unit("m", "M1", "Mixer"), "p": _stream("p", "P", source="m"),
    }}
    agreeing_result, _ = culture.propagate(agreeing, {"A": _reported(1), "B": _reported(1), "P": _reported(2)})
    assert agreeing_result["P"]["values"]["ph"]["display"]["value"] == 8.1
    assert agreeing_result["P"]["pH_reason"] == culture.PH_REASON
    zero, zero_findings = culture.propagate(document, {"A": _reported(0), "Makeup": _reported(0), "P": _reported(0)})
    assert zero["P"]["status"] == "failed"
    assert zero_findings[-1]["code"] == "CULTURE_MIXER_FLOW_INVALID"
    for density in (0.0, float("nan")):
        no_density, density_findings = culture.propagate(document, {**solved, "P": _reported(4, density)})
        assert no_density["P"]["status"] == "failed"
        assert density_findings[-1]["code"] == "CULTURE_DENSITY_UNAVAILABLE"
    vapor, vapor_findings = culture.propagate(document, {**solved, "P": _reported(4, 900, 2e-6)})
    assert vapor["P"]["status"] == "failed"
    assert vapor_findings[-1]["code"] == "CULTURE_PHASE_NOT_LIQUID"
    vapor_feed, vapor_feed_findings = culture.propagate(document, {**solved, "A": _reported(1, 1000, 2e-6)})
    assert vapor_feed["A"]["status"] == "failed"
    assert vapor_feed["P"]["status"] == "failed"
    assert [item["code"] for item in vapor_feed_findings] == ["CULTURE_PHASE_NOT_LIQUID", "CULTURE_UPSTREAM_FAILED"]


def test_heat_exchanger_carries_each_culture_side_independently() -> None:
    hot = _culture(biomass=(1, "g/L"), salinity=(35, "g/kg"))
    cold = _culture(biomass=(2, "g/L"), salinity=(20, "g/kg"))
    objects = {
        "hot": _stream("hot", "HotIn", target="hx", port=0, spec={"culture": hot}),
        "cold": _stream("cold", "ColdIn", target="hx", port=1, spec={"culture": cold}),
        "hx": _unit("hx", "HX1", "HeatExchanger"),
        "hotout": _stream("hotout", "HotOut", source="hx", port=0),
        "coldout": _stream("coldout", "ColdOut", source="hx", port=1),
    }
    result, findings = culture.propagate({"objects": objects}, {
        "HotIn": _reported(1.0, 1000), "ColdIn": _reported(2.0, 800),
        "HotOut": _reported(1.0, 900), "ColdOut": _reported(2.0, 700),
    })
    assert not findings
    assert result["HotOut"]["values"]["biomass"]["display"]["value"] == pytest.approx(0.9)
    assert result["ColdOut"]["values"]["biomass"]["display"]["value"] == pytest.approx(1.75)
    assert result["HotOut"]["unit_balances"]["biomass"]["passed"]
    assert result["ColdOut"]["unit_balances"]["salinity"]["unit"] == "kg/s"


def test_feed_input_findings_include_liquid_and_value_bounds() -> None:
    stream = _stream("f", "Culture", target="u", spec={
        "composition": {"Water": 1.0}, "vapor_fraction": {"si": 0.01},
        "culture": {"biomass": {"si": -1, "value": -1, "unit": "kg/m3"},
                    "salinity": {"si": 301, "value": 301, "unit": "g/kg"},
                    "ph": {"si": 15, "value": 15, "unit": "pH"}},
    })
    document = {"objects": {"f": stream, "u": _unit("u", "P1", "Pump"),
                            "p": _stream("p", "Product", source="u")}, "compounds": ["Water"],
                "property_package": "NRTL"}
    codes = {item["code"] for item in culture.culture_findings(document)}
    assert {"CULTURE_FEED_NOT_LIQUID", "CULTURE_VALUE_OUT_OF_RANGE"} <= codes


def test_workspace_action_sets_single_culture_field_confirm_tier_and_brief_refuses_flash() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "Culture action", "slug": f"culture-action-{uuid4().hex[:8]}"}).json()
        wsid = workspace["id"]
        created = draft.create_draft(wsid, "Culture action")
        state = draft.patch(wsid, created["draft_id"], created["revision"], [
            AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=0),
            AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump", x=100, y=0),
            AddStream(op="add_stream", id="product", tag="Product", x=200, y=0),
            Connect(op="connect", stream="feed", end="target", unit="pump", port=0),
            Connect(op="connect", stream="product", end="source", unit="pump", port=0),
            SetStreamCulture(op="set_stream_culture", stream="feed", culture={
                "biomass": draft.DraftQuantity(value=1, unit="g/L"),
                "nitrogen": draft.DraftQuantity(value=2, unit="mg/L"),
                "salinity": draft.DraftQuantity(value=35, unit="g/kg"),
            }),
        ])
        origin = ActionOrigin(kind="local", thread_id="culture", interaction_id="167")
        request = ActionRequest.model_validate({"surface": "process", "base_revision": state["revision"],
            "draft_id": state["draft_id"], "actions": [{"op": "set_value", "target": "Feed",
            "property": "biomass", "value": {"value": 1.5, "unit": "g/L"}}]})
        proposal = submit(wsid, request, origin)
        assert proposal.state == "proposed" and proposal.tier == "confirm", proposal.reason
        applied = apply_action(wsid, proposal.action_id)
        assert applied.state == "applied"
        feed = next(item for item in draft.projection(wsid, state["draft_id"])["objects"] if item["tag"] == "Feed")
        assert feed["spec"]["culture"]["biomass"]["si"] == 1.5
        assert feed["spec"]["culture"]["nitrogen"]["si"] == 0.002
        brief = surface_brief(wsid, SurfaceRef(route_id="design-process", draft_id=state["draft_id"],
            process_selection=[{"kind": "stream", "tag": "Feed"}]))
        assert "Selected feed culture values" in brief.text and "1.5" in brief.text
        assert "cannot pass through Flash" in brief.text
        current = draft.projection(wsid, state["draft_id"])
        refusal = submit(wsid, ActionRequest.model_validate({"surface": "process", "base_revision": current["revision"],
            "draft_id": state["draft_id"], "actions": [{"op": "insert_unit_after", "type": "Flash", "after": "Pump"}]}), origin)
        assert refusal.state == "refused"
        assert "CULTURE_UNIT_UNSUPPORTED" in (refusal.reason or "")


def test_workspace_action_configures_separator_and_brief_explains_168_contract() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "Mixed action", "slug": f"mixed-action-{uuid4().hex[:8]}"}).json()
        wsid = workspace["id"]
        created = draft.create_draft(wsid, "Mixed action")
        state = draft.patch(wsid, created["draft_id"], created["revision"], [
            AddStream(op="add_stream", id="feed", tag="CultureFeed", x=0, y=0),
            AddUnit(op="add_unit", id="separator", type="SpecifiedSeparator", tag="Separator", x=100, y=0),
            AddStream(op="add_stream", id="conc", tag="Concentrate", x=200, y=-20),
            AddStream(op="add_stream", id="clar", tag="Clarified", x=200, y=20),
            Connect(op="connect", stream="feed", end="target", unit="separator", port=0),
            Connect(op="connect", stream="conc", end="source", unit="separator", port=0),
            Connect(op="connect", stream="clar", end="source", unit="separator", port=1),
            SetStreamCulture(op="set_stream_culture", stream="feed", culture={
                "biomass": draft.DraftQuantity(value=1, unit="g/L"),
                "salinity": draft.DraftQuantity(value=35, unit="g/kg"),
            }),
        ])
        origin = ActionOrigin(kind="local", thread_id="mixed", interaction_id="168")
        request = ActionRequest.model_validate({"surface": "process", "base_revision": state["revision"],
            "draft_id": state["draft_id"], "actions": [{"op": "set_value", "target": "Separator",
            "property": "concentration_factor", "value": {"value": 20, "unit": "dimensionless"}}]})
        proposal = submit(wsid, request, origin)
        assert proposal.state == "proposed" and proposal.tier == "confirm", proposal.reason
        applied = apply_action(wsid, proposal.action_id)
        assert applied.state == "applied"
        assert applied.result_revision != state["revision"]
        brief = surface_brief(wsid, SurfaceRef(route_id="design-process", draft_id=state["draft_id"],
            process_selection=[{"kind": "unit", "tag": "Separator"}]))
        assert len(brief.text) <= 6000
        assert "Separator=jarvis_bio" in brief.text
        assert "biomass recovery 90%" in brief.text
        assert "concentration factor 20" in brief.text
        assert "derived concentrate carrier split 4.5%" in brief.text
        assert "native Recycles inside mixed loops are refused" in brief.text
