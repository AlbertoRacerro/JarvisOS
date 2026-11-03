"""Spec 173: additive dynamic projection, byte-identical legacy projection, staleness digest and refusals."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import Move, SetController, SetScenario, SetSchedule
from tests.plumbing_170_support import new_workspace, pbr_ops
from tests.test_process_dynamic_jobs import (
    _start,
    _wait,
    api,  # noqa: F401
)

CONTROLLER = {"type": "onoff", "measurement": "X", "unit": "PBR", "actuator": "feed:Feed",
              "output_unit": "m3/s", "cadence_s": 60, "setpoint": 0.5, "lower": 0.0, "upper": 0.001, "output": 0.0}
SCENARIO = {"units": ["PBR"], "profiles": [{"profile_id": "p1", "digest": "d1"}],
            "start_utc": "2026-01-01T00:00:00Z", "end_utc": "2026-01-02T00:00:00Z", "output_cadence_s": 3600,
            "temperature_source": "unit_mean", "initial": {"PBR": {"X": 0.2, "N": 0.05, "O2": 0.008}}}
DRAW = {"type": "actions", "time_s": 86400, "every_s": 172800, "count": 3,
        "actions": [{"type": "draw", "tank": "PBR", "loop_fraction": 0.2}]}


def _draft() -> tuple[str, dict[str, Any]]:
    workspace_id = new_workspace()
    state = draft.create_draft(workspace_id, "dynamic studio 173")
    return workspace_id, draft.patch(workspace_id, state["draft_id"], state["revision"], pbr_ops())


def _patch(workspace_id: str, state: dict[str, Any], ops: list[Any]) -> dict[str, Any]:
    return draft.patch(workspace_id, state["draft_id"], state["revision"], ops)


def test_draft_without_dynamic_content_has_no_dynamic_key_and_stable_digest() -> None:
    workspace_id, state = _draft()
    assert "dynamic" not in state
    assert "dynamic" not in draft.projection(workspace_id, state["draft_id"])
    before = json.dumps(draft.projection(workspace_id, state["draft_id"]), sort_keys=True)
    digest = draft.current_digest(workspace_id, state["draft_id"])
    again = json.dumps(draft.projection(workspace_id, state["draft_id"]), sort_keys=True)
    assert before == again
    assert digest == draft.current_digest(workspace_id, state["draft_id"])
    # An empty set of dynamic maps is not content either.
    document = draft.empty_document("empty")
    assert draft.dynamic_content(document) == {}
    assert draft.content_digest(document) == draft.content_digest({**document, "schedules": {}, "controllers": {}, "scenarios": {}})


def test_projection_returns_stored_typed_content_and_deleting_it_restores_the_legacy_shape() -> None:
    workspace_id, state = _draft()
    legacy = {key: value for key, value in state.items() if key not in {"revision", "seq"}}
    state = _patch(workspace_id, state, [
        SetSchedule(op="set_schedule", id="sched", value={"events": [DRAW]}),
        SetController(op="set_controller", id="ctrl", value=CONTROLLER),
        SetScenario(op="set_scenario", id="run", value={**SCENARIO, "schedule_id": "sched", "controllers": ["ctrl"]}),
    ])
    projected = draft.projection(workspace_id, state["draft_id"])["dynamic"]
    assert set(projected) == {"scenarios", "schedules", "controllers"}
    assert projected["schedules"]["sched"]["events"] == [DRAW]
    assert projected["controllers"]["ctrl"]["setpoint"] == 0.5
    assert projected["scenarios"]["run"]["schedule_id"] == "sched"
    from app.modules.process_stack.dynamic_models import Scenario

    Scenario.model_validate(projected["scenarios"]["run"])
    from app.modules.process_stack.draft_models import DeleteController, DeleteScenario, DeleteSchedule

    state = _patch(workspace_id, state, [DeleteScenario(op="delete_scenario", id="run"),
                                         DeleteController(op="delete_controller", id="ctrl"),
                                         DeleteSchedule(op="delete_schedule", id="sched")])
    after = draft.projection(workspace_id, state["draft_id"])
    assert "dynamic" not in after
    assert {key: value for key, value in after.items() if key not in {"revision", "seq"}}.keys() == legacy.keys()


def test_edits_make_the_bound_digest_stale_and_layout_moves_do_not() -> None:
    workspace_id, state = _draft()
    state = _patch(workspace_id, state, [
        SetSchedule(op="set_schedule", id="sched", value={"events": [DRAW]}),
        SetController(op="set_controller", id="ctrl", value=CONTROLLER),
        SetScenario(op="set_scenario", id="run", value={**SCENARIO, "schedule_id": "sched", "controllers": ["ctrl"]}),
    ])
    bound = draft.current_digest(workspace_id, state["draft_id"])
    moved = _patch(workspace_id, state, [Move(op="move", id="pbr", x=300, y=40)])
    assert draft.current_digest(workspace_id, moved["draft_id"]) == bound
    edits = {
        "schedule": SetSchedule(op="set_schedule", id="sched", value={"events": [{**DRAW, "every_s": 86400}]}),
        "controller": SetController(op="set_controller", id="ctrl", value={**CONTROLLER, "setpoint": 0.6}),
        "scenario": SetScenario(op="set_scenario", id="run", value={
            **SCENARIO, "schedule_id": "sched", "controllers": ["ctrl"], "output_cadence_s": 7200}),
    }
    seen = {bound}
    current = moved
    for name, op in edits.items():
        current = _patch(workspace_id, current, [op])
        digest = draft.current_digest(workspace_id, current["draft_id"])
        assert digest not in seen, name
        seen.add(digest)
    assert len(seen) == 4


@pytest.mark.parametrize(("ops", "code"), [
    ([SetController(op="set_controller", id="c", value={**CONTROLLER, "cadence_s": 10})], "dynamic_content_invalid"),
    ([SetScenario(op="set_scenario", id="run", value={**SCENARIO, "schedule_id": "missing"})], "dynamic_schedule_not_found"),
    ([SetScenario(op="set_scenario", id="run", value={**SCENARIO, "units": ["Nope"]})], "dynamic_unit_not_found"),
    ([SetScenario(op="set_scenario", id="run", value={**SCENARIO, "controllers": ["gone"]})], "dynamic_controller_not_found"),
    ([SetSchedule(op="set_schedule", id="s", value={"events": [{"type": "actions", "time_s": 0, "actions": [
        {"type": "draw", "tank": "PBR", "loop_fraction": 2}]}]})], "dynamic_content_invalid"),
])
def test_refusals_carry_backend_codes_and_leave_the_revision_unchanged(ops: list[Any], code: str) -> None:
    workspace_id, state = _draft()
    with pytest.raises(draft.DraftError) as refused:
        _patch(workspace_id, state, ops)
    assert refused.value.code == code
    assert draft.projection(workspace_id, state["draft_id"])["revision"] == state["revision"]


def test_series_pages_by_inclusive_windows_reassemble_the_stored_artifact_rows(api: Any) -> None:  # noqa: F811
    """The client pages a run above 5000 points by inclusive t_s windows and drops the repeated boundary row."""
    import numpy as np

    from app.core.paths import build_paths

    client, engine, wid, did, base = api
    engine.result = engine.EngineResult(rows=12001)
    _, job_id = _start(api)
    _wait(client, base, did, job_id, "succeeded")
    url = f"{base}/{did}/dynamic/runs/{job_id}/series"
    overview = client.get(url, params={"channels": "X", "max_points": 5000}).json()
    assert overview["stride"] > 1 and overview["total_points"] == 12001
    stride = overview["stride"]
    ticks = overview["channels"]["t_s"]
    step = max(1, 4999 // stride)
    rows_t: list[float] = []
    rows_x: list[float] = []
    for first in range(0, len(ticks), step):
        last = min(first + step, len(ticks) - 1)
        page = client.get(url, params={"channels": "X", "start_s": ticks[first], "end_s": ticks[last] if last < len(ticks) - 1 else 1e18,
                                       "max_points": 5000}).json()
        assert page["stride"] == 1, "a page must be exact, not strided"
        for t_value, x_value in zip(page["channels"]["t_s"], page["channels"]["X"], strict=True):
            if not rows_t or t_value > rows_t[-1]:
                rows_t.append(t_value)
                rows_x.append(x_value)
    with np.load(build_paths().data_root / "process_dynamic" / wid / job_id / "series.npz") as stored:
        assert rows_t == stored["t_s"].tolist() and rows_x == stored["X"].tolist()
