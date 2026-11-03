"""#920: Process adapter selection between optics_t2.v1 and v2 (typed OD + wall absorption)."""

from __future__ import annotations

import json

import pytest

from app.modules.process_stack import draft, dynamic_engine, optics_t2
from app.modules.process_stack.draft_models import SetUnitParams
from tests.plumbing_170_support import new_workspace
from tests.test_optics_t2_dynamic_174 import _t2_state


def _prepared(extra: dict[str, tuple[float, str]] | None = None):
    workspace_id = new_workspace()
    state = _t2_state(workspace_id)
    if extra:
        state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
            SetUnitParams(op="set_unit_params", unit="pbr",
                          values={k: {"value": v, "unit": u} for k, (v, u) in extra.items()})])
    return workspace_id, state


def _inner(workspace_id: str, state: dict) -> float:
    snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    return snapshot.payload["units"][0]["params"]["tube_inner_diameter"]


def _prepare(extra=None):
    workspace_id, state = _prepared(extra)
    return dynamic_engine.prepare(workspace_id, state["draft_id"], "run")


def _optics(snapshot):
    return snapshot.payload["units"][0]["optics_t2"]


def test_default_stays_v1_without_outer_keys() -> None:
    optics = _optics(_prepare())
    assert optics["provenance"]["version"] == optics_t2.VERSION
    assert "outer_diameter" in optics["layout"] and optics["layout"]["outer_diameter"] is None
    assert optics["provenance"]["parameter_digest"] == optics_t2.Optics(**optics["layout"]).digest()
    # Digest equals the one a pure v1 layout produces (no OD keys entered).
    layout = {k: v for k, v in optics["layout"].items() if k not in {"outer_diameter", "wall_absorption_m_inv"}}
    assert optics_t2.Optics(**layout).digest() == optics["provenance"]["parameter_digest"]


def test_both_fields_select_v2_and_digest_changes() -> None:
    v1 = _optics(_prepare())
    v2 = _optics(_prepare({"tube_outer_diameter": (0.08, "m"), "wall_absorption": (1.5, "1/cm")}))
    assert v2["provenance"]["version"] == optics_t2.VERSION_V2
    assert v2["layout"]["outer_diameter"] == 0.08
    assert v2["layout"]["wall_absorption_m_inv"] == pytest.approx(150.0)
    assert v2["provenance"]["parameter_digest"] != v1["provenance"]["parameter_digest"]


@pytest.mark.parametrize("extra,code", [
    ({"tube_outer_diameter": (0.08, "m")}, "OPTICS_V2_GEOMETRY_INCOMPLETE"),
    ({"wall_absorption": (10.0, "1/m")}, "OPTICS_V2_GEOMETRY_INCOMPLETE"),
    ({"tube_outer_diameter": (0.001, "m"), "wall_absorption": (10.0, "1/m")}, "OPTICS_V2_GEOMETRY_INVALID"),
])
def test_incomplete_or_invalid_geometry_is_typed_before_run(extra, code) -> None:
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _prepare(extra)
    assert exc.value.code == code


def test_outer_equal_inner_is_refused() -> None:
    workspace_id, state = _prepared()
    inner = _inner(workspace_id, state)
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetUnitParams(op="set_unit_params", unit="pbr", values={
            "tube_outer_diameter": {"value": inner, "unit": "m"}, "wall_absorption": {"value": 0.0, "unit": "1/m"}})])
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert exc.value.code == "OPTICS_V2_GEOMETRY_INVALID"


def test_negative_absorption_rejected_by_draft_registry() -> None:
    workspace_id, state = _prepared()
    with pytest.raises(draft.DraftError) as exc:
        draft.patch(workspace_id, state["draft_id"], state["revision"], [
            SetUnitParams(op="set_unit_params", unit="pbr", values={
                "wall_absorption": {"value": -1.0, "unit": "1/m"}})])
    assert exc.value.code == "quantity_out_of_range"


def test_negative_absorption_in_adapter_is_typed() -> None:
    item = {"tag": "PBR", "params": {"tube_inner_diameter": 0.05, "tube_count": 4}, "unit": {"params": {
        **{k: {"si": v} for k, v in dict(tubes_across=2, layers=2, pitch_h=0.09, pitch_v=0.09, stagger_offset=0.0,
                                         axis_azimuth=0.0, centre_height_above_sea=0.1, n_wall=1.5, n_water=1.33,
                                         sea_albedo=0.06).items()},
        "tube_outer_diameter": {"si": 0.06}, "wall_absorption": {"si": -1.0}}}}
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        dynamic_engine.t2_optics_for_unit(item)
    assert exc.value.code == "OPTICS_V2_GEOMETRY_INVALID"
    item["unit"]["params"]["wall_absorption"] = {"si": 0.0}
    assert dynamic_engine.t2_optics_for_unit(item).version == optics_t2.VERSION_V2


@pytest.mark.parametrize("extra,version", [
    (None, optics_t2.VERSION),
    ({"tube_outer_diameter": (0.08, "m"), "wall_absorption": (10.0, "1/m")}, optics_t2.VERSION_V2),
])
def test_snapshot_replay_round_trips_both_layouts(extra, version) -> None:
    snapshot = _prepare(extra)
    restored = json.loads(json.dumps(snapshot.payload, allow_nan=False))
    result = dynamic_engine.run(dynamic_engine.Snapshot(payload=restored, digest=snapshot.digest),
                                cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded", result.error
    assert result.manifest["units"][0]["optics"]["version"] == version
    assert result.series["PBR_par_absorbed_umol_s_m"].min() >= 0


def _unset(workspace_id: str, state: dict, keys: list[str], unit: str = "pbr") -> dict:
    return draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetUnitParams(op="set_unit_params", unit=unit, unset=keys)])


def test_unset_both_returns_to_v1_with_original_digest() -> None:
    workspace_id, state = _prepared()
    before = _optics(dynamic_engine.prepare(workspace_id, state["draft_id"], "run"))
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetUnitParams(op="set_unit_params", unit="pbr", values={
            "tube_outer_diameter": {"value": 0.08, "unit": "m"}, "wall_absorption": {"value": 10.0, "unit": "1/m"}})])
    assert _optics(dynamic_engine.prepare(workspace_id, state["draft_id"], "run"))["provenance"]["version"] == optics_t2.VERSION_V2
    revision = state["revision"]
    state = _unset(workspace_id, state, ["tube_outer_diameter", "wall_absorption"])
    assert state["revision"] != revision
    after = _optics(dynamic_engine.prepare(workspace_id, state["draft_id"], "run"))
    assert after["provenance"]["version"] == optics_t2.VERSION
    assert after["provenance"]["parameter_digest"] == before["provenance"]["parameter_digest"]


def test_unset_recovers_half_set_pair() -> None:
    workspace_id, state = _prepared({"tube_outer_diameter": (0.08, "m")})
    with pytest.raises(dynamic_engine.DynamicError):
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    state = _unset(workspace_id, state, ["tube_outer_diameter"])
    assert _optics(dynamic_engine.prepare(workspace_id, state["draft_id"], "run"))["provenance"]["version"] == optics_t2.VERSION


def test_unset_disallowed_key_is_rejected() -> None:
    workspace_id, state = _prepared()
    for keys in (["tube_inner_diameter"], ["pitch_h"], ["wall_absorption", "n_wall"]):
        with pytest.raises(draft.DraftError) as exc:
            _unset(workspace_id, state, keys)
        assert exc.value.code == "unset_unsupported"


def test_unset_absent_key_is_noop() -> None:
    workspace_id, state = _prepared()
    state = _unset(workspace_id, state, ["tube_outer_diameter", "wall_absorption"])
    assert _optics(dynamic_engine.prepare(workspace_id, state["draft_id"], "run"))["provenance"]["version"] == optics_t2.VERSION
