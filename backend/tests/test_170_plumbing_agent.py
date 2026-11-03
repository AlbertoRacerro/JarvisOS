"""Spec 170 capability 8 (backend part): the 166 vocabulary for the Photobioreactor and the Process brief."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from app.core.paths import build_paths
from app.modules.agents.hermes import broker_mcp
from app.modules.ai.thread_service import _guard_tool_shaped_output
from app.modules.bio_models import service as bio_models
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddStream, AddUnit, Connect
from app.modules.workspace_actions.models import ActionOrigin, ActionRequest, ProcessAction, SurfaceRef
from app.modules.workspace_actions.service import apply, submit, surface_brief, undo
from tests.plumbing_170_support import make_card, new_workspace, pbr_ops, pin_of

ORIGIN = ActionOrigin(kind="local", thread_id="t-170", interaction_id="i-170")


def _origin() -> ActionOrigin:
    return ActionOrigin(kind="local", thread_id="t-170", interaction_id=f"i-{uuid4().hex[:8]}")


def _state(workspace_id: str, ops: list[Any] | None = None) -> dict[str, Any]:
    created = draft.create_draft(workspace_id, "PBR agent")
    return draft.patch(workspace_id, created["draft_id"], created["revision"], ops if ops is not None else pbr_ops())


def _request(state: dict[str, Any], *actions: dict[str, Any]) -> ActionRequest:
    return ActionRequest.model_validate({"surface": "process", "base_revision": state["revision"],
                                         "draft_id": state["draft_id"], "actions": list(actions)})


def _pbr(workspace_id: str, state: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in draft.projection(workspace_id, state["draft_id"])["objects"]
                if item["type"] == "PhotobioreactorT1")


def test_registry_actions_add_insert_and_connect_a_pbr() -> None:
    workspace_id = new_workspace()
    state = _state(workspace_id, [
        AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=0),
        AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump", x=100, y=0),
        AddStream(op="add_stream", id="out", tag="Out", x=200, y=0),
        Connect(op="connect", stream="feed", end="target", unit="pump", port=0),
        Connect(op="connect", stream="out", end="source", unit="pump", port=0)])
    added = submit(workspace_id, _request(state, {"op": "add_unit", "type": "PhotobioreactorT1", "tag": "PBR1",
                                                  "near": "Pump"}), _origin())
    assert added.state == "proposed" and added.tier == "confirm", added.reason
    assert apply(workspace_id, added.action_id).state == "applied"
    state = draft.projection(workspace_id, state["draft_id"])
    unknown = submit(workspace_id, _request(state, {"op": "insert_unit_after", "type": "Bioreactor", "after": "Pump"}),
                     _origin())
    assert unknown.state == "refused" and "PhotobioreactorT1" in (unknown.reason or "")
    # the operator's word for the unit resolves through the registry label
    inserted = submit(workspace_id, _request(state, {"op": "insert_unit_after", "type": "Photobioreactor",
                                                     "after": "Pump", "tag": "PBR2"}), _origin())
    assert inserted.state == "proposed", inserted.reason
    assert apply(workspace_id, inserted.action_id).state == "applied"
    state = draft.projection(workspace_id, state["draft_id"])
    pbr2 = next(item for item in state["objects"] if item["tag"] == "PBR2")
    assert pbr2["type"] == "PhotobioreactorT1" and pbr2["params"] == {}
    streams = {item["tag"]: item for item in state["objects"] if item["kind"] == "stream"}
    assert streams["Out"]["source"]["unit"] == pbr2["id"]
    disconnected = submit(workspace_id, _request(state, {"op": "disconnect", "stream": "S_auto1"}), _origin())
    assert disconnected.state == "proposed", disconnected.reason
    assert apply(workspace_id, disconnected.action_id).state == "applied"
    state = draft.projection(workspace_id, state["draft_id"])
    connected = submit(workspace_id, _request(state, {"op": "connect", "from": "PBR1", "from_port": "outlet",
                                                      "to": "PBR2", "to_port": "inlet"}), _origin())
    assert connected.state == "proposed", connected.reason
    refused = submit(workspace_id, _request(state, {"op": "connect", "from": "PBR1", "from_port": "outlet 2", "to": "PBR2"}),
                     _origin())
    assert refused.state == "refused" and "outlet" in (refused.reason or "")


def test_set_value_on_pbr_parameters_is_confirm_tier_with_units_validated() -> None:
    workspace_id = new_workspace()
    state = _state(workspace_id)
    ok = submit(workspace_id, _request(state, {"op": "set_value", "target": "PBR", "property": "oxygen_kla",
                                               "value": {"value": 2, "unit": "1/d"}}), _origin())
    assert ok.state == "proposed" and ok.tier == "confirm", ok.reason
    assert apply(workspace_id, ok.action_id).state == "applied"
    assert _pbr(workspace_id, state)["params"]["oxygen_kla"]["si"] == pytest.approx(2 / 86400)
    for prop, quantity in (("liquid_velocity", {"value": 3, "unit": "km/h"}), ("photoperiod", {"value": 30, "unit": "h"}),
                           ("tube_count", {"value": 1.5, "unit": "dimensionless"}),
                           ("temperature_amplitude", {"value": 500, "unit": "K"}), ("unknown_param", {"value": 1, "unit": "m"})):
        current = draft.projection(workspace_id, state["draft_id"])
        refused = submit(workspace_id, _request(current, {"op": "set_value", "target": "PBR", "property": prop,
                                                          "value": quantity}), _origin())
        assert refused.state == "refused", prop


def test_set_unit_model_resolves_by_id_or_exact_name_to_the_current_revision_and_applies_with_undo() -> None:
    workspace_id = new_workspace()
    card = make_card(workspace_id, "Synthetic Monod card")
    state = _state(workspace_id)
    by_name = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": "Synthetic Monod card"}),
                     _origin())
    assert by_name.state == "proposed" and by_name.tier == "confirm", by_name.reason
    assert by_name.summary == f"Pin model card Synthetic Monod card (revision {card['revision'][2:10]}) on PBR"
    assert by_name.changes[0].before == "none" and by_name.changes[0].after == f"Synthetic Monod card (revision {card['revision'][2:10]})"
    assert "model" not in _pbr(workspace_id, state), "a proposal changes nothing"
    applied = apply(workspace_id, by_name.action_id)
    assert applied.state == "applied" and applied.result_revision != state["revision"]
    assert _pbr(workspace_id, state)["model"] == pin_of(card)
    by_id = submit(workspace_id, _request(draft.projection(workspace_id, state["draft_id"]),
                                          {"op": "set_unit_model", "unit": "PBR", "card": card["id"]}), _origin())
    assert by_id.state == "proposed" and by_id.changes[0].before == "Synthetic Monod card (revision " + card["revision"][2:10] + ")"
    assert card["id"] not in (by_id.changes[0].before or "") and card["id"] not in (by_id.changes[0].after or "")
    assert undo(workspace_id, applied.action_id).state == "undone"
    assert "model" not in _pbr(workspace_id, state)


def test_set_unit_model_refuses_ambiguous_unknown_and_wrong_targets() -> None:
    workspace_id = new_workspace()
    first, second = make_card(workspace_id, "Twin"), make_card(workspace_id, "Twin")
    make_card(workspace_id, "Unique")
    state = _state(workspace_id, [*pbr_ops(), AddUnit(op="add_unit", id="h", type="Heater", tag="H", x=0, y=50)])
    state = draft.projection(workspace_id, state["draft_id"])
    ambiguous = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": "Twin"}), _origin())
    assert ambiguous.state == "refused" and ambiguous.reason_code == "invalid_action"
    assert first["id"] in (ambiguous.reason or "") and second["id"] in (ambiguous.reason or "")
    unknown = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": "Nothing"}), _origin())
    assert unknown.state == "refused" and "Nothing" in (unknown.reason or "")
    partial = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": "Uniq"}), _origin())
    assert partial.state == "refused", "names match exactly, never by prefix"
    phrased = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": "model card 'Unique'"}),
                     _origin())
    assert phrased.state == "proposed", "the request's own 'model card' words and quotes are not part of the name"
    phrased_partial = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": "model card Uniq"}),
                             _origin())
    assert phrased_partial.state == "refused", "the stripped name still matches exactly"
    wrong = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "H", "card": "Unique"}), _origin())
    assert wrong.state == "refused" and "only a Photobioreactor" in (wrong.reason or "")
    ghost = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "Ghost", "card": "Unique"}), _origin())
    assert ghost.state == "refused"


def test_a_card_revision_that_moves_between_proposal_and_apply_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = new_workspace()
    card = make_card(workspace_id, "Moving card")
    state = _state(workspace_id)
    proposal = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": "Moving card"}), _origin())
    assert proposal.state == "proposed"
    newer = {**card, "revision": "r-" + "e" * 16, "digest": "f" * 64}
    monkeypatch.setattr(bio_models, "list_cards", lambda _workspace_id: [newer])
    outcome = apply(workspace_id, proposal.action_id)
    assert outcome.state == "stale" and "model card changed" in (outcome.reason or "")
    assert "model" not in _pbr(workspace_id, state)


def test_wire_surfaces_know_set_unit_model() -> None:
    action = {"op": "set_unit_model", "unit": "PBR", "card": "Some card"}
    assert ActionRequest.model_validate({"surface": "process", "base_revision": "r", "actions": [action]})
    with pytest.raises(ValueError):
        ActionRequest.model_validate({"surface": "process", "base_revision": "r",
                                      "actions": [{**action, "card": ""}]})
    with pytest.raises(ValueError):
        ActionRequest.model_validate({"surface": "process", "base_revision": "r",
                                      "actions": [{**action, "extra": 1}]})
    variants = broker_mcp._PROCESS_ACT_TOOL["inputSchema"]["properties"]["actions"]["items"]["oneOf"]
    schema = next(item for item in variants if item["properties"]["op"] == {"const": "set_unit_model"})
    assert schema["required"] == ["op", "unit", "card"] and schema["additionalProperties"] is False
    assert ProcessAction is not None
    visible, details = _guard_tool_shaped_output(json.dumps({"op": "set_unit_model", "unit": "PBR", "card": "x"}))
    assert details is not None and "set_unit_model" not in visible


def _record_run(workspace_id: str, state: dict[str, Any], run: dict[str, Any]) -> None:
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    draft.record_run(directory, {"run_id": uuid4().hex, "action": "run", "draft_id": state["draft_id"],
                                 "draft_revision": state["revision"], "started_at": "2026-10-03T10:00:00+00:00", **run})


def test_brief_names_owners_card_verification_and_last_run() -> None:
    workspace_id = new_workspace()
    card = make_card(workspace_id, "Synthetic Monod card")
    state = _state(workspace_id, pbr_ops(model=False))
    pinned = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": card["id"]}), _origin())
    apply(workspace_id, pinned.action_id)
    state = draft.projection(workspace_id, state["draft_id"])
    ref = SurfaceRef(route_id="design-process", draft_id=state["draft_id"])
    before_run = surface_brief(workspace_id, ref).text
    assert "PBR=jarvis_bio" in before_run
    assert 'model card "Synthetic Monod card"' in before_run and card["revision"] in before_run
    assert "N source assumed NH3" in before_run and "1 candidate" in before_run
    assert "No run recorded." in before_run
    assert "Jarvis-native units arrive" not in before_run
    assert "set_unit_model" in before_run and "never edits or verifies parameter sets" in before_run
    _record_run(workspace_id, state, {
        "status": "completed", "mixed_solve": {"status": "completed", "reason": "converged", "history": []},
        "units": {"PBR": {"owner": "jarvis_bio", "branch": "productive",
                          "reported": {"hrt_d": {"value": 2.5, "units": "d"}, "lambda_h": {"value": 0.0236, "units": "1/h"},
                                       "dilution_h": {"value": 0.01667, "units": "1/h"}},
                          "unit_balances": {"biomass": {"residual": 1e-10, "tolerance": 1e-8},
                                            "oxygen": {"residual": -5e-9, "tolerance": 1e-8}}}}})
    after_run = surface_brief(workspace_id, ref).text
    assert "Last run completed; branch productive; HRT 2.5 d; thin-culture growth rate Λ 0.0236 1/h; " \
           "dilution rate D 0.01667 1/h; worst balance residual 0.5 of tolerance (balances close). " \
           "Productive: Λ > D" in after_run
    # The real 168 record shape: reason is "segment_failed"; the typed code and message live under errors.
    message = "The photobioreactor has no carrier flow through it, so dilution and residence time are undefined."
    _record_run(workspace_id, state, {"status": "segment_failed", "started_at": "2026-10-03T11:00:00+00:00",
                                      "mixed_solve": {"status": "segment_failed", "reason": "segment_failed",
                                                      "failed_segment": "PBR", "failed_units": [], "message": message,
                                                      "errors": {"code": "PBR_NO_THROUGHFLOW", "error_type": "PbrFailure",
                                                                 "message": message, "detail": None}}})
    failed = surface_brief(workspace_id, ref).text
    assert f"Last run segment_failed: the PBR failed with PBR_NO_THROUGHFLOW ({message.rstrip('.')})." in failed
    assert len(failed) <= 6000
    # A failure elsewhere keeps the generic wording.
    _record_run(workspace_id, state, {"status": "segment_failed", "started_at": "2026-10-03T12:00:00+00:00",
                                      "mixed_solve": {"status": "segment_failed", "reason": "segment_failed",
                                                      "failed_segment": "Mixer", "failed_units": ["Mixer"],
                                                      "errors": {"code": "check_failed"}}})
    assert "no PBR result recorded (reason segment_failed)." in surface_brief(workspace_id, ref).text


def test_brief_stays_within_the_6000_character_cap_with_35_objects_and_keeps_its_vocabulary() -> None:
    workspace_id = new_workspace()
    card = make_card(workspace_id, "A deliberately long model card name " * 2)
    ops: list[Any] = [*pbr_ops(model=False)]
    units = [(f"HX{index:02d}_" + "x" * 24, "DistillationColumn" if index % 2 else "HeatExchanger") for index in range(18)]
    for index, (tag, unit_type) in enumerate(units):
        ops.append(AddUnit(op="add_unit", id=f"u{index}", type=unit_type, tag=tag, x=index * 10, y=200))
        ops.append(AddStream(op="add_stream", id=f"w{index}", tag=f"Stream_{index:02d}_" + "y" * 20, x=index * 10, y=250))
    state = _state(workspace_id, ops)
    assert len(state["objects"]) >= 38
    pinned = submit(workspace_id, _request(state, {"op": "set_unit_model", "unit": "PBR", "card": card["id"]}), _origin())
    apply(workspace_id, pinned.action_id)
    state = draft.projection(workspace_id, state["draft_id"])
    brief = surface_brief(workspace_id, SurfaceRef(route_id="design-process", draft_id=state["draft_id"],
                                                   process_selection=[{"kind": "unit", "tag": "PBR"}]))
    assert len(brief.text) <= 6000
    assert brief.text.endswith("DWSIM Run stays operator-only; Thermo is edited in the operator editor.")
    assert "Objects (" in brief.text and "set_unit_model" in brief.text and "(Photobioreactor)" in brief.text
    assert "Selected unit owner: jarvis_bio; culture rule: pbr." in brief.text
    assert "set_unit_model" in brief.actions and "Run is operator-only" in " ".join(brief.limits)
    assert build_paths() is not None


def test_brief_shrinks_its_lists_but_never_its_tail_when_the_cap_would_be_exceeded() -> None:
    from app.modules.workspace_actions import service

    workspace_id = new_workspace()
    state = _state(workspace_id)
    document = draft.load_revision(draft.draft_dir(workspace_id, state["draft_id"]), state["revision"])["document"]
    objects = list(document["objects"].values())
    tail = "TAIL. " * 100
    huge_rows = ["R" * 900 for _ in range(6)]
    text = service._fit_process_brief(header="H\n", objects=objects, document=document, selected_line="S\n",
                                      pbr_rows=huge_rows, reaction_rows=[], run_summary="run", selected_context="", tail=tail)
    assert len(text) <= 6000 and text.endswith(tail)
    assert text.count("R" * 900) < 6
