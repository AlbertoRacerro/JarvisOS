"""Spec 170: set_unit_model DraftOp with CAS, undo, staleness and refusal."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.modules.process_stack import draft, mixed
from app.modules.process_stack.draft_models import AddUnit, Move, SetUnitModel
from tests.plumbing_170_support import PIN, new_workspace, pbr_ops

OTHER_PIN = {"card_id": "card-2", "card_revision": "r-" + "c" * 16, "card_digest": "d" * 64}


def _draft(workspace_id: str, **kwargs):
    created = draft.create_draft(workspace_id, "PBR draft")
    return draft.patch(workspace_id, created["draft_id"], created["revision"], pbr_ops(**kwargs))


def _pbr(state: dict) -> dict:
    return next(item for item in state["objects"] if item["type"] == "PhotobioreactorT1")


def test_set_unit_model_sets_replaces_and_clears_the_pin() -> None:
    workspace_id = new_workspace()
    state = _draft(workspace_id)
    assert "model" not in _pbr(state)
    pinned = draft.patch(workspace_id, state["draft_id"], state["revision"],
                         [SetUnitModel(op="set_unit_model", unit="pbr", model=PIN)])  # type: ignore[arg-type]
    assert _pbr(pinned)["model"] == PIN
    repinned = draft.patch(workspace_id, state["draft_id"], pinned["revision"],
                           [SetUnitModel(op="set_unit_model", unit="pbr", model=OTHER_PIN)])  # type: ignore[arg-type]
    assert _pbr(repinned)["model"] == OTHER_PIN
    cleared = draft.patch(workspace_id, state["draft_id"], repinned["revision"],
                          [SetUnitModel(op="set_unit_model", unit="pbr", model=None)])
    assert "model" not in _pbr(cleared)
    assert "PBR_REQUIRES_MODEL_CARD" in {item["code"] for item in cleared["findings"]}


def test_set_unit_model_uses_compare_and_swap() -> None:
    workspace_id = new_workspace()
    state = _draft(workspace_id)
    op = SetUnitModel(op="set_unit_model", unit="pbr", model=PIN)  # type: ignore[arg-type]
    draft.patch(workspace_id, state["draft_id"], state["revision"], [op])
    with pytest.raises(draft.DraftError) as error:
        draft.patch(workspace_id, state["draft_id"], state["revision"], [op])  # the first revision is now stale
    assert error.value.code == "revision_conflict" and error.value.status == 409


def test_set_unit_model_undo_restores_the_previous_revision() -> None:
    workspace_id = new_workspace()
    state = _draft(workspace_id)
    pinned = draft.patch(workspace_id, state["draft_id"], state["revision"],
                         [SetUnitModel(op="set_unit_model", unit="pbr", model=PIN)])  # type: ignore[arg-type]
    restored = draft.restore(workspace_id, state["draft_id"], pinned["revision"], state["revision"])
    assert "model" not in _pbr(restored)
    assert [row["ops"] for row in draft.list_revisions(workspace_id, state["draft_id"])][:2] == [["restore"], ["set_unit_model"]]


@pytest.mark.parametrize("unit_type", ["Heater", "Pump", "SpecifiedSeparator", "Mixer"])
def test_set_unit_model_is_refused_for_other_unit_types(unit_type: str) -> None:
    document = draft.apply_ops(draft.empty_document("x"), [AddUnit(op="add_unit", id="u", type=unit_type, tag="U", x=0, y=0)])
    with pytest.raises(draft.DraftError) as error:
        draft.apply_ops(document, [SetUnitModel(op="set_unit_model", unit="u", model=PIN)])  # type: ignore[arg-type]
    assert error.value.code == "unit_model_unsupported" and error.value.detail["op_index"] == 0


def test_set_unit_model_rejects_malformed_pins_and_unknown_units() -> None:
    for bad in ({**PIN, "card_revision": "r-short"}, {**PIN, "card_digest": "z" * 64}, {**PIN, "extra": 1}):
        with pytest.raises(ValidationError):
            SetUnitModel(op="set_unit_model", unit="pbr", model=bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SetUnitModel.model_validate({"op": "set_unit_model", "unit": "pbr"})  # null must be explicit
    document = draft.apply_ops(draft.empty_document("x"), [])
    with pytest.raises(draft.DraftError) as error:
        draft.apply_ops(document, [SetUnitModel(op="set_unit_model", unit="ghost", model=None)])
    assert error.value.code == "object_not_found"


def test_schema_version_stays_one_and_old_documents_load_unchanged() -> None:
    document = draft.apply_ops(draft.empty_document("x"), [AddUnit(op="add_unit", id="u", type="Pump", tag="U", x=0, y=0)])
    assert document["schema_version"] == 1 and "model" not in document["objects"]["u"]
    pinned = pbr_ops(model=True)
    assert draft.apply_ops(draft.empty_document("p"), pinned)["schema_version"] == 1


def test_http_patch_accepts_the_op_and_reports_cas_and_refusal() -> None:
    workspace_id = new_workspace()
    state = _draft(workspace_id)
    base = f"/workspaces/{workspace_id}/process/drafts/{state['draft_id']}/patch"
    body = {"expected_revision": state["revision"], "ops": [{"op": "set_unit_model", "unit": "pbr", "model": PIN}]}
    with TestClient(app) as client:
        ok = client.post(base, json=body)
        assert ok.status_code == 200, ok.text
        assert _pbr(ok.json())["model"] == PIN
        stale = client.post(base, json=body)
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "revision_conflict"
        refused = client.post(base, json={"expected_revision": ok.json()["revision"], "ops": [
            {"op": "add_unit", "id": "h", "type": "Heater", "tag": "H", "x": 0, "y": 0},
            {"op": "set_unit_model", "unit": "h", "model": PIN}]})
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "unit_model_unsupported"


def _solved(document: dict, workspace_id: str) -> dict:
    return {"action": "run", "status": "completed", "run_id": "run-1", "draft_revision": "1:old", "mixed_solve": {"status": "completed"},
            "process_fingerprint": mixed.fingerprint(document, mixed.partition(document), workspace_id)}


def test_pin_edit_and_parameter_edit_stale_results_but_layout_moves_do_not() -> None:
    workspace_id = new_workspace()
    state = _draft(workspace_id, model=True)
    document = draft.load_revision(draft.draft_dir(workspace_id, state["draft_id"]), state["revision"])["document"]
    head = {"revision": "9:new", "seq": 9}
    solved = _solved(document, workspace_id)

    def current(edited: dict) -> str:
        return str(draft.results_state(head, [solved], edited, edits_since=1, workspace_id=workspace_id)["state"])

    assert current(document) == "current"
    moved = draft.apply_ops(document, [Move(op="move", id="pbr", x=400, y=40)])
    assert current(moved) == "current"
    repinned = draft.apply_ops(document, [SetUnitModel(op="set_unit_model", unit="pbr", model=OTHER_PIN)])  # type: ignore[arg-type]
    assert current(repinned) == "stale"
    cleared = draft.apply_ops(document, [SetUnitModel(op="set_unit_model", unit="pbr", model=None)])
    assert current(cleared) == "stale"
    from app.modules.process_stack.draft_models import DraftQuantity, SetUnitParams

    edited = draft.apply_ops(document, [SetUnitParams(op="set_unit_params", unit="pbr", values={
        "peak_par": DraftQuantity(value=1000, unit="umol/(m2.s)")})])
    assert current(edited) == "stale"
